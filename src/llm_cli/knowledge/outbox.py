"""ID-only projection intents; never open the optional knowledge database."""

from __future__ import annotations

import hashlib
import sqlite3
import weakref
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_cli.storage.control import ControlStore

_enabled: weakref.WeakKeyDictionary[ControlStore, Path] = weakref.WeakKeyDictionary()


def configure(store: ControlStore, *, enabled: bool, candidate_dir: Path) -> None:
    if enabled:
        _enabled[store] = candidate_dir
    else:
        _enabled.pop(store, None)


def artifact_directory(store: ControlStore) -> Path | None:
    return _enabled.get(store)


def subscribed(
    connection: sqlite3.Connection, store: ControlStore, checkout: str
) -> bool:
    return (
        store in _enabled
        and connection.execute(
            "SELECT 1 FROM knowledge_subscriptions WHERE checkout_id=? AND enabled=1",
            (checkout,),
        ).fetchone()
        is not None
    )


def enqueue_batch(
    connection: sqlite3.Connection,
    store: ControlStore,
    *,
    checkout_id: str,
    workspace_id: str,
    batch_id: str,
    sequence: int,
    now: int,
) -> None:
    if not subscribed(connection, store, checkout_id):
        return
    shared = connection.execute(
        "SELECT 1 FROM workspaces WHERE workspace_id=? AND mode='shared'",
        (workspace_id,),
    ).fetchone()
    if shared:
        connection.execute(
            "INSERT OR IGNORE INTO knowledge_outbox(checkout_id,event_sequence,"
            "batch_id,created_at) VALUES (?,?,?,?)",
            (checkout_id, sequence, batch_id, now),
        )


def enqueue_external(
    connection: sqlite3.Connection,
    store: ControlStore,
    *,
    checkout_id: str,
    path: str,
    identity: str | None,
    content_hash: str | None,
    sequence: int,
    now: int,
) -> None:
    if not subscribed(connection, store, checkout_id):
        return
    version_id = hashlib.sha256(
        f"{checkout_id}\0{sequence}\0{path}".encode()
    ).hexdigest()
    connection.execute(
        "INSERT OR IGNORE INTO knowledge_external_versions(version_id,checkout_id,"
        "relative_path,identity_json,content_hash) VALUES (?,?,?,?,?)",
        (version_id, checkout_id, path, identity, content_hash),
    )
    connection.execute(
        "INSERT OR IGNORE INTO knowledge_outbox(checkout_id,event_sequence,"
        "version_id,created_at) VALUES (?,?,?,?)",
        (checkout_id, sequence, version_id, now),
    )
