"""Crash-recoverable incoming batches and source-only isolated publication.

An isolated worktree's detached HEAD is not its source baseline. The accepted
manifest follows the checkout event stream; local bytes may differ from it.
Incoming conflicts retain those bytes and the exact incoming artifact. Every
filesystem transition is journaled before writing, and recovery recognizes only
its frozen before/after states.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm_cli.agent.source_policy import ensure_safe_content, excluded_paths
from llm_cli.coordination.scopes import normalize_changed_path
from llm_cli.errors import LlmCoordError
from llm_cli.storage.connection import immediate_transaction
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.batches import (
    BatchFile,
    BatchPublication,
    SharedBatchPublisher,
    _identity_from_json,
    _identity_json,
    _journal_files,
    _request,
    candidate_target,
    result_identity,
)
from llm_cli.workspace.broker import (
    MAX_SHARED_TEXT_BYTES,
    WorkspaceBrokerError,
    _open_publication_parent,
    atomic_delete_file,
    atomic_replace_regular_file,
    atomic_replace_symlink,
    load_candidate_content,
    read_candidate_path,
    store_candidate_content,
)
from llm_cli.workspace.identity import (
    ABSENT,
    DIRECTORY_MODE,
    EXECUTABLE_MODE,
    REGULAR_MODE,
    SYMLINK_MODE,
    FileIdentity,
    ObjectKind,
)
from llm_cli.workspace.merge import has_conflict_markers, merge_text

Manifest = dict[str, dict[str, Any]]


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _entry(
    identity: FileIdentity, content_hash: str | None, binary: bool = False
) -> dict[str, Any]:
    return {
        **_identity_json(identity),
        "content_hash": content_hash,
        **({"binary": True} if binary else {}),
    }


def _identity(entry: Mapping[str, Any] | None) -> FileIdentity:
    if entry is None:
        return ABSENT
    # Initial source manifests can include large unchanged files. Batch limits
    # apply to changed paths, not to merely retaining their accepted identity.
    identity = FileIdentity(
        ObjectKind(entry["kind"]),
        entry["mode"],
        entry["digest"],
        entry["size"],
        entry.get("permissions"),
    )
    modes = {
        ObjectKind.ABSENT: {""},
        ObjectKind.REGULAR: {REGULAR_MODE, EXECUTABLE_MODE},
        ObjectKind.SYMLINK: {SYMLINK_MODE},
        ObjectKind.DIRECTORY_MARKER: {DIRECTORY_MODE},
    }
    if (
        identity.mode not in modes.get(identity.kind, set())
        or type(identity.size) is not int
        or not 0 <= identity.size <= 256 * 1024 * 1024
        or len(identity.digest) != 64
        or any(c not in "0123456789abcdef" for c in identity.digest)
    ):
        raise WorkspaceBrokerError("isolated manifest identity is invalid")
    if identity.absent and identity != ABSENT:
        raise WorkspaceBrokerError("isolated absent identity is invalid")
    return identity


def _binary(body: bytes | None) -> bool:
    if body is None:
        return False
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return b"\0" in body


@dataclass(frozen=True)
class SyncResult:
    applied: tuple[str, ...] = ()
    merged: tuple[str, ...] = ()
    conflicted: tuple[str, ...] = ()
    sequence: int = 0

    def note(self) -> str:
        notices = []
        if self.applied:
            notices.append("Received shared changes: " + ", ".join(self.applied))
        if self.merged:
            notices.append(
                "Merged shared changes with local edits: " + ", ".join(self.merged)
            )
        if self.conflicted:
            notices.append(
                "Shared changes conflict with preserved isolated files: "
                + ", ".join(self.conflicted)
                + ". Read and resolve these paths before publication."
            )
        return "\n".join(notices)


class IsolatedSynchronizer:
    def __init__(
        self,
        store: ControlStore,
        publisher: SharedBatchPublisher,
        capture: Callable[[Path, Sequence[str]], Manifest],
        validate: Callable[[str], object] | None = None,
    ) -> None:
        self.store = store
        self.publisher = publisher
        self.capture = capture
        self.validate = validate

    def register(
        self,
        workspace_id: str,
        source_manifest: Manifest,
        source_sequence: int,
        runtime_paths: Sequence[str],
    ) -> None:
        """Pin the exact creation snapshot once, never replace a live baseline."""
        with self.publisher.lock, self.store.connection() as con:
            con.execute(
                "INSERT OR IGNORE INTO isolated_sync_state "
                "(workspace_id,manifest_json,source_sequence,"
                "runtime_paths_json,generation) "
                "VALUES (?,?,?,?,0)",
                (
                    workspace_id,
                    _encode(source_manifest),
                    source_sequence,
                    _encode(list(runtime_paths)),
                ),
            )

    def _state(self, workspace_id: str) -> dict[str, Any]:
        if self.validate is not None:
            self.validate(workspace_id)
        with self.store.connection() as con:
            row = con.execute(
                "SELECT s.*,w.canonical_path,w.checkout_id,w.source_workspace_id,"
                "c.git_common_dir,c.canonical_path AS source_path "
                "FROM isolated_sync_state s "
                "JOIN workspaces w USING(workspace_id) "
                "JOIN checkouts c USING(checkout_id) "
                "WHERE s.workspace_id=? AND w.kind='linked_worktree' "
                "AND w.mode='isolated' AND w.state='active'",
                (workspace_id,),
            ).fetchone()
        if row is None:
            raise WorkspaceBrokerError(
                "isolated synchronization binding is unavailable"
            )
        state = dict(row)
        root = Path(state["canonical_path"])
        if root.resolve(strict=True) != root or not root.is_dir():
            raise WorkspaceBrokerError("isolated workspace root changed")
        return state

    def _body(self, entry: Mapping[str, Any] | None) -> bytes | None:
        if entry is None or _identity(entry).absent:
            return None
        body_hash = entry.get("content_hash")
        if not isinstance(body_hash, str):
            raise WorkspaceBrokerError("isolated source artifact is unavailable")
        body = load_candidate_content(self.publisher.candidate_dir, body_hash)
        expected = _identity(entry)
        actual = result_identity(BatchFile("body", ABSENT, body, expected.mode))
        if actual != expected:
            raise WorkspaceBrokerError("isolated source artifact identity changed")
        ensure_safe_content(body)
        return body

    @staticmethod
    def _runtime(path: str, state: Mapping[str, Any]) -> bool:
        return any(
            path == p or path.startswith(p + "/")
            for p in json.loads(state["runtime_paths_json"])
        )

    def synchronize(self, workspace_id: str) -> SyncResult:
        """Apply complete ordered incoming batches at a caller-owned safe point."""
        with self.publisher.lock:
            self.recover(workspace_id)
            state = self._state(workspace_id)
            applied: list[str] = []
            merged: list[str] = []
            conflicted: list[str] = []
            with self.store.connection() as con:
                rows = con.execute(
                    "SELECT sequence,event_type,payload_json FROM checkout_events "
                    "WHERE checkout_id=? AND sequence>? ORDER BY sequence LIMIT 1001",
                    (state["checkout_id"], state["source_sequence"]),
                ).fetchall()
            if len(rows) > 1000:
                raise WorkspaceBrokerError(
                    "isolated synchronization exceeds 1000 pending events"
                )
            sequence = int(state["source_sequence"])
            for row in rows:
                if int(row["sequence"]) != sequence + 1:
                    raise WorkspaceBrokerError("isolated incoming history has a gap")
                sequence += 1
                if row["event_type"] != "workspace.batch_published":
                    # Non-batch events carry no reconstructible source bodies.
                    # Exact publication comparison still catches outside writes.
                    with self.store.connection() as con:
                        con.execute(
                            "UPDATE isolated_sync_state SET "
                            "source_sequence=? WHERE workspace_id=?",
                            (sequence, workspace_id),
                        )
                    continue
                payload = json.loads(row["payload_json"])
                with self.store.connection() as con:
                    batch = con.execute(
                        "SELECT * FROM workspace_batches "
                        "WHERE batch_id=? AND checkout_id=? "
                        "AND state='published' AND checkout_event_sequence=?",
                        (payload["batch_id"], state["checkout_id"], sequence),
                    ).fetchone()
                if batch is None:
                    raise WorkspaceBrokerError("incoming batch artifact is unavailable")
                report = self._prepare(
                    workspace_id, sequence, str(batch["request_json"])
                )
                self.recover(workspace_id)
                applied.extend(report.applied)
                merged.extend(report.merged)
                conflicted.extend(report.conflicted)
            report = self._refresh_primary(workspace_id)
            applied.extend(report.applied)
            merged.extend(report.merged)
            conflicted.extend(report.conflicted)
            return SyncResult(
                tuple(applied), tuple(merged), tuple(conflicted), sequence
            )

    def _refresh_primary(self, workspace_id: str) -> SyncResult:
        """Reconcile exact source bytes changed without a reconstructible batch."""
        state = self._state(workspace_id)
        baseline: Manifest = json.loads(state["manifest_json"])
        primary = self.capture(
            Path(state["source_path"]), tuple(json.loads(state["runtime_paths_json"]))
        )
        files = []
        for path in sorted(baseline.keys() | primary.keys()):
            if self._runtime(path, state):
                continue
            before, after = baseline.get(path), primary.get(path)
            if _identity(before).kind is ObjectKind.DIRECTORY_MARKER:
                target = candidate_target(Path(state["source_path"]), path)
                if target.is_dir() and not target.is_symlink():
                    primary[path] = before  # type: ignore[assignment]
                    continue
            if _identity(before) == _identity(after):
                continue
            files.append(
                BatchFile(
                    path,
                    _identity(before),
                    self._body(after),
                    _identity(after).mode,
                    self._body(before),
                    _identity(after).permissions,
                    bool((after or {}).get("binary") or (before or {}).get("binary"))
                    or _binary(self._body(after)),
                )
            )
        if not files:
            return SyncResult(sequence=int(state["source_sequence"]))
        # Source scans contain leaves; explicit parent operations make a newly
        # added source subtree replayable in a different physical worktree.
        added = {file.relative_path for file in files}
        directories = []
        for file in files:
            if file.content is None:
                continue
            for parent in Path(file.relative_path).parents:
                relative = parent.as_posix()
                if relative == "." or relative in baseline or relative in added:
                    continue
                directories.append(BatchFile(relative, ABSENT, b"", DIRECTORY_MODE))
                added.add(relative)
        files = [*directories, *files]
        # Retain source artifacts before the journal, including empty directory bodies.
        for file in files:
            if file.content is not None:
                store_candidate_content(self.publisher.candidate_dir, file.content)
        request = _request("isolated-source-refresh", files, ("*",), False)
        report = self._prepare(workspace_id, int(state["source_sequence"]), request)
        self.recover(workspace_id)
        return report

    def _conflicts(self, workspace_id: str) -> dict[str, dict[str, Any]]:
        with self.store.connection() as con:
            journals = con.execute(
                "SELECT steps_json FROM isolated_sync_journal WHERE workspace_id=? "
                "AND state='applied' ORDER BY generation",
                (workspace_id,),
            ).fetchall()
        unresolved: dict[str, dict[str, Any]] = {}
        for journal in journals:
            for step in json.loads(journal["steps_json"]):
                if step["disposition"] == "divergence_preserved":
                    unresolved[step["path"]] = step
                else:
                    unresolved.pop(step["path"], None)
        return unresolved

    def _prepare(
        self, workspace_id: str, sequence: int, request_json: str
    ) -> SyncResult:
        state = self._state(workspace_id)
        root = Path(state["canonical_path"])
        manifest: Manifest = json.loads(state["manifest_json"])
        steps: list[dict[str, Any]] = []
        applied: list[str] = []
        merged: list[str] = []
        conflicts: list[str] = []
        unresolved = self._conflicts(workspace_id)
        incoming_files = _journal_files(request_json)
        if excluded_paths(root, [item.relative_path for item in incoming_files]):
            raise WorkspaceBrokerError(
                "incoming source is excluded by the current isolated policy"
            )
        for incoming in incoming_files:
            path = incoming.relative_path
            if self._runtime(path, state):
                raise WorkspaceBrokerError(
                    "incoming batch overlaps retained runtime data"
                )
            accepted = manifest.get(path)
            original = self._body(accepted)
            result = _entry(incoming.result, incoming.content_hash, incoming.binary)
            theirs = self._body(result)
            current, ours = read_candidate_path(
                candidate_target(root, path), max_bytes=MAX_SHARED_TEXT_BYTES
            )
            if ours is not None:
                ensure_safe_content(ours)
            local_hash = (
                store_candidate_content(self.publisher.candidate_dir, ours)
                if ours is not None
                else None
            )
            after = current
            content_hash: str | None = None
            disposition = "already_current"
            if current == incoming.result:
                pass
            elif path in unresolved and current == _identity_from_json(
                unresolved[path]["after"]
            ):
                # A later disjoint edit in the same file must not silently
                # resolve an earlier overlap the user has not addressed.
                disposition = "divergence_preserved"
            elif current == _identity(accepted) and incoming.base == _identity(
                accepted
            ):
                after, content_hash = incoming.result, incoming.content_hash
                disposition = "applied"
                applied.append(path)
            elif (
                incoming.base == _identity(accepted)
                and not incoming.binary
                and not bool(accepted and accepted.get("binary"))
                and current.kind is ObjectKind.REGULAR
                and incoming.result.kind is ObjectKind.REGULAR
                and original is not None
                and ours is not None
                and theirs is not None
                and b"\0" not in original + ours + theirs
            ):
                try:
                    merge = merge_text(original, ours, theirs)
                except UnicodeDecodeError:
                    merge = None
                if merge is not None and not merge.conflicts:
                    mode = (
                        incoming.result.mode
                        if current.mode == incoming.base.mode
                        else current.mode
                    )
                    if (
                        current.mode != incoming.base.mode
                        and incoming.result.mode
                        not in {incoming.base.mode, current.mode}
                    ):
                        merge = None
                    else:
                        after = result_identity(
                            BatchFile(path, current, merge.content, mode)
                        )
                        content_hash = store_candidate_content(
                            self.publisher.candidate_dir, merge.content
                        )
                        disposition = "merged"
                        merged.append(path)
                if merge is None or merge.conflicts:
                    disposition = "divergence_preserved"
            else:
                disposition = "divergence_preserved"
            if disposition == "divergence_preserved":
                conflicts.append(path)
            if incoming.result.absent:
                manifest.pop(path, None)
            else:
                manifest[path] = result
            steps.append(
                {
                    "path": path,
                    "before": _identity_json(current),
                    "local_content_hash": local_hash,
                    "after": _identity_json(after),
                    "content_hash": content_hash,
                    "disposition": disposition,
                    "incoming": result,
                }
            )
        with self.store.connection() as con, immediate_transaction(con):
            con.execute(
                "INSERT INTO "
                "isolated_sync_journal(workspace_id,sequence,generation,"
                "manifest_json,steps_json,state) VALUES(?,?,?,?,?,'prepared')",
                (
                    workspace_id,
                    sequence,
                    state["generation"],
                    _encode(manifest),
                    _encode(steps),
                ),
            )
        return SyncResult(tuple(applied), tuple(merged), tuple(conflicts), sequence)

    def recover(self, workspace_id: str) -> None:
        with self.publisher.lock:
            state = self._state(workspace_id)
            with self.store.connection() as con:
                journals = con.execute(
                    "SELECT * FROM isolated_sync_journal WHERE "
                    "workspace_id=? AND state='prepared' ORDER BY generation",
                    (workspace_id,),
                ).fetchall()
            root = Path(state["canonical_path"])
            for journal in journals:
                steps = json.loads(journal["steps_json"])
                # Validate the entire transition before making any further write.
                for step in steps:
                    path = normalize_changed_path(step["path"])
                    if self._runtime(path, state):
                        raise WorkspaceBrokerError(
                            "isolated journal overlaps runtime data"
                        )
                    current, _ = read_candidate_path(
                        candidate_target(root, path), max_bytes=MAX_SHARED_TEXT_BYTES
                    )
                    if current not in (
                        _identity_from_json(step["before"]),
                        _identity_from_json(step["after"]),
                    ):
                        raise WorkspaceBrokerError(
                            "isolated recovery found an unrecognized local state"
                        )
                    if step["content_hash"] is not None:
                        self._body(
                            {**step["after"], "content_hash": step["content_hash"]}
                        )
                ordered = sorted(
                    steps,
                    key=lambda step: (
                        0
                        if step["after"]["mode"] == DIRECTORY_MODE
                        else 2
                        if step["after"]["kind"] == "absent"
                        else 1,
                        (-1 if step["after"]["kind"] == "absent" else 1)
                        * len(Path(step["path"]).parts),
                    ),
                )
                for ordinal, step in enumerate(ordered):
                    target = candidate_target(root, step["path"])
                    before, after = (
                        _identity_from_json(step["before"]),
                        _identity_from_json(step["after"]),
                    )
                    current, _ = read_candidate_path(
                        target, max_bytes=MAX_SHARED_TEXT_BYTES
                    )
                    if current == after:
                        continue
                    if current != before:
                        raise WorkspaceBrokerError(
                            "isolated path changed during synchronization"
                        )
                    staging_id = hashlib.sha256(
                        f"isolated:{workspace_id}:{journal['generation']}:{ordinal}".encode()
                    ).hexdigest()
                    if after.absent:
                        if before.kind is ObjectKind.DIRECTORY_MARKER:
                            _directory_transition(
                                root, step["path"], before, create=False
                            )
                        else:
                            atomic_delete_file(
                                root=root,
                                relative_path=step["path"],
                                expected=before,
                            )
                        _sync_parent(target)
                    elif after.kind is ObjectKind.DIRECTORY_MARKER:
                        _directory_transition(root, step["path"], before, create=True)
                        _sync_parent(target)
                    elif after.kind is ObjectKind.REGULAR:
                        body = self._body(
                            {**step["after"], "content_hash": step["content_hash"]}
                        )
                        assert body is not None
                        atomic_replace_regular_file(
                            checkout_root=root,
                            git_common_dir=Path(state["git_common_dir"]),
                            relative_path=step["path"],
                            content=body,
                            mode=after.mode,
                            publication_id=staging_id,
                            permissions=after.permissions,
                            expected_base=before,
                        )
                    elif after.kind is ObjectKind.SYMLINK:
                        body = self._body(
                            {**step["after"], "content_hash": step["content_hash"]}
                        )
                        assert body is not None
                        atomic_replace_symlink(
                            checkout_root=root,
                            git_common_dir=Path(state["git_common_dir"]),
                            relative_path=step["path"],
                            content=body,
                            publication_id=staging_id,
                            expected_base=before,
                        )
                    else:
                        raise WorkspaceBrokerError(
                            "isolated synchronization cannot materialize this path kind"
                        )
                with self.store.connection() as con, immediate_transaction(con):
                    for step in steps:
                        current, _ = read_candidate_path(
                            candidate_target(root, step["path"]),
                            max_bytes=MAX_SHARED_TEXT_BYTES,
                        )
                        if current != _identity_from_json(step["after"]):
                            raise WorkspaceBrokerError(
                                "isolated synchronized path changed before confirmation"
                            )
                    changed = con.execute(
                        "UPDATE isolated_sync_state SET "
                        "manifest_json=?,source_sequence=?,generation=generation+1 "
                        "WHERE workspace_id=? AND generation=?",
                        (
                            journal["manifest_json"],
                            journal["sequence"],
                            workspace_id,
                            journal["generation"],
                        ),
                    ).rowcount
                    if changed != 1:
                        raise WorkspaceBrokerError(
                            "isolated synchronization generation changed"
                        )
                    con.execute(
                        "UPDATE isolated_sync_journal SET state='applied' "
                        "WHERE workspace_id=? AND generation=?",
                        (workspace_id, journal["generation"]),
                    )

    def publication_files(
        self, workspace_id: str, overlay: Sequence[BatchFile]
    ) -> tuple[BatchFile, ...]:
        """Freeze local source plus pending edits against the accepted baseline."""
        with self.publisher.lock:
            state = self._state(workspace_id)
            baseline: Manifest = json.loads(state["manifest_json"])
            local = self.capture(
                Path(state["canonical_path"]),
                tuple(json.loads(state["runtime_paths_json"])),
            )
            for item in overlay:
                if self._runtime(item.relative_path, state):
                    raise WorkspaceBrokerError("isolated edit overlaps runtime data")
                if _identity(local.get(item.relative_path)) != item.base:
                    raise WorkspaceBrokerError(
                        "isolated pending edit needs reconciliation"
                    )
                if item.content is None:
                    local.pop(item.relative_path, None)
                else:
                    local[item.relative_path] = _entry(
                        result_identity(item),
                        store_candidate_content(
                            self.publisher.candidate_dir, item.content
                        ),
                        item.binary,
                    )
            # Directory operations have identities in incoming batches, while
            # the source scanner inventories leaves. Keep existing directories.
            for path, entry in baseline.items():
                if _identity(entry).kind is ObjectKind.DIRECTORY_MARKER:
                    target = candidate_target(Path(state["canonical_path"]), path)
                    if target.is_dir() and not target.is_symlink():
                        local[path] = entry
            unresolved = self._conflicts(workspace_id)
            for path, step in unresolved.items():
                current = _identity(local.get(path))
                if current == _identity_from_json(
                    step["after"]
                ) and current != _identity(step["incoming"]):
                    raise WorkspaceBrokerError(
                        "isolated incoming conflict remains unresolved: " + path
                    )
            files = []
            for path in sorted(baseline.keys() | local.keys()):
                if self._runtime(path, state):
                    continue
                before, after = baseline.get(path), local.get(path)
                if _identity(before) == _identity(after):
                    continue
                content = self._body(after)
                if has_conflict_markers(content):
                    raise WorkspaceBrokerError(
                        "isolated source contains unresolved conflict markers"
                    )
                files.append(
                    BatchFile(
                        path,
                        _identity(before),
                        content,
                        _identity(after).mode,
                        self._body(before),
                        _identity(after).permissions,
                        bool(
                            (after or {}).get("binary") or (before or {}).get("binary")
                        )
                        or _binary(content),
                    )
                )
            added = {file.relative_path for file in files}
            directories = []
            for file in files:
                if file.content is None:
                    continue
                for parent in Path(file.relative_path).parents:
                    if str(parent) == ".":
                        continue
                    relative = parent.as_posix()
                    if relative in added:
                        continue
                    source_parent = candidate_target(
                        Path(state["source_path"]), relative
                    )
                    if not source_parent.exists() and not source_parent.is_symlink():
                        directories.append(
                            BatchFile(relative, ABSENT, b"", DIRECTORY_MODE)
                        )
                        added.add(relative)
            return tuple(
                sorted([*directories, *files], key=lambda file: file.relative_path)
            )

    def publish(
        self,
        batch_id: str,
        session_id: str,
        files: Sequence[BatchFile],
        scopes: tuple[str, ...],
        *,
        case_insensitive_filesystem: bool = False,
    ) -> BatchPublication:
        with self.publisher.lock:
            publication = self.publisher.publish(
                batch_id, session_id, files, scopes, case_insensitive_filesystem
            )
            if publication.state == "published":
                session = self.store.get_session(session_id)
                assert session is not None and session.workspace_id is not None
                self.after_publication(batch_id, session.workspace_id)
            return publication

    def after_publication(self, batch_id: str, workspace_id: str) -> None:
        """Private catch-up cannot turn a confirmed shared publication into failure."""
        try:
            self.synchronize(workspace_id)
        except (ValueError, OSError, LlmCoordError):
            with self.store.connection() as con:
                execution = con.execute(
                    "SELECT task_id FROM task_executions WHERE execution_id=?",
                    (batch_id,),
                ).fetchone()
            if execution is not None:
                self.store.record_task_event(
                    claim_id=None,
                    task_id=str(execution["task_id"]),
                    event_type="workspace.isolated_sync_pending",
                    payload={
                        "message": (
                            "Published to the shared checkout; "
                            "private synchronization needs recovery."
                        )
                    },
                )


def _sync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _directory_transition(
    root: Path, relative: str, before: FileIdentity, *, create: bool
) -> None:
    target = candidate_target(root, relative)
    descriptor = _open_publication_parent(root, relative)
    try:
        current, _ = read_candidate_path(target)
        parent = target.parent.stat()
        opened = os.fstat(descriptor)
        if current != before or (parent.st_dev, parent.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise WorkspaceBrokerError(
                "isolated directory changed during synchronization"
            )
        if create:
            os.mkdir(target.name, 0o700, dir_fd=descriptor)
        else:
            os.rmdir(target.name, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
