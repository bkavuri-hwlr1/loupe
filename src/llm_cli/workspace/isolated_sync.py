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
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm_cli.agent.source_policy import (
    ensure_safe_content,
    excluded_paths,
    runtime_path_matches,
)
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
    deferred: tuple[str, ...] = ()
    runtime_notices: tuple[str, ...] = ()

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
        if self.deferred:
            paths = ", ".join(path[:200] for path in self.deferred[:10])
            suffix = " and other paths" if len(self.deferred) > 10 else ""
            notices.append(
                "Source synchronization deferred for "
                + paths
                + suffix
                + "; keeping accepted source and retrying at the next safe point."
            )
        notices.extend(self.runtime_notices)
        return "\n".join(notices)


class IsolatedSynchronizer:
    def __init__(
        self,
        store: ControlStore,
        publisher: SharedBatchPublisher,
        capture: Callable[[Path, Sequence[str]], Manifest],
        validate: Callable[[str], object] | None = None,
        *,
        refresh_capture: Callable[[Path, Sequence[str]], Manifest] | None = None,
    ) -> None:
        self.store = store
        self.publisher = publisher
        self.capture = capture
        self.validate = validate
        self.refresh_capture = refresh_capture or capture
        self.runtime_refresh: Callable[[str], tuple[str, ...]] | None = None

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
                "w.git_dir AS private_git_dir,"
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
        return runtime_path_matches(path, json.loads(state["runtime_paths_json"]))

    @contextmanager
    def _authority_pin(
        self, workspace_id: str, *, recovery: bool = False
    ) -> Iterator[None]:
        authority = getattr(self.store, "authority", None)
        if authority is None:
            yield
            return
        with self.store.connection() as con:
            row = con.execute(
                "SELECT c.canonical_path FROM workspaces w "
                "JOIN checkouts c USING(checkout_id) WHERE w.workspace_id=?",
                (workspace_id,),
            ).fetchone()
        if row is None:
            raise WorkspaceBrokerError(
                "isolated source ownership binding is unavailable"
            )
        with authority.pin(Path(row[0]), recovery=recovery):
            yield

    def synchronize(
        self,
        workspace_id: str,
        *,
        refresh_source: bool = True,
        refresh_runtime: bool = False,
    ) -> SyncResult:
        with self._authority_pin(workspace_id):
            return self._synchronize(
                workspace_id,
                refresh_source=refresh_source,
                refresh_runtime=refresh_runtime,
            )

    def _synchronize(
        self,
        workspace_id: str,
        *,
        refresh_source: bool = True,
        refresh_runtime: bool = False,
    ) -> SyncResult:
        """Apply complete ordered incoming batches at a caller-owned safe point."""
        runtime_notices = (
            self.runtime_refresh(workspace_id)
            if self.runtime_refresh and refresh_source and refresh_runtime
            else ()
        )
        with self.publisher.lock:
            self.recover(workspace_id)
            state = self._state(workspace_id)
            applied: list[str] = []
            merged: list[str] = []
            conflicted: list[str] = []
            sequence = int(state["source_sequence"])
            with self.store.connection() as con:
                latest = con.execute(
                    "SELECT MAX(sequence) FROM checkout_events WHERE checkout_id=?",
                    (state["checkout_id"],),
                ).fetchone()[0]
            upper_bound = max(sequence, int(latest or 0))
            while sequence < upper_bound:
                with self.store.connection() as con:
                    rows = con.execute(
                        "SELECT sequence,event_type,payload_json FROM checkout_events "
                        "WHERE checkout_id=? AND sequence>? AND sequence<=? "
                        "ORDER BY sequence LIMIT 1000",
                        (state["checkout_id"], sequence, upper_bound),
                    ).fetchall()
                if not rows:
                    raise WorkspaceBrokerError("isolated incoming history has a gap")
                for row in rows:
                    if int(row["sequence"]) != sequence + 1:
                        raise WorkspaceBrokerError(
                            "isolated incoming history has a gap"
                        )
                    sequence += 1
                    if row["event_type"] != "workspace.batch_published":
                        # Exact source comparison below also catches outside writes.
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
                        raise WorkspaceBrokerError(
                            "incoming batch artifact is unavailable"
                        )
                    report = self._prepare(
                        workspace_id, sequence, str(batch["request_json"])
                    )
                    self.recover(workspace_id)
                    applied.extend(report.applied)
                    merged.extend(report.merged)
                    conflicted.extend(report.conflicted)
            state = self._state(workspace_id)
        # Filesystem scanning, screening and artifact writes must not monopolize
        # the same lock that every shared session needs to publish.
        if refresh_source:
            try:
                primary = self.refresh_capture(
                    Path(state["source_path"]),
                    tuple(json.loads(state["runtime_paths_json"])),
                )
            except (LlmCoordError, OSError, ValueError):
                primary = None
            with self.publisher.lock:
                current = self._state(workspace_id)
                with self.store.connection() as con:
                    latest = con.execute(
                        "SELECT MAX(sequence) FROM checkout_events WHERE checkout_id=?",
                        (state["checkout_id"],),
                    ).fetchone()[0]
                if (
                    primary is None
                    or current["generation"] != state["generation"]
                    or current["source_sequence"] != state["source_sequence"]
                    or int(latest or 0) != upper_bound
                ):
                    return SyncResult(
                        tuple(applied),
                        tuple(merged),
                        tuple(conflicted),
                        sequence,
                        ("checkout snapshot",),
                        runtime_notices,
                    )
                report = self._refresh_primary(workspace_id, primary)
                applied.extend(report.applied)
                merged.extend(report.merged)
                conflicted.extend(report.conflicted)
                deferred = report.deferred
        else:
            deferred = ()
        return SyncResult(
            tuple(applied),
            tuple(merged),
            tuple(conflicted),
            sequence,
            deferred,
            runtime_notices,
        )

    def _refresh_primary(self, workspace_id: str, primary: Manifest) -> SyncResult:
        """Reconcile a source snapshot captured without the publication lock."""
        state = self._state(workspace_id)
        baseline: Manifest = json.loads(state["manifest_json"])
        deferred = set(getattr(primary, "deferred", ()))
        files = []
        screened: list[str] = []
        for path in sorted(baseline.keys() | primary.keys()):
            if self._runtime(path, state):
                continue
            before, after = baseline.get(path), primary.get(path)
            if path in deferred:
                continue
            if (before or {}).get("screened") or (after or {}).get("screened"):
                if _identity(before) != _identity(after):
                    screened.append(path)
                continue
            if _identity(before).kind is ObjectKind.DIRECTORY_MARKER:
                target = candidate_target(Path(state["source_path"]), path)
                if target.is_dir() and not target.is_symlink():
                    continue
            if _identity(before) == _identity(after):
                continue
            theirs = self._body(after)
            original = (
                self._body(before)
                if _identity(before).kind in {ObjectKind.REGULAR, ObjectKind.SYMLINK}
                else None
            )
            files.append(
                BatchFile(
                    path,
                    _identity(before),
                    theirs,
                    _identity(after).mode,
                    original,
                    _identity(after).permissions,
                    bool((after or {}).get("binary") or (before or {}).get("binary"))
                    or _binary(theirs),
                )
            )
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
        # Bodies already live in the capture cache's immutable artifact store.
        for file in directories:
            store_candidate_content(self.publisher.candidate_dir, file.content or b"")
        # Retire opaque leaves before their ordinary directory operations, so
        # an unchanged private fixture does not create a false container conflict.
        opaque = self._prepare_screened(workspace_id, primary, screened)
        self.recover(workspace_id)
        waiting = deferred | set(opaque.deferred)
        files = [
            file
            for file in files
            if not (
                file.content is None
                and file.base.kind is ObjectKind.DIRECTORY_MARKER
                and any(path.startswith(file.relative_path + "/") for path in waiting)
            )
        ]
        report = SyncResult(sequence=int(state["source_sequence"]))
        if files:
            request = _request("isolated-source-refresh", files, ("*",), False)
            report = self._prepare(workspace_id, int(state["source_sequence"]), request)
            self.recover(workspace_id)
        return SyncResult(
            (*report.applied, *opaque.applied),
            report.merged,
            (*report.conflicted, *opaque.conflicted),
            report.sequence,
            tuple(sorted(deferred | set(opaque.deferred))),
        )

    @staticmethod
    def _opaque_git_dir(state: Mapping[str, Any]) -> Path:
        # The manager validates a real private worktree's pinned Git directory.
        # Standalone synchronizers may use a complete private test repository.
        directory = Path(
            state["private_git_dir"] or Path(state["canonical_path"]) / ".git"
        )
        if directory.resolve(strict=True) != directory or not directory.is_dir():
            raise WorkspaceBrokerError("private screened staging directory changed")
        return directory

    def _stage_screened(self, state: Mapping[str, Any], body: bytes) -> str:
        from llm_cli.workspace.isolated import _parent, _write_body

        token = uuid.uuid4().hex
        directory = self._opaque_git_dir(state)
        with _parent(directory, "loupe-screened-incoming") as (parent, name):
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
        with _parent(directory, "loupe-screened-incoming/" + token) as (parent, name):
            os.fchmod(parent, 0o700)
            _write_body(parent, name, body, 0o600)
        return token

    def _screened_body(
        self, state: Mapping[str, Any], step: Mapping[str, Any]
    ) -> bytes:
        from llm_cli.workspace.isolated import _read, _source

        token = step["private_content_token"]
        if (
            not isinstance(token, str)
            or len(token) != 32
            or any(character not in "0123456789abcdef" for character in token)
        ):
            raise WorkspaceBrokerError("private screened staging token is invalid")
        with _source(
            self._opaque_git_dir(state), "loupe-screened-incoming/" + token
        ) as descriptor:
            body = _read(descriptor, MAX_SHARED_TEXT_BYTES)
        expected = _identity(step["incoming"])
        if result_identity(BatchFile("body", ABSENT, body, expected.mode)) != expected:
            raise WorkspaceBrokerError("private screened incoming identity changed")
        return body

    def _prepare_screened(
        self, workspace_id: str, primary: Manifest, paths: Sequence[str]
    ) -> SyncResult:
        """Journal opaque fixture copies by identity, never ordinary artifacts."""
        if not paths:
            return SyncResult()
        state = self._state(workspace_id)
        root = Path(state["canonical_path"])
        manifest: Manifest = json.loads(state["manifest_json"])
        unresolved = self._conflicts(workspace_id)
        steps: list[dict[str, Any]] = []
        applied: list[str] = []
        conflicted: list[str] = []
        deferred: list[str] = []
        parents: set[str] = set()
        for path in paths:
            incoming = primary.get(path)
            expected = _identity(incoming)
            try:
                # The scan happened outside the lock; a later save must defer
                # this path rather than copying bytes from another identity.
                actual, body = read_candidate_path(
                    candidate_target(Path(state["source_path"]), path),
                    max_bytes=MAX_SHARED_TEXT_BYTES,
                )
                if actual != expected:
                    deferred.append(path)
                    continue
                current, _ = read_candidate_path(
                    candidate_target(root, path), max_bytes=MAX_SHARED_TEXT_BYTES
                )
                token = self._stage_screened(state, body) if body is not None else None
            except (OSError, ValueError, WorkspaceBrokerError):
                deferred.append(path)
                continue
            accepted = _identity(manifest.get(path))
            after = current
            disposition = "already_current"
            if current != expected:
                previous = unresolved.get(path)
                unresolved_here = (
                    previous is not None
                    and current == _identity_from_json(previous["after"])
                    and current != _identity(previous["incoming"])
                )
                if current == accepted and not unresolved_here:
                    after = expected
                    disposition = "applied"
                    applied.append(path)
                else:
                    disposition = "divergence_preserved"
                    if path not in unresolved:
                        conflicted.append(path)
            # Retain an opaque absent identity too: a preserved local file
            # still needs source-tool exclusion and private cleanup on close.
            result = {
                **(incoming or _entry(ABSENT, None)),
                "screened": True,
                "content_hash": None,
            }
            manifest[path] = result
            if not after.absent:
                for parent in reversed(Path(path).parents):
                    relative = parent.as_posix()
                    if relative == "." or relative in parents:
                        continue
                    identity, _ = read_candidate_path(candidate_target(root, relative))
                    if identity.absent:
                        directory = result_identity(
                            BatchFile(relative, ABSENT, b"", DIRECTORY_MODE)
                        )
                        steps.append(
                            {
                                "path": relative,
                                "before": _identity_json(ABSENT),
                                "after": _identity_json(directory),
                                "content_hash": None,
                                "local_content_hash": None,
                                "disposition": "applied",
                                "incoming": _entry(directory, None),
                            }
                        )
                        manifest[relative] = _entry(
                            directory,
                            store_candidate_content(self.publisher.candidate_dir, b""),
                        )
                        parents.add(relative)
            steps.append(
                {
                    "path": path,
                    "before": _identity_json(current),
                    "after": _identity_json(after),
                    "content_hash": None,
                    "local_content_hash": None,
                    "disposition": disposition,
                    "incoming": result,
                    "screened": True,
                    "private_content_token": token,
                }
            )
        if steps:
            with self.store.connection() as con, immediate_transaction(con):
                con.execute(
                    "INSERT INTO isolated_sync_journal(workspace_id,sequence,"
                    "generation,manifest_json,steps_json,state) "
                    "VALUES(?,?,?,?,?,'prepared')",
                    (
                        workspace_id,
                        state["source_sequence"],
                        state["generation"],
                        _encode(manifest),
                        _encode(steps),
                    ),
                )
        return SyncResult(
            tuple(applied),
            (),
            tuple(conflicted),
            int(state["source_sequence"]),
            tuple(deferred),
        )

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
                candidate_target(root, path),
                max_bytes=MAX_SHARED_TEXT_BYTES,
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
            elif current == _identity(accepted) and (
                current.absent
                or incoming.result.absent
                or current.kind is incoming.result.kind
            ):
                # A shared batch can include outside edits absent from this
                # session's baseline. Only private divergence blocks receiving
                # its exact result; unsupported cross-kind replacements remain
                # conflicts rather than reaching a typed writer that refuses them.
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
        preserved = _preserve_local_directories(root, steps)
        applied = [path for path in applied if path not in preserved]
        conflicts.extend(preserved)
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
        with self._authority_pin(workspace_id, recovery=True):
            self._recover(workspace_id)

    def _recover(self, workspace_id: str) -> None:
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
                if _preserve_local_directories(root, steps):
                    # A private child can be added after preparation or a crash.
                    # Retain its container durably before replaying other paths.
                    with self.store.connection() as con:
                        con.execute(
                            "UPDATE isolated_sync_journal SET steps_json=? "
                            "WHERE workspace_id=? AND generation=? "
                            "AND state='prepared'",
                            (_encode(steps), workspace_id, journal["generation"]),
                        )
                # Validate the entire transition before making any further write.
                for step in steps:
                    path = normalize_changed_path(step["path"])
                    if self._runtime(path, state):
                        raise WorkspaceBrokerError(
                            "isolated journal overlaps runtime data"
                        )
                    current, _ = read_candidate_path(
                        candidate_target(root, path),
                        max_bytes=MAX_SHARED_TEXT_BYTES,
                    )
                    if current not in (
                        _identity_from_json(step["before"]),
                        _identity_from_json(step["after"]),
                    ):
                        raise WorkspaceBrokerError(
                            "isolated recovery found an unrecognized local state"
                        )
                    if step.get("private_content_token") is not None:
                        self._screened_body(state, step)
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
                        target,
                        max_bytes=MAX_SHARED_TEXT_BYTES,
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
                        body = (
                            self._screened_body(state, step)
                            if step.get("screened")
                            else self._body(
                                {**step["after"], "content_hash": step["content_hash"]}
                            )
                        )
                        assert body is not None
                        atomic_replace_regular_file(
                            checkout_root=root,
                            git_common_dir=(
                                self._opaque_git_dir(state)
                                if step.get("screened")
                                else Path(state["git_common_dir"])
                            ),
                            relative_path=step["path"],
                            content=body,
                            mode=after.mode,
                            publication_id=staging_id,
                            permissions=after.permissions,
                            expected_base=before,
                        )
                    elif after.kind is ObjectKind.SYMLINK:
                        body = (
                            self._screened_body(state, step)
                            if step.get("screened")
                            else self._body(
                                {**step["after"], "content_hash": step["content_hash"]}
                            )
                        )
                        assert body is not None
                        atomic_replace_symlink(
                            checkout_root=root,
                            git_common_dir=(
                                self._opaque_git_dir(state)
                                if step.get("screened")
                                else Path(state["git_common_dir"])
                            ),
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
                            max_bytes=(
                                256 * 1024 * 1024
                                if step.get("screened")
                                else MAX_SHARED_TEXT_BYTES
                            ),
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

                for step in steps:
                    token = step.get("private_content_token")
                    if (
                        token is not None
                        and step["disposition"] != "divergence_preserved"
                    ):
                        from llm_cli.workspace.isolated import _parent

                        with _parent(
                            self._opaque_git_dir(state),
                            "loupe-screened-incoming/" + token,
                        ) as (parent, name):
                            os.unlink(name, dir_fd=parent)
                            os.fsync(parent)

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
                if (baseline.get(item.relative_path) or {}).get("screened") or (
                    local.get(item.relative_path) or {}
                ).get("screened"):
                    raise WorkspaceBrokerError(
                        "isolated edit overlaps screened private source"
                    )
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
                if step["after"]["kind"] == "directory_marker":
                    current, _ = read_candidate_path(
                        candidate_target(Path(state["canonical_path"]), path)
                    )
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
                if (before or {}).get("screened") or (after or {}).get("screened"):
                    continue
                if _identity(before) == _identity(after):
                    continue
                content = self._body(after)
                binary = bool(
                    (after or {}).get("binary") or (before or {}).get("binary")
                ) or _binary(content)
                if not binary and has_conflict_markers(content):
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
                        binary,
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
            self.synchronize(workspace_id, refresh_source=False)
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


def _preserve_local_directories(
    root: Path, steps: list[dict[str, Any]]
) -> tuple[str, ...]:
    """Keep containers whose children will survive this incoming transition."""
    deleted = {step["path"] for step in steps if step["after"]["kind"] == "absent"}
    preserved = []
    for step in sorted(steps, key=lambda item: -len(Path(item["path"]).parts)):
        if (
            step["before"]["kind"] != "directory_marker"
            or step["after"]["kind"] != "absent"
        ):
            continue
        target = candidate_target(root, step["path"])
        current, _ = read_candidate_path(target)
        if current.kind is not ObjectKind.DIRECTORY_MARKER:
            continue  # Recovery's exact state check handles changed path kinds.
        descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            live = target.lstat()
            if (opened.st_dev, opened.st_ino) != (live.st_dev, live.st_ino):
                raise WorkspaceBrokerError(
                    "isolated directory changed during inspection"
                )
            with os.scandir(descriptor) as entries:
                retained_child = any(
                    step["path"] + "/" + entry.name not in deleted for entry in entries
                )
        finally:
            os.close(descriptor)
        if retained_child:
            step["after"] = step["before"]
            step["content_hash"] = None
            step["disposition"] = "divergence_preserved"
            deleted.remove(step["path"])
            preserved.append(step["path"])
    return tuple(preserved)


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
