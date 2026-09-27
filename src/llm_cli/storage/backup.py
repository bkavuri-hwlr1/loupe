"""Crash-safe SQLite backups for local coordinator databases.

The source is copied through SQLite's online backup API so committed pages that
still live in a WAL file are included.  A backup is validated and made durable
in the destination directory before it atomically replaces the requested path.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import tempfile
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path


class BackupError(RuntimeError):
    """A database could not be backed up without weakening safety guarantees."""


class BackupIntegrityError(BackupError):
    """SQLite rejected the generated backup during its integrity check."""


@dataclass(frozen=True, slots=True)
class BackupMetadata:
    """Verified metadata for one atomically installed SQLite backup.

    ``size`` is measured in bytes and ``sha256`` covers the complete installed
    database file.  ``schema_version`` is the highest checksummed migration
    version, or SQLite's ``user_version`` for a database without that table.
    """

    path: Path
    size: int
    sha256: str
    schema_version: int


def backup_database(
    source: str | Path,
    destination: str | Path,
    *,
    timeout_seconds: float = 30.0,
) -> BackupMetadata:
    """Create and atomically install a verified backup of ``source``.

    The destination's parent must already exist and must not be a symlink.  The
    function never copies the source file directly: doing so could omit commits
    that have not yet been checkpointed from a live WAL.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    source_path = _absolute_path(source)
    destination_path = _absolute_path(destination)
    _validate_paths(source_path, destination_path)

    temporary_path: Path | None = None
    try:
        temporary_path = _temporary_database_path(destination_path.parent)
        schema_version = _copy_and_validate(
            source_path,
            temporary_path,
            timeout_seconds=timeout_seconds,
        )
        os.chmod(temporary_path, 0o600)
        digest, size = _digest_and_fsync(temporary_path)
        os.replace(temporary_path, destination_path)
        temporary_path = None
        _fsync_directory(destination_path.parent)
    except BackupError:
        raise
    except (OSError, sqlite3.Error, ValueError) as error:
        raise BackupError(
            f"could not back up {source_path} to {destination_path}: {error}"
        ) from error
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)

    return BackupMetadata(
        path=destination_path,
        size=size,
        sha256=digest,
        schema_version=schema_version,
    )


def _absolute_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.name:
        raise BackupError("backup paths must name an explicit file")
    return Path(os.path.abspath(path))


def _validate_paths(source: Path, destination: Path) -> None:
    if not source.exists():
        raise BackupError(f"backup source does not exist: {source}")
    if not source.is_file():
        raise BackupError(f"backup source is not a regular file: {source}")

    parent = destination.parent
    if not parent.exists():
        raise BackupError(f"backup destination parent does not exist: {parent}")
    if parent.is_symlink():
        raise BackupError(f"backup destination parent must not be a symlink: {parent}")
    if not parent.is_dir():
        raise BackupError(f"backup destination parent is not a directory: {parent}")

    if destination.is_symlink():
        raise BackupError(f"backup destination must not be a symlink: {destination}")
    if destination.exists():
        destination_mode = destination.lstat().st_mode
        if not stat.S_ISREG(destination_mode):
            raise BackupError(
                f"backup destination is not a regular file: {destination}"
            )

    if source == destination:
        raise BackupError("backup source and destination must be different files")
    if destination.exists() and source.samefile(destination):
        raise BackupError("backup source and destination must be different files")


def _temporary_database_path(parent: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=".llm-coord-backup-",
        suffix=".tmp",
        dir=parent,
    )
    os.close(descriptor)
    return Path(name)


def _copy_and_validate(
    source: Path,
    temporary: Path,
    *,
    timeout_seconds: float,
) -> int:
    source_uri = f"{source.as_uri()}?mode=ro"
    with (
        closing(
            sqlite3.connect(
                source_uri,
                uri=True,
                isolation_level=None,
                timeout=timeout_seconds,
            )
        ) as source_connection,
        closing(
            sqlite3.connect(
                temporary,
                isolation_level=None,
                timeout=timeout_seconds,
            )
        ) as destination_connection,
    ):
        source_connection.backup(destination_connection, pages=256, sleep=0.01)
        # A backup is a standalone file.  Do not publish a database whose
        # usability depends on an uninstalled temporary WAL sidecar.
        destination_connection.execute("PRAGMA journal_mode = DELETE")
        _require_integrity(destination_connection)
        return _schema_version(destination_connection)


def _require_integrity(connection: sqlite3.Connection) -> None:
    messages = tuple(
        str(row[0]) for row in connection.execute("PRAGMA integrity_check")
    )
    if messages == ("ok",):
        return
    summary = "; ".join(messages[:5])[:1000]
    raise BackupIntegrityError(f"generated backup failed integrity_check: {summary}")


def _schema_version(connection: sqlite3.Connection) -> int:
    migration_table = connection.execute(
        """
        SELECT 1
        FROM sqlite_schema
        WHERE type = 'table' AND name = 'schema_migrations'
        """
    ).fetchone()
    if migration_table is not None:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        assert row is not None
        return int(row[0])
    row = connection.execute("PRAGMA user_version").fetchone()
    assert row is not None
    return int(row[0])


def _digest_and_fsync(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_RDONLY)
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
