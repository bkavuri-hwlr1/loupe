"""SQLite connection policy for the local control and retrieval stores.

Connections use explicit transactions (``isolation_level=None``).  Code that
changes authoritative state must use :func:`immediate_transaction` so that the
read/decide/write sequence is serialized with every other writer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

SynchronousMode = Literal["FULL", "NORMAL"]


def connect_database(
    path: str | Path,
    *,
    synchronous: SynchronousMode,
    timeout_seconds: float = 30.0,
) -> sqlite3.Connection:
    """Open a configured SQLite database.

    ``FULL`` is used for the authoritative control database.  Knowledge and
    vector projections are reconstructable and use ``NORMAL``.  WAL is enabled
    for all three so readers do not block the daemon's writer transaction.
    """

    database_path = Path(path).expanduser()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        database_path,
        timeout=timeout_seconds,
        isolation_level=None,
    )
    # SQLite creates the file during connect.  Tighten an existing permissive
    # mode too; control metadata can reveal repository and task identities.
    database_path.chmod(0o600)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {max(1, int(timeout_seconds * 1000))}")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(f"PRAGMA synchronous = {synchronous}")
    connection.execute("PRAGMA trusted_schema = OFF")
    return connection


def connect_control(
    path: str | Path,
    *,
    timeout_seconds: float = 30.0,
) -> sqlite3.Connection:
    """Open the authoritative control database with full WAL durability."""

    return connect_database(
        path,
        synchronous="FULL",
        timeout_seconds=timeout_seconds,
    )


def connect_knowledge(
    path: str | Path,
    *,
    timeout_seconds: float = 30.0,
) -> sqlite3.Connection:
    """Open the rebuildable lexical/graph knowledge database."""

    return connect_database(
        path,
        synchronous="NORMAL",
        timeout_seconds=timeout_seconds,
    )


def connect_vectors(
    path: str | Path,
    *,
    timeout_seconds: float = 30.0,
) -> sqlite3.Connection:
    """Open the disposable vector projection database."""

    return connect_database(
        path,
        synchronous="NORMAL",
        timeout_seconds=timeout_seconds,
    )


@contextmanager
def immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Run one fail-closed ``BEGIN IMMEDIATE`` transaction.

    An authority decision must never be split across implicit transactions.
    ``BEGIN IMMEDIATE`` obtains the SQLite write reservation before reading the
    queue, preventing two processes from both deciding that a scope is free.
    """

    if connection.in_transaction:
        raise sqlite3.ProgrammingError("nested authority transactions are forbidden")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
