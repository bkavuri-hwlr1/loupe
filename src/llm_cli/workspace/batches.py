"""Bounded, durable shared-checkout publication for regular UTF-8 files.

The caller shares ``lock`` with every cooperative checkout read and write.
Journal recovery rolls forward from recorded bases; it never overwrites a
third state or rolls back the user's files. Existing real parent directories
are required. Multi-file atomicity applies only to callers honoring the lock.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from llm_cli.coordination.scopes import (
    casefold_aliases,
    normalize_changed_path,
    normalize_scopes,
    uncovered_paths,
)
from llm_cli.storage.connection import immediate_transaction
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.broker import (
    MAX_SHARED_TEXT_BYTES,
    WorkspaceBrokerError,
    atomic_replace_regular_file,
    candidate_content_hash,
    load_candidate_content,
    store_candidate_content,
)
from llm_cli.workspace.identity import (
    ABSENT,
    DIRECTORY_MODE,
    EXECUTABLE_MODE,
    REGULAR_MODE,
    FileIdentity,
    ObjectKind,
    content_identity,
    read_identified_path,
)

MAX_BATCH_FILES = 50
MAX_BATCH_BYTES = 4 * 1024 * 1024
BatchState = Literal["published", "diverged", "operator_attention"]


@dataclass(frozen=True, slots=True)
class BatchFile:
    relative_path: str
    base: FileIdentity
    content: bytes | None
    mode: str
    original: bytes | None = None
    permissions: int | None = None


@dataclass(frozen=True, slots=True)
class BatchPublication:
    batch_id: str
    state: BatchState
    workspace_revision: int | None
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _JournalFile:
    relative_path: str
    base: FileIdentity
    result: FileIdentity
    content_hash: str | None


def _identity_json(identity: FileIdentity) -> dict[str, object]:
    value: dict[str, object] = {
        "kind": str(identity.kind),
        "mode": identity.mode,
        "digest": identity.digest,
        "size": identity.size,
    }
    if identity.permissions is not None:
        value["permissions"] = identity.permissions
    return value


def _validate_base(base: FileIdentity) -> None:
    if not isinstance(base, FileIdentity):
        raise WorkspaceBrokerError("batch base must be a FileIdentity")
    if base.permissions is not None and (
        base.kind is not ObjectKind.REGULAR
        or type(base.permissions) is not int
        or not 0 <= base.permissions <= 0o777
    ):
        raise WorkspaceBrokerError("batch filesystem permissions are invalid")
    if base.kind is ObjectKind.ABSENT:
        if base != ABSENT:
            raise WorkspaceBrokerError("batch absent base identity is invalid")
        return
    if base.kind is ObjectKind.DIRECTORY_MARKER:
        if base != directory_identity():
            raise WorkspaceBrokerError("invalid directory identity")
        return
    if (
        base.kind is not ObjectKind.REGULAR
        or base.mode not in {REGULAR_MODE, EXECUTABLE_MODE}
        or not isinstance(base.digest, str)
        or len(base.digest) != 64
        or any(char not in "0123456789abcdef" for char in base.digest)
        or type(base.size) is not int
        or base.size < 0
        or base.size > MAX_SHARED_TEXT_BYTES
    ):
        raise WorkspaceBrokerError("batch supports bounded regular or absent bases")


def _identity_from_json(value: object) -> FileIdentity:
    if not isinstance(value, dict) or set(value) not in (
        {"kind", "mode", "digest", "size"},
        {"kind", "mode", "digest", "size", "permissions"},
    ):
        raise WorkspaceBrokerError("stored batch identity is invalid")
    if (
        not isinstance(value["kind"], str)
        or not isinstance(value["mode"], str)
        or not isinstance(value["digest"], str)
        or type(value["size"]) is not int
    ):
        raise WorkspaceBrokerError("stored batch identity fields are invalid")
    identity = FileIdentity(
        ObjectKind(value["kind"]),
        value["mode"],
        value["digest"],
        value["size"],
        value.get("permissions"),
    )
    _validate_base(identity)
    return identity


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def candidate_target(root: Path, relative_path: str) -> Path:
    """Validate existing parents while allowing journaled missing directories."""
    relative = normalize_changed_path(relative_path)
    target = root / relative
    parent = root
    for component in Path(relative).parts[:-1]:
        parent /= component
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise WorkspaceBrokerError("candidate parent is not a real directory")
        if (parent / ".git").exists() or (parent / ".git").is_symlink():
            raise WorkspaceBrokerError("candidate cannot enter a nested repository")
    return target


def directory_identity() -> FileIdentity:
    return FileIdentity(
        ObjectKind.DIRECTORY_MARKER,
        DIRECTORY_MODE,
        content_identity(ObjectKind.DIRECTORY_MARKER, DIRECTORY_MODE),
    )


def result_identity(item: BatchFile) -> FileIdentity:
    if item.content is None:
        return ABSENT
    if item.mode == DIRECTORY_MODE:
        return directory_identity()
    permissions = (
        item.permissions if item.permissions is not None else item.base.permissions
    )
    if permissions is not None:
        if type(permissions) is not int or not 0 <= permissions <= 0o777:
            raise WorkspaceBrokerError("batch filesystem permissions are invalid")
        permissions = (
            permissions | 0o100
            if item.mode == EXECUTABLE_MODE
            else permissions & ~0o100
        )
    return FileIdentity(
        ObjectKind.REGULAR,
        item.mode,
        content_identity(ObjectKind.REGULAR, item.mode, item.content),
        len(item.content),
        permissions,
    )


def _matches_snapshot(
    current: FileIdentity, expected: FileIdentity, *, result: bool = False
) -> bool:
    if current != expected:
        return False
    if expected.permissions is None:
        # Existing journals predate filesystem permission metadata. The
        # atomic writer still preserves live permissions, or creates privately.
        return True
    if current.permissions is None:
        return False
    if result:
        # A user may tighten access after publication. Never undo that on
        # recovery merely to match the originally proposed permission mask.
        return current.permissions & ~expected.permissions == 0
    return current.permissions == expected.permissions


def _target(root: Path, relative_path: str) -> Path:
    target = candidate_target(root, relative_path)
    parent = target.parent
    while parent != root:
        if (parent / ".git").exists() or (parent / ".git").is_symlink():
            raise WorkspaceBrokerError("batch cannot enter a nested repository")
        parent = parent.parent
    return target


def _observe(target: Path) -> FileIdentity:
    identity, body = read_identified_path(target, max_bytes=MAX_SHARED_TEXT_BYTES)
    if body is not None:
        body.decode("utf-8")
    return identity


def _request(
    session_id: str,
    files: Sequence[BatchFile],
    scopes: tuple[str, ...],
    case_insensitive_filesystem: bool,
) -> str:
    if not files or len(files) > MAX_BATCH_FILES:
        raise WorkspaceBrokerError(f"batch must contain 1 to {MAX_BATCH_FILES} files")
    if not session_id or not isinstance(case_insensitive_filesystem, bool):
        raise WorkspaceBrokerError("batch session or case policy is invalid")
    normalize_scopes(scopes, case_insensitive_filesystem=case_insensitive_filesystem)
    paths: list[str] = []
    encoded: list[dict[str, object]] = []
    total = 0
    for item in files:
        if not isinstance(item, BatchFile):
            raise WorkspaceBrokerError("batch files must be BatchFile values")
        path = normalize_changed_path(item.relative_path)
        if path != item.relative_path:
            raise WorkspaceBrokerError("batch paths must use their canonical spelling")
        if path in paths:
            raise WorkspaceBrokerError("batch paths must be unique")
        paths.append(path)
        _validate_base(item.base)
        if item.mode not in {REGULAR_MODE, EXECUTABLE_MODE, DIRECTORY_MODE, ""}:
            raise WorkspaceBrokerError(
                "batch result mode must be regular or executable"
            )
        if item.content is not None and not isinstance(item.content, bytes):
            raise WorkspaceBrokerError("batch content must be bytes")
        if item.original is not None and (
            item.base.kind is not ObjectKind.REGULAR
            or len(item.original) != item.base.size
            or content_identity(ObjectKind.REGULAR, item.base.mode, item.original)
            != item.base.digest
        ):
            raise WorkspaceBrokerError("original bytes do not match the observed base")
        body = item.content or b""
        if (item.content is None) != (item.mode == ""):
            raise WorkspaceBrokerError("deletion requires absent result mode")
        if item.mode == DIRECTORY_MODE and (body or not item.base.absent):
            raise WorkspaceBrokerError(
                "only creation of absent directories is supported"
            )
        if len(body) > MAX_SHARED_TEXT_BYTES:
            raise WorkspaceBrokerError("batch file exceeds the shared text byte limit")
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceBrokerError("batch content must be UTF-8 text") from exc
        total += len(body) + len(item.original or b"")
        if total > MAX_BATCH_BYTES:
            raise WorkspaceBrokerError("batch exceeds the 4 MiB aggregate byte limit")
        result = result_identity(item)
        _validate_base(result)
        encoded.append(
            {
                "relative_path": path,
                "base": _identity_json(item.base),
                "result": _identity_json(result),
                "content_hash": candidate_content_hash(body)
                if item.content is not None
                else None,
                "original_hash": candidate_content_hash(item.original)
                if item.original is not None
                else None,
            }
        )
    if case_insensitive_filesystem and casefold_aliases(paths):
        raise WorkspaceBrokerError("batch paths contain case-folded aliases")
    if uncovered_paths(
        scopes, paths, case_insensitive_filesystem=case_insensitive_filesystem
    ):
        raise WorkspaceBrokerError("batch paths are outside the supplied scopes")
    return _encode(
        {
            "session_id": session_id,
            "scopes": list(scopes),
            "case_insensitive_filesystem": case_insensitive_filesystem,
            "files": encoded,
        }
    )


def _journal_files(request_json: str) -> tuple[_JournalFile, ...]:
    value = json.loads(request_json)
    if not isinstance(value, dict) or not isinstance(value.get("files"), list):
        raise WorkspaceBrokerError("stored batch request is invalid")
    entries = value["files"]
    if not 1 <= len(entries) <= MAX_BATCH_FILES:
        raise WorkspaceBrokerError("stored batch file count is invalid")
    files: list[_JournalFile] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise WorkspaceBrokerError("stored batch file is invalid")
        path = entry.get("relative_path")
        digest = entry.get("content_hash")
        if (
            not isinstance(path, str)
            or normalize_changed_path(path) != path
            or (
                digest is not None
                and (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)
                )
            )
        ):
            raise WorkspaceBrokerError("stored batch path or content hash is invalid")
        result = _identity_from_json(entry.get("result"))
        if (digest is None) != result.absent:
            raise WorkspaceBrokerError("stored batch result body is inconsistent")
        files.append(
            _JournalFile(path, _identity_from_json(entry.get("base")), result, digest)
        )
    return tuple(files)


class SharedBatchPublisher:
    """Publish one immutable batch while holding the daemon's shared barrier."""

    def __init__(
        self, store: ControlStore, candidate_dir: Path, lock: threading.RLock
    ) -> None:
        self.store = store
        self.candidate_dir = candidate_dir
        self.lock = lock

    def get(self, batch_id: str) -> BatchPublication | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            return self._publication(row) if row is not None else None

    @staticmethod
    def _publication(row: sqlite3.Row) -> BatchPublication:
        state: BatchState = "operator_attention"
        if row["state"] == "published":
            state = "published"
        elif row["state"] == "diverged":
            state = "diverged"
        return BatchPublication(
            str(row["batch_id"]),
            state,
            int(row["workspace_revision"])
            if row["workspace_revision"] is not None
            else None,
            tuple(
                item.relative_path for item in _journal_files(str(row["request_json"]))
            ),
        )

    def blocks_workspace(self, workspace_id: str) -> bool:
        with self.store.connection() as connection:
            return self._blocked(connection, workspace_id)

    @staticmethod
    def _blocked(connection: sqlite3.Connection, workspace_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM workspace_batches WHERE workspace_id = ? "
                "AND state IN ('applying', 'operator_attention') LIMIT 1",
                (workspace_id,),
            ).fetchone()
            is not None
        )

    def publish(
        self,
        batch_id: str,
        session_id: str,
        files: Sequence[BatchFile],
        scopes: tuple[str, ...],
        case_insensitive_filesystem: bool = False,
    ) -> BatchPublication:
        if not isinstance(batch_id, str) or not batch_id or len(batch_id) > 256:
            raise WorkspaceBrokerError("batch ID must contain 1 to 256 characters")
        request_json = _request(session_id, files, scopes, case_insensitive_filesystem)
        with self.lock:
            with self.store.connection() as connection:
                existing = connection.execute(
                    "SELECT * FROM workspace_batches WHERE batch_id = ?", (batch_id,)
                ).fetchone()
                if existing is not None:
                    if str(existing["request_json"]) != request_json:
                        raise WorkspaceBrokerError(
                            "batch ID is bound to another request"
                        )
                    # Explicit recovery owns interrupted writes; retries observe.
                    return self._publication(existing)
            for item in files:
                if item.content is not None:
                    store_candidate_content(self.candidate_dir, item.content)
                if item.original is not None:
                    store_candidate_content(self.candidate_dir, item.original)
            with (
                self.store.connection() as connection,
                immediate_transaction(connection),
            ):
                binding = self._binding(connection, session_id, require_active=True)
                workspace_id = str(binding["workspace_id"])
                if self._blocked(connection, workspace_id):
                    raise WorkspaceBrokerError(
                        "workspace has an unresolved batch journal"
                    )
                manual = connection.execute(
                    "SELECT 1 FROM workspace_publications WHERE workspace_id = ? "
                    "AND operation_state IN ('prepared', 'operator_attention') LIMIT 1",
                    (workspace_id,),
                ).fetchone()
                if manual is not None:
                    raise WorkspaceBrokerError(
                        "workspace has an unresolved file publication"
                    )
                root = Path(str(binding["canonical_path"]))
                # The checkout's durable case policy cannot be weakened by the caller.
                _request(
                    session_id,
                    files,
                    scopes,
                    case_insensitive_filesystem
                    or bool(binding["path_case_insensitive"]),
                )
                created_dirs = {
                    item.relative_path for item in files if item.mode == DIRECTORY_MODE
                }
                for item in files:
                    for parent in Path(item.relative_path).parents:
                        if str(parent) == ".":
                            continue
                        path = parent.as_posix()
                        if (
                            not _target(root, path).is_dir()
                            and path not in created_dirs
                        ):
                            raise WorkspaceBrokerError(
                                "missing parent requires a directory operation"
                            )
                    if (
                        item.base.kind is ObjectKind.DIRECTORY_MARKER
                        and item.content is None
                    ):
                        deleted = {f.relative_path for f in files if f.content is None}
                        if any(
                            p.relative_to(root).as_posix() not in deleted
                            for p in _target(root, item.relative_path).rglob("*")
                        ):
                            raise WorkspaceBrokerError(
                                "directory contains changes from another writer"
                            )
                targets = {
                    item.relative_path: _target(root, item.relative_path)
                    for item in files
                }
                observed: dict[str, object] = {}
                state = "applying"
                for item in files:
                    try:
                        current = _observe(targets[item.relative_path])
                        observed[item.relative_path] = _identity_json(current)
                        if not _matches_snapshot(current, item.base):
                            state = "diverged"
                    except (OSError, ValueError) as exc:
                        observed[item.relative_path] = {"error": str(exc)}
                        state = "diverged"
                now = time.time_ns() // 1_000_000
                connection.execute(
                    """
                    INSERT INTO workspace_batches(
                        batch_id, session_id, workspace_id, checkout_id, canonical_path,
                        git_common_dir, request_json, observations_json, state,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        session_id,
                        workspace_id,
                        binding["checkout_id"],
                        binding["canonical_path"],
                        binding["git_common_dir"],
                        request_json,
                        _encode(observed),
                        state,
                        now,
                        now,
                    ),
                )
            if state == "applying":
                return self._apply(batch_id)
            publication = self.get(batch_id)
            assert publication is not None
            return publication

    @staticmethod
    def _binding(
        connection: sqlite3.Connection, session_id: str, *, require_active: bool
    ) -> sqlite3.Row:
        binding = connection.execute(
            """
            SELECT workspaces.*, checkouts.git_common_dir,
                checkouts.path_case_insensitive,
                checkouts.canonical_path AS checkout_path,
                sessions.state AS session_state, sessions.workspace_mode,
                sessions.checkout_id AS session_checkout_id
            FROM sessions JOIN workspaces
                ON workspaces.workspace_id = sessions.workspace_id
            JOIN checkouts ON checkouts.checkout_id = workspaces.checkout_id
            WHERE sessions.session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if (
            binding is None
            or (
                require_active
                and binding["session_state"] not in {"active", "disconnected"}
            )
            or binding["state"] != "active"
            or binding["kind"] != "shared_checkout"
            or binding["mode"] != "shared"
            or binding["workspace_mode"] != "shared"
            or binding["checkout_id"] != binding["session_checkout_id"]
            or binding["canonical_path"] != binding["checkout_path"]
        ):
            raise WorkspaceBrokerError(
                "batch requires an active shared workspace session"
            )
        root = Path(str(binding["canonical_path"]))
        if str(root.resolve(strict=True)) != str(root) or not root.is_dir():
            raise WorkspaceBrokerError("registered workspace root changed")
        assert isinstance(binding, sqlite3.Row)
        return binding

    def recover(self, batch_id: str | None = None) -> list[BatchPublication]:
        """Explicitly roll forward pending journals whose paths are still recognized."""

        with self.lock:
            with self.store.connection() as connection:
                rows = connection.execute(
                    "SELECT batch_id FROM workspace_batches "
                    "WHERE state IN ('applying', 'operator_attention') "
                    "AND (? IS NULL OR batch_id = ?) ORDER BY created_at, batch_id",
                    (batch_id, batch_id),
                ).fetchall()
            return [self._apply(str(row["batch_id"])) for row in rows]

    def _apply(self, batch_id: str) -> BatchPublication:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            assert row is not None
            if row["state"] not in {"applying", "operator_attention"}:
                return self._publication(row)
        observations: dict[str, object] = {}
        try:
            with self.store.connection() as connection:
                binding = self._binding(
                    connection, str(row["session_id"]), require_active=False
                )
                for key in (
                    "workspace_id",
                    "checkout_id",
                    "canonical_path",
                    "git_common_dir",
                ):
                    if binding[key] != row[key]:
                        raise WorkspaceBrokerError("batch workspace binding changed")
            files = _journal_files(str(row["request_json"]))
            root = Path(str(row["canonical_path"]))
            # Load and validate every body before writing any path, including paths
            # that already match the result after a crash before database commit.
            bodies: dict[str, bytes] = {}
            for item in files:
                body = (
                    load_candidate_content(self.candidate_dir, item.content_hash)
                    if item.content_hash is not None
                    else b""
                )
                body.decode("utf-8")
                if item.result.kind is ObjectKind.REGULAR and (
                    len(body) != item.result.size
                    or content_identity(ObjectKind.REGULAR, item.result.mode, body)
                    != item.result.digest
                ):
                    raise WorkspaceBrokerError(
                        "stored batch result does not match its body"
                    )
                bodies[item.relative_path] = body
                current = _observe(_target(root, item.relative_path))
                observations[item.relative_path] = _identity_json(current)
                if not (
                    _matches_snapshot(current, item.base)
                    or _matches_snapshot(current, item.result, result=True)
                ):
                    raise WorkspaceBrokerError(
                        "batch recovery found an unrecognized path state"
                    )
            ordered = sorted(
                files,
                key=lambda item: (
                    0
                    if item.result.kind is ObjectKind.DIRECTORY_MARKER
                    else 2
                    if item.base.kind is ObjectKind.DIRECTORY_MARKER
                    else 1,
                    -len(Path(item.relative_path).parts)
                    if item.result.absent
                    else len(Path(item.relative_path).parts),
                    item.relative_path,
                ),
            )
            for ordinal, item in enumerate(ordered):
                current = _observe(_target(root, item.relative_path))
                observations[item.relative_path] = _identity_json(current)
                if _matches_snapshot(current, item.result, result=True):
                    continue
                if not _matches_snapshot(current, item.base):
                    raise WorkspaceBrokerError("batch path changed during publication")
                # IDs are caller-controlled labels. Hash them before constructing
                # an administrative staging filename.
                staging_id = hashlib.sha256(
                    f"batch:{batch_id}:{ordinal}".encode()
                ).hexdigest()
                target = _target(root, item.relative_path)
                if item.result.kind is ObjectKind.DIRECTORY_MARKER:
                    target.mkdir(mode=0o700)
                    _sync_parent(target)
                    continue
                if item.result.absent:
                    if item.base.kind is ObjectKind.DIRECTORY_MARKER:
                        target.rmdir()
                    else:
                        target.unlink()
                    _sync_parent(target)
                    continue
                atomic_replace_regular_file(
                    checkout_root=root,
                    git_common_dir=Path(str(row["git_common_dir"])),
                    relative_path=item.relative_path,
                    content=bodies[item.relative_path],
                    mode=item.result.mode,
                    publication_id=staging_id,
                    permissions=item.result.permissions,
                )
            with (
                self.store.connection() as connection,
                immediate_transaction(connection),
            ):
                for item in files:
                    current = _observe(_target(root, item.relative_path))
                    observations[item.relative_path] = _identity_json(current)
                    if not _matches_snapshot(current, item.result, result=True):
                        raise WorkspaceBrokerError(
                            "batch materialized result failed verification"
                        )
                self._confirm(connection, row, files)
        except (OSError, ValueError) as exc:
            observations["error"] = str(exc)
            with (
                self.store.connection() as connection,
                immediate_transaction(connection),
            ):
                connection.execute(
                    "UPDATE workspace_batches SET state = 'operator_attention', "
                    "observations_json = ?, updated_at = ? WHERE batch_id = ? "
                    "AND state IN ('applying', 'operator_attention')",
                    (_encode(observations), time.time_ns() // 1_000_000, batch_id),
                )
        publication = self.get(batch_id)
        assert publication is not None
        return publication

    def _confirm(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        files: tuple[_JournalFile, ...],
    ) -> None:
        current = connection.execute(
            "SELECT state FROM workspace_batches WHERE batch_id = ?", (row["batch_id"],)
        ).fetchone()
        assert current is not None
        if current["state"] == "published":
            return
        workspace = connection.execute(
            "SELECT workspace_revision FROM workspaces WHERE workspace_id = ?",
            (row["workspace_id"],),
        ).fetchone()
        assert workspace is not None
        revision = int(workspace["workspace_revision"]) + 1
        now = time.time_ns() // 1_000_000
        connection.execute(
            "UPDATE workspaces SET workspace_revision = ?, updated_at = ? "
            "WHERE workspace_id = ?",
            (revision, now, row["workspace_id"]),
        )
        event = self.store.append_checkout_event(
            connection,
            checkout_id=str(row["checkout_id"]),
            event_type="workspace.batch_published",
            caused_by_session_id=str(row["session_id"]),
            payload={
                "batch_id": str(row["batch_id"]),
                "workspace_id": str(row["workspace_id"]),
                "workspace_revision": revision,
                "paths": [item.relative_path for item in files],
            },
            now=now,
        )
        connection.execute(
            "UPDATE workspace_batches SET state = 'published', workspace_revision = ?, "
            "checkout_event_sequence = ?, updated_at = ? WHERE batch_id = ?",
            (revision, event.sequence, now, row["batch_id"]),
        )


def _sync_parent(target: Path) -> None:
    descriptor = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
