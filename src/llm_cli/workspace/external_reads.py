"""Bounded advisory read registrations; canonical publication bases stay intact.

The daemon initializes this LRU once after migrations. Registration does no
history query: only an overflow deletes a known primary-key row. Cache deltas
are confirmed after the surrounding SQLite transaction commits, under the same
registry lock, so a failed read cannot advance or evict an in-memory entry.
"""

from __future__ import annotations

import sqlite3
import threading
import weakref
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from llm_cli.storage.connection import immediate_transaction

if TYPE_CHECKING:
    from llm_cli.storage.control import ControlStore

MAX_READ_REGISTRATIONS = 20_000
Scope = tuple[str, int]


@dataclass
class _Reads:
    scope: Scope
    paths: OrderedDict[str, None]
    truncated: bool = False


class ReadRegistry:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.sessions: dict[str, _Reads] = {}
        self.ready = False

    def initialize(self, connection: sqlite3.Connection) -> None:
        with self.lock:
            if self.ready:
                return
            sessions: dict[str, _Reads] = {}
            with immediate_transaction(connection):
                # Closed sessions and stale switch bindings need no advisory
                # observations. Their canonical source-base records are retained.
                connection.execute(
                    "DELETE FROM workspace_external_observations WHERE "
                    "NOT EXISTS (SELECT 1 FROM sessions s WHERE "
                    "s.session_id=workspace_external_observations.session_id "
                    "AND s.state IN ('active','disconnected')) OR NOT EXISTS "
                    "(SELECT 1 FROM workspace_read_observations r WHERE "
                    "r.session_id=workspace_external_observations.session_id "
                    "AND r.relative_path=workspace_external_observations.relative_path)"
                )
                active = connection.execute(
                    "SELECT session_id,workspace_id,workspace_binding_generation "
                    "FROM sessions WHERE state IN ('active','disconnected') "
                    "AND workspace_id IS NOT NULL"
                ).fetchall()
                for session in active:
                    session_id = str(session["session_id"])
                    connection.execute(
                        "DELETE FROM workspace_external_observations WHERE "
                        "session_id=? AND relative_path NOT IN ("
                        "SELECT x.relative_path FROM workspace_external_observations x "
                        "LEFT JOIN workspace_read_observations r ON "
                        "r.session_id=x.session_id AND r.relative_path=x.relative_path "
                        "WHERE x.session_id=? ORDER BY r.observed_at DESC, "
                        "x.relative_path LIMIT ?)",
                        (session_id, session_id, MAX_READ_REGISTRATIONS),
                    )
                    paths = OrderedDict(
                        (str(row[0]), None)
                        for row in connection.execute(
                            "SELECT x.relative_path FROM "
                            "workspace_external_observations x "
                            "JOIN workspace_read_observations r ON "
                            "r.session_id=x.session_id "
                            "AND r.relative_path=x.relative_path "
                            "WHERE x.session_id=? ORDER BY r.observed_at, "
                            "x.relative_path DESC",
                            (session_id,),
                        )
                    )
                    truncated = (
                        connection.execute(
                            "SELECT 1 FROM workspace_read_observations "
                            "WHERE session_id=? "
                            "LIMIT 1 OFFSET ?",
                            (session_id, MAX_READ_REGISTRATIONS),
                        ).fetchone()
                        is not None
                    )
                    sessions[session_id] = _Reads(
                        (
                            str(session["workspace_id"]),
                            int(session["workspace_binding_generation"]),
                        ),
                        paths,
                        truncated,
                    )
            self.sessions = sessions
            self.ready = True

    def truncated(self, session_id: str) -> bool:
        with self.lock:
            reads = self.sessions.get(session_id)
            return reads is not None and reads.truncated


class ReadRegistration:
    """A bounded cache delta, applied only after its SQLite writes commit."""

    def __init__(self, registry: ReadRegistry, session_id: str) -> None:
        self.registry, self.session_id = registry, session_id
        self.scope: Scope | None = None
        self.original: OrderedDict[str, None] = OrderedDict()
        self.oldest: Iterator[str] = iter(())
        self.added: OrderedDict[str, None] = OrderedDict()
        self.evicted: set[str] = set()
        self.count = 0
        self.limited = False

    def bind(self, connection: sqlite3.Connection, scope: Scope) -> None:
        if self.scope is not None:
            if self.scope != scope:
                raise ValueError("external registration binding changed")
            return
        self.scope = scope
        prior = self.registry.sessions.get(self.session_id)
        if prior is not None and prior.scope != scope:
            connection.execute(
                "DELETE FROM workspace_external_observations WHERE session_id=?",
                (self.session_id,),
            )
            prior = None
        if prior is not None:
            self.original = prior.paths
            self.limited = prior.truncated
        self.count = len(self.original)
        self.oldest = iter(self.original)

    def remember(
        self, connection: sqlite3.Connection, path: str, *, actual_read: bool
    ) -> None:
        assert self.scope is not None
        if path in self.added:
            if actual_read:
                self.added.move_to_end(path)
            return
        if path in self.original and path not in self.evicted:
            if actual_read:
                self.added[path] = None
            return
        if self.count >= MAX_READ_REGISTRATIONS:
            for oldest in self.oldest:
                if oldest not in self.evicted and oldest not in self.added:
                    break
            else:
                oldest = next(iter(self.added))
                self.added.pop(oldest)
            self.evicted.add(oldest)
            connection.execute(
                "DELETE FROM workspace_external_observations "
                "WHERE session_id=? AND relative_path=?",
                (self.session_id, oldest),
            )
            self.count -= 1
            self.limited = True
        self.added[path] = None
        self.count += 1

    def confirm(self) -> None:
        if self.scope is None:
            return
        for path in self.evicted:
            self.original.pop(path, None)
        for path in self.added:
            self.original.pop(path, None)
            self.original[path] = None
        self.registry.sessions[self.session_id] = _Reads(
            self.scope, self.original, self.limited
        )


_REGISTRY_LOCK = threading.Lock()
_REGISTRIES: weakref.WeakValueDictionary[tuple[Path, int, int], ReadRegistry] = (
    weakref.WeakValueDictionary()
)


def initialize_read_registry(
    store: ControlStore, connection: sqlite3.Connection
) -> ReadRegistry:
    path = store.path.resolve(strict=True)
    status = path.stat()
    key = (path, status.st_dev, status.st_ino)
    with _REGISTRY_LOCK:
        registry = _REGISTRIES.get(key)
        if registry is None:
            registry = ReadRegistry()
            _REGISTRIES[key] = registry
    registry.initialize(connection)
    return registry


@contextmanager
def registering_reads(
    store: ControlStore, session_id: str
) -> Iterator[ReadRegistration]:
    registry = store._external_reads
    if registry is None:
        raise ValueError("initialize the control store before registering reads")
    # Enter before the SQLite transaction and confirm after it commits. Every
    # registration/scanner writer uses this order, so concurrent readers cannot
    # deadlock by holding the database lock while waiting for this lock.
    with registry.lock:
        registration = ReadRegistration(registry, session_id)
        yield registration
        registration.confirm()


@contextmanager
def closing_reads(store: ControlStore, session_id: str) -> Iterator[None]:
    registry = store._external_reads
    if registry is None:
        yield
        return
    with registry.lock:
        yield
        registry.sessions.pop(session_id, None)
