"""Bounded external-change observations for paths shared sessions have read.

Metadata is a cheap invalidation hint. Stable, screened source identities decide
net changes; excluded, secret, nonregular and oversized files stay opaque.
No scan changes a session's publication base or terminal delivery cursor.

A scan covers a session's most recently read paths. A path that changes while
it is read, or does not fit the scan's byte or time budget, keeps its previous
state and is checked by a later scan, so large outside changes catch up over
several scans instead of failing all of them.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from llm_cli.agent.source_policy import contains_secret_material, excluded_paths
from llm_cli.coordination.scopes import normalize_changed_path
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.storage.connection import immediate_transaction
from llm_cli.workspace.broker import MAX_SHARED_TEXT_BYTES
from llm_cli.workspace.identity import (
    ABSENT,
    EXECUTABLE_MODE,
    REGULAR_MODE,
    FileIdentity,
    ObjectKind,
    content_identity,
)

if TYPE_CHECKING:
    from llm_cli.storage.control import ControlStore

MAX_PATHS = 1_000
MAX_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = MAX_SHARED_TEXT_BYTES
MAX_SECONDS = 5.0
EVENT_TYPE = "workspace.external_changed"


def _encode(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _stamp(status: os.stat_result) -> str:
    return _encode(
        [
            status.st_dev,
            status.st_ino,
            status.st_mode,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
        ]
    )


@contextmanager
def _parent(root: Path, relative: str) -> Iterator[tuple[int, str]]:
    """Anchor every lookup to no-follow directory descriptors."""
    if normalize_changed_path(relative) != relative:
        raise ValueError("invalid observed path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = [os.open(root, flags)]
    try:
        parts = Path(relative).parts
        for part in parts[:-1]:
            descriptor = os.open(part, flags, dir_fd=descriptors[-1])
            descriptors.append(descriptor)
            try:
                os.stat(".git", dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("observed path enters a nested repository")
        yield descriptors[-1], parts[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def metadata(root: Path, relative: str) -> str:
    """An opaque stamp, never a link target or file bytes."""
    try:
        with _parent(root, relative) as (parent, leaf):
            try:
                return _stamp(os.stat(leaf, dir_fd=parent, follow_symlinks=False))
            except FileNotFoundError:
                return "absent"
    except (OSError, ValueError):
        # A formerly safe parent became inaccessible, a symlink or a gitlink.
        return "unavailable"


def register_read(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    workspace_id: str,
    relative_path: str,
) -> None:
    """Remember metadata at an actual read without duplicating its digest."""
    row = connection.execute(
        "SELECT c.canonical_path, h.next_event_sequence FROM workspaces w "
        "JOIN checkouts c ON c.checkout_id=w.checkout_id "
        "JOIN checkout_heads h ON h.checkout_id=c.checkout_id "
        "WHERE w.workspace_id=?",
        (workspace_id,),
    ).fetchone()
    assert row is not None
    stamp = metadata(Path(row["canonical_path"]), relative_path)
    connection.execute(
        """INSERT INTO workspace_external_observations(
            session_id, relative_path, generation, stamp, identity_json, sequence
        ) VALUES (?, ?, 1, ?, NULL, ?)
        ON CONFLICT(session_id, relative_path) DO UPDATE SET
            generation=generation+1, stamp=excluded.stamp,
            identity_json=NULL, sequence=excluded.sequence""",
        (session_id, relative_path, stamp, row["next_event_sequence"]),
    )


class _Deferred(Exception):
    """This path does not fit the current scan; a later scan checks it."""


def _identity(identity: FileIdentity) -> str:
    return _encode([str(identity.kind), identity.mode, identity.digest])


def _source(
    root: Path,
    relative: str,
    expected: str,
    remaining: int,
    capture: Callable[[bytes], None] | None = None,
) -> tuple[str | None, int]:
    """Hash only stable, bounded, screened regular source; everything else opaque."""
    with _parent(root, relative) as (parent, leaf):
        try:
            before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if expected != "absent":
                raise ValueError("path changed during scan") from None
            return _identity(ABSENT), 0
        if _stamp(before) != expected:
            raise ValueError("path changed during scan")
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            return None, 0
        if before.st_size > remaining:
            raise _Deferred
        descriptor = os.open(
            leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            if _stamp(os.fstat(descriptor)) != expected:
                raise ValueError("path changed during scan")
            chunks: list[bytes] = []
            count = 0
            while count <= before.st_size:
                chunk = os.read(descriptor, min(65_536, before.st_size + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                chunks.append(chunk)
            if count != before.st_size or _stamp(os.fstat(descriptor)) != expected:
                raise ValueError("path changed during scan")
            if _stamp(os.stat(leaf, dir_fd=parent, follow_symlinks=False)) != expected:
                raise ValueError("path changed during scan")
        finally:
            os.close(descriptor)
        content = b"".join(chunks)
        if contains_secret_material(content):
            return None, count
        if capture is not None:
            capture(content)
        mode = EXECUTABLE_MODE if before.st_mode & stat.S_IXUSR else REGULAR_MODE
        return _identity(
            FileIdentity(
                ObjectKind.REGULAR,
                mode,
                content_identity(ObjectKind.REGULAR, mode, content),
            )
        ), count


class ExternalWorkspaceScanner:
    def __init__(self, store: ControlStore) -> None:
        self.store = store
        self._last_session = ""

    def scan_active(self) -> int:
        with self.store.connection() as connection:
            sessions = connection.execute(
                "SELECT DISTINCT s.session_id FROM sessions s "
                "JOIN workspace_read_observations r ON r.session_id=s.session_id "
                "WHERE s.state IN ('active','disconnected') AND s.session_id>? "
                "ORDER BY s.session_id LIMIT 8",
                (self._last_session,),
            ).fetchall()
        if not sessions:
            self._last_session = ""
            return 0
        # Notices are advisory: no session's scan may stop the daemon loop.
        # Each tick is bounded and rotates; every model boundary scans its own session.
        deadline = time.monotonic() + MAX_SECONDS
        emitted = 0
        for row in sessions:
            self._last_session = str(row[0])
            with suppress(LlmCoordError, OSError, ValueError, sqlite3.Error):
                emitted += self.scan_session(self._last_session, background=True)
            if time.monotonic() >= deadline:
                break
        return emitted

    def scan_session(self, session_id: str, *, background: bool = False) -> int:
        from llm_cli.workspace.authority import managed_pin

        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT c.canonical_path FROM sessions s JOIN checkouts c ON "
                "c.checkout_id=s.checkout_id WHERE s.session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return 0
        root = Path(row[0])
        if (
            background
            and self.store.authority is not None
            and not self.store.authority.is_owned(root)
        ):
            return 0
        with managed_pin(self.store, root, activity=not background):
            return self._scan_session_owned(session_id)

    def _scan_session_owned(self, session_id: str) -> int:
        deadline = time.monotonic() + MAX_SECONDS

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise _incomplete("external scan exceeds its time budget")
            return value

        with self.store.connection() as connection:
            binding = connection.execute(
                "SELECT s.workspace_id, s.checkout_id, c.canonical_path, "
                "c.path_case_insensitive, "
                "w.workspace_revision, h.next_event_sequence FROM sessions s "
                "JOIN workspaces w ON w.workspace_id=s.workspace_id "
                "JOIN checkouts c ON c.checkout_id=s.checkout_id "
                "JOIN checkout_heads h ON h.checkout_id=s.checkout_id "
                "WHERE s.session_id=? AND s.state IN ('active','disconnected') "
                "AND w.kind='shared_checkout' AND w.mode='shared' AND w.state='active'",
                (session_id,),
            ).fetchone()
            if binding is None or self._pending(
                connection, str(binding["workspace_id"])
            ):
                return 0
            # Read observations last as long as the session; a long conversation
            # is covered for the files it read most recently.
            rows = connection.execute(
                "SELECT r.*, x.generation, x.stamp, x.identity_json, x.sequence "
                "FROM workspace_read_observations r LEFT JOIN "
                "workspace_external_observations x "
                "ON x.session_id=r.session_id AND x.relative_path=r.relative_path "
                "WHERE r.session_id=? ORDER BY r.observed_at DESC, r.relative_path "
                "LIMIT ?",
                (session_id, MAX_PATHS),
            ).fetchall()
            if not rows:
                return 0
            rows.sort(key=lambda row: str(row["relative_path"]))
            published = self._published(connection, binding, rows)
        root = Path(binding["canonical_path"])
        stamps = {
            str(row["relative_path"]): metadata(root, str(row["relative_path"]))
            for row in rows
        }
        excluded = excluded_paths(
            root,
            [path for path, stamp in stamps.items() if stamp != "unavailable"],
            remaining=remaining,
        )
        from llm_cli.knowledge import outbox
        from llm_cli.workspace.broker import store_candidate_content

        artifact_root = outbox.artifact_directory(self.store)
        with self.store.connection() as connection:
            project = artifact_root is not None and outbox.subscribed(
                connection, self.store, str(binding["checkout_id"])
            )
        artifact_hashes: dict[str, str] = {}
        results: list[tuple[sqlite3.Row, str, str | None, bool]] = []
        consumed = 0
        for row in rows:
            if time.monotonic() >= deadline:
                break  # The remaining paths keep their state for a later scan.
            path = str(row["relative_path"])
            stamp = stamps[path]
            # First scans always verify source identity against the actual read.
            if stamp == row["stamp"] and row["identity_json"] is not None:
                results.append((row, stamp, row["identity_json"], False))
                continue
            identity = None
            if path not in excluded and stamp != "unavailable":
                try:

                    def capture(content: bytes, relative: str = path) -> None:
                        # Optional projection failure must not suppress an
                        # ordinary outside-edit notice or its cursor progress.
                        if project and artifact_root is not None:
                            with suppress(OSError, ValueError, LlmCoordError):
                                artifact_hashes[relative] = store_candidate_content(
                                    artifact_root, content
                                )

                    if project:
                        identity, count = _source(
                            root, path, stamp, MAX_BYTES - consumed, capture=capture
                        )
                    else:
                        # Keep the ordinary source-reader call unchanged when
                        # optional projection is off.
                        identity, count = _source(
                            root, path, stamp, MAX_BYTES - consumed
                        )
                except _Deferred:
                    continue
                except (OSError, ValueError):
                    # Being written right now, as by an editor's save; a later
                    # scan sees its settled state.
                    continue
                consumed += count
            previous = row["identity_json"]
            if previous is None and path not in excluded:
                previous = _encode(
                    [row["observed_kind"], row["observed_mode"], row["observed_digest"]]
                )
            changed = (
                identity != previous
                if identity is not None and previous is not None
                else stamp != row["stamp"]
            )
            publication_key = (
                path.casefold() if binding["path_case_insensitive"] else path
            )
            if (
                identity is not None
                and publication_key in published
                and published[publication_key][0] > int(row["sequence"] or 0)
                and published[publication_key][1] == identity
            ):
                changed = False
            # Opaque stamps get a non-digest sentinel to enable the cheap path.
            results.append((row, stamp, identity or "opaque", changed))
        emitted = 0
        with self.store.connection() as connection, immediate_transaction(connection):
            current = connection.execute(
                "SELECT workspace_revision FROM workspaces WHERE workspace_id=?",
                (binding["workspace_id"],),
            ).fetchone()
            if (
                current is None
                or current[0] != binding["workspace_revision"]
                or self._pending(connection, str(binding["workspace_id"]))
            ):
                raise _incomplete(
                    "publication changed during external scan; retry context refresh"
                )
            for row, stamp, identity, changed in results:
                found = connection.execute(
                    "SELECT generation, stamp, identity_json, sequence FROM "
                    "workspace_external_observations WHERE session_id=? AND "
                    "relative_path=?",
                    (session_id, row["relative_path"]),
                ).fetchone()
                if found is not None and tuple(found) != (
                    row["generation"],
                    row["stamp"],
                    row["identity_json"],
                    row["sequence"],
                ):
                    # A fresh real read or another scan won the observation race.
                    continue
                if found is None and row["generation"] is not None:
                    continue
                sequence = int(binding["next_event_sequence"])
                if changed:
                    event = self.store.append_checkout_event(
                        connection,
                        checkout_id=str(binding["checkout_id"]),
                        event_type=EVENT_TYPE,
                        caused_by_session_id=None,
                        payload={
                            "path": row["relative_path"],
                            "affected_session_id": session_id,
                        },
                        now=int(time.time() * 1_000),
                    )
                    sequence = event.sequence
                    outbox.enqueue_external(
                        connection,
                        self.store,
                        checkout_id=str(binding["checkout_id"]),
                        path=str(row["relative_path"]),
                        identity=identity,
                        content_hash=artifact_hashes.get(str(row["relative_path"])),
                        sequence=sequence,
                        now=int(time.time() * 1_000),
                    )
                    emitted += 1
                connection.execute(
                    "INSERT INTO workspace_external_observations(session_id, "
                    "relative_path, generation, stamp, identity_json, sequence) "
                    "VALUES (?,?,0,?,?,?) ON CONFLICT(session_id, relative_path) DO "
                    "UPDATE SET stamp=excluded.stamp, "
                    "identity_json=excluded.identity_json, sequence=excluded.sequence",
                    (session_id, row["relative_path"], stamp, identity, sequence),
                )
        return emitted

    @staticmethod
    def _pending(connection: sqlite3.Connection, workspace: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM workspace_batches WHERE workspace_id=? AND state "
                "IN ('applying','operator_attention') UNION ALL SELECT 1 FROM "
                "workspace_publications WHERE workspace_id=? AND "
                "operation_state='prepared' LIMIT 1",
                (workspace, workspace),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _published(
        connection: sqlite3.Connection, binding: sqlite3.Row, rows: list[sqlite3.Row]
    ) -> dict[str, tuple[int, str]]:
        sequence = min(int(row["sequence"] or 0) for row in rows)
        results: dict[str, tuple[int, str]] = {}

        def key(path: str) -> str:
            return path.casefold() if binding["path_case_insensitive"] else path

        batches = connection.execute(
            "SELECT checkout_event_sequence, length(CAST(request_json AS BLOB)), "
            "CASE WHEN length(CAST(request_json AS BLOB))<=1048576 "
            "THEN request_json ELSE NULL END FROM workspace_batches "
            "WHERE workspace_id=? AND state='published' AND "
            "checkout_event_sequence>? ORDER BY checkout_event_sequence LIMIT 1001",
            (binding["workspace_id"], sequence),
        )
        consumed = 0
        for count, row in enumerate(batches, 1):
            consumed += int(row[1])
            if count > 1_000 or consumed > MAX_BYTES or row[2] is None:
                raise _incomplete("publication history exceeds external scan budget")
            try:
                value = json.loads(row[2])
                files = value["files"]
                if not isinstance(files, list) or not 1 <= len(files) <= 50:
                    raise ValueError("invalid publication paths")
                for item in files:
                    result = item["result"]
                    results[key(item["relative_path"])] = (
                        int(row[0]),
                        _encode([result["kind"], result["mode"], result["digest"]]),
                    )
            except (ValueError, TypeError, KeyError) as exc:
                raise _incomplete(
                    "publication metadata is unavailable for external scan"
                ) from exc
        singles = connection.execute(
            "SELECT c.relative_path, c.result_kind, c.result_mode, "
            "c.result_digest, p.checkout_event_sequence FROM "
            "workspace_publications p JOIN workspace_candidates c ON "
            "c.candidate_id=p.candidate_id WHERE p.workspace_id=? AND "
            "p.operation_state='confirmed' AND p.checkout_event_sequence>? "
            "ORDER BY p.checkout_event_sequence LIMIT 1001",
            (binding["workspace_id"], sequence),
        ).fetchall()
        if len(singles) > 1_000:
            raise _incomplete("publication history exceeds external scan budget")
        for row in singles:
            path = key(str(row[0]))
            if path not in results or results[path][0] < row[4]:
                results[path] = (row[4], _encode([row[1], row[2], row[3]]))
        return results


def _incomplete(message: str) -> LlmCoordError:
    return LlmCoordError(ErrorCode.CONTEXT_TOO_LARGE, message)
