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
from bisect import bisect_left
from collections.abc import Callable, Iterator, Mapping, Sequence, Set
from contextlib import contextmanager
from contextvars import ContextVar
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
    MAX_BATCH_BYTES,
    MAX_BATCH_FILES,
    BatchFile,
    BatchPublication,
    SharedBatchPublisher,
    _identity_json,
    _JournalFile,
    _request,
    candidate_target,
    result_identity,
)
from llm_cli.workspace.batches import _identity_from_json as _stored_identity
from llm_cli.workspace.batches import _journal_files as _stored_journal_files
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

# Applied steps are diagnostic only; unresolved evidence has its own durable row.
_APPLIED_JOURNAL_WINDOW = 8

# The isolated capture's per-file limit. Outside edits are the user's own
# source, so catch-up carries files a shared edit cannot, such as lockfiles.
_SOURCE_FILE_BYTES = 256 * 1024 * 1024


def _identity_from_json(value: object) -> FileIdentity:
    return _stored_identity(value, max_size=_SOURCE_FILE_BYTES)


def _journal_files(request_json: str) -> tuple[_JournalFile, ...]:
    return _stored_journal_files(request_json, max_size=_SOURCE_FILE_BYTES)


# A safe point validates its private worktree when it starts and again after
# its unlocked source capture, not at every internal step.
_VALIDATED: ContextVar[set[str] | None] = ContextVar(
    "isolated_sync_validated", default=None
)


@contextmanager
def _validation_scope() -> Iterator[None]:
    if _VALIDATED.get() is not None:
        yield
        return
    token = _VALIDATED.set(set())
    try:
        yield
    finally:
        _VALIDATED.reset(token)


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
    # Source manifests and outside catch-up carry files up to the capture's
    # limit; shared batch limits apply only to edits sessions publish.
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
        or not 0 <= identity.size <= _SOURCE_FILE_BYTES
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


def _listed(paths: Sequence[str], limit: int = 20) -> str:
    # A branch switch can deliver thousands of paths in one safe point.
    shown = ", ".join(paths[:limit])
    return shown + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


class _OpeningDirectoryChanged(Exception):
    def __init__(self, path: str) -> None:
        self.path = path


@dataclass(frozen=True, slots=True)
class _CatchUp:
    """One path's outside change, loaded as a BatchFile only when journaled."""

    path: str
    before: Mapping[str, Any] | None
    after: Mapping[str, Any] | None
    directory: bool = False

    @property
    def size(self) -> int:
        return sum(
            identity.size
            for identity in (_identity(self.before), _identity(self.after))
            if identity.kind in {ObjectKind.REGULAR, ObjectKind.SYMLINK}
        )


def _placeable(
    root: Path,
    steps: Sequence[_CatchUp],
    *,
    creating: bool,
    held: Set[str] = frozenset(),
) -> tuple[list[_CatchUp], set[str]]:
    """Keep the steps recovery can replay; return the others as deferred paths.

    A prepared journal must complete, or every later safe point fails. A
    private file or link where a directory now belongs, a missing parent no
    earlier step creates, or a held removal defers that path and its subtree.
    """
    kept: list[_CatchUp] = []
    blocked: set[str] = set()
    created: set[str] = set()
    for step in steps:
        parents = [parent.as_posix() for parent in Path(step.path).parents][:-1]
        waiting = blocked | held
        try:
            candidate_target(root, step.path)
        except WorkspaceBrokerError:
            blocked.add(step.path)
            continue
        if (
            step.path in waiting
            or any(parent in waiting for parent in parents)
            or (
                creating
                and not all(
                    parent in created or (root / parent).is_dir() for parent in parents
                )
            )
        ):
            blocked.add(step.path)
            continue
        kept.append(step)
        if step.directory:
            created.add(step.path)
    return kept, blocked


def _chunks(steps: Sequence[_CatchUp]) -> list[list[_CatchUp]]:
    """Split ordered steps into journals one stored batch can hold."""
    chunks: list[list[_CatchUp]] = []
    total = 0
    for step in steps:
        if (
            not chunks
            or len(chunks[-1]) == MAX_BATCH_FILES
            or total + step.size > MAX_BATCH_BYTES
        ):
            chunks.append([])
            total = 0
        chunks[-1].append(step)
        total += step.size
    return chunks


def _unchanged_opening_directory(
    root: Path, relative: str, expected: Manifest
) -> tuple[str, ...] | None:
    """Prove the private subtree still equals its accepted source descendants."""
    directories = {relative}
    for path, entry in expected.items():
        if _identity(entry).kind is ObjectKind.DIRECTORY_MARKER:
            directories.add(path)
        for parent in Path(path).parents:
            name = parent.as_posix()
            if name == relative or name.startswith(relative + "/"):
                directories.add(name)
    pending = [relative]
    seen: set[str] = set()
    try:
        while pending:
            path = pending.pop()
            if path not in directories and path not in expected:
                return None
            target = candidate_target(root, path)
            current, _ = read_candidate_path(target, max_bytes=_SOURCE_FILE_BYTES)
            if path in directories:
                if current.kind is not ObjectKind.DIRECTORY_MARKER:
                    return None
                descriptor = os.open(
                    target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
                try:
                    opened, live = os.fstat(descriptor), target.lstat()
                    if (opened.st_dev, opened.st_ino) != (live.st_dev, live.st_ino):
                        return None
                    with os.scandir(descriptor) as entries:
                        for child_entry in entries:
                            child = path + "/" + child_entry.name
                            if child not in directories and child not in expected:
                                return None
                            pending.append(child)
                finally:
                    os.close(descriptor)
            elif path not in expected or current != _identity(expected[path]):
                return None
            seen.add(path)
    except (OSError, ValueError, LlmCoordError):
        return None
    if seen != expected.keys() | directories:
        return None
    return tuple(directories)


def _opening_directories(
    state: Mapping[str, Any], primary: Manifest
) -> tuple[_CatchUp, ...]:
    """Recover implicit opening parents only from matching accepted descendants.

    This proof runs outside the publication lock. Each later journal still
    validates its exact private before-state and protects newly added children.
    """
    baseline: Manifest = json.loads(state["manifest_json"])
    runtime_paths = json.loads(state["runtime_paths_json"])
    candidates = [
        path
        for path, entry in primary.items()
        if path not in baseline
        and _identity(entry).kind
        not in {ObjectKind.ABSENT, ObjectKind.DIRECTORY_MARKER}
        and not runtime_path_matches(path, runtime_paths)
    ]
    if not candidates:
        return ()
    accepted_paths = sorted(baseline)
    implicit: set[str] = set()
    for path in candidates:
        prefix = path + "/"
        index = bisect_left(accepted_paths, prefix)
        descendants: Manifest = {}
        while index < len(accepted_paths) and accepted_paths[index].startswith(prefix):
            descendant = accepted_paths[index]
            if not _identity(baseline[descendant]).absent:
                descendants[descendant] = baseline[descendant]
            index += 1
        if not descendants:
            continue
        unchanged = _unchanged_opening_directory(
            Path(state["canonical_path"]), path, descendants
        )
        if unchanged is not None:
            implicit.update(name for name in unchanged if name not in baseline)
    # Confirm descendants before their root marker. A crash between chunks
    # leaves that root implicit, so the next safe point can finish normalization.
    return tuple(
        _CatchUp(path, None, None, directory=True)
        for path in sorted(implicit, key=lambda path: (-len(Path(path).parts), path))
    )


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
            notices.append("Received shared changes: " + _listed(self.applied))
        if self.merged:
            notices.append(
                "Merged shared changes with local edits: " + _listed(self.merged)
            )
        if self.conflicted:
            notices.append(
                "Shared changes conflict with preserved isolated files: "
                + _listed(self.conflicted)
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
        self.process_notice: Callable[[str], str] | None = None
        self.process_writer_notice: Callable[[str], str] | None = None

    def _process_deferred(self, workspace_id: str) -> SyncResult | None:
        notice = self.process_notice(workspace_id) if self.process_notice else ""
        if not notice:
            return None
        state = self._state(workspace_id)
        return SyncResult(
            sequence=int(state["source_sequence"]),
            runtime_notices=(
                "Incoming synchronization waits for managed processes. " + notice,
            ),
        )

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

    def _state(self, workspace_id: str, *, revalidate: bool = False) -> dict[str, Any]:
        validated = _VALIDATED.get()
        if self.validate is not None and (
            validated is None or revalidate or workspace_id not in validated
        ):
            self.validate(workspace_id)
            if validated is not None:
                validated.add(workspace_id)
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
        with self._authority_pin(workspace_id), _validation_scope():
            return self._synchronize(
                workspace_id,
                refresh_source=refresh_source,
                refresh_runtime=refresh_runtime,
            )

    def _between_journals(self, workspace_id: str) -> None:
        """Fault hook after a confirmed journal and outside our publication lock."""

    def _synchronize(
        self,
        workspace_id: str,
        *,
        refresh_source: bool = True,
        refresh_runtime: bool = False,
    ) -> SyncResult:
        applied: list[str] = []
        opaque_applied: list[str] = []
        merged: list[str] = []
        conflicted: list[str] = []
        runtime_notices: list[str] = []
        while True:
            report, more, opaque = self._synchronize_pass(
                workspace_id,
                refresh_source=refresh_source,
                refresh_runtime=refresh_runtime,
            )
            (opaque_applied if opaque else applied).extend(report.applied)
            merged.extend(report.merged)
            conflicted.extend(report.conflicted)
            runtime_notices.extend(report.runtime_notices)
            if not more:
                conflicts = tuple(dict.fromkeys(conflicted))
                return SyncResult(
                    tuple(
                        path
                        for path in dict.fromkeys((*applied, *opaque_applied))
                        if path not in conflicts
                    ),
                    tuple(dict.fromkeys(merged)),
                    conflicts,
                    report.sequence,
                    report.deferred,
                    tuple(dict.fromkeys(runtime_notices)),
                )
            # The next pass starts from the manifest this journal confirmed and
            # consumes any intervening publication before recapturing source.
            self._between_journals(workspace_id)
            refresh_runtime = False

    def _synchronize_pass(
        self,
        workspace_id: str,
        *,
        refresh_source: bool,
        refresh_runtime: bool,
    ) -> tuple[SyncResult, bool, bool]:
        """Confirm at most one journal before yielding the publication lock."""
        with self.publisher.lock:
            deferred_process = self._process_deferred(workspace_id)
            if deferred_process is not None:
                return deferred_process, False, False
        runtime_notices = (
            self.runtime_refresh(workspace_id)
            if self.runtime_refresh and refresh_source and refresh_runtime
            else ()
        )
        with self.publisher.lock:
            deferred_process = self._process_deferred(workspace_id)
            if deferred_process is not None:
                return deferred_process, False, False
            # A previous pass yielded the lock; validate its private binding
            # before recovery or another journal can write to it.
            self._state(workspace_id, revalidate=True)
            self.recover(workspace_id)
            state = self._state(workspace_id)
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
                    waiting = self._pending_journal(workspace_id)
                    return (
                        SyncResult(
                            report.applied,
                            report.merged,
                            report.conflicted,
                            sequence,
                            runtime_notices=runtime_notices,
                        ),
                        not waiting and (sequence < upper_bound or refresh_source),
                        False,
                    )
            state = self._state(workspace_id)
        if not refresh_source:
            return (
                SyncResult(sequence=sequence, runtime_notices=runtime_notices),
                False,
                False,
            )
        # Scanning, screening and proving implicit opening directories must not
        # monopolize the same lock that every shared session needs to publish.
        try:
            primary = self.refresh_capture(
                Path(state["source_path"]),
                tuple(json.loads(state["runtime_paths_json"])),
            )
            directories = _opening_directories(state, primary)
        except (LlmCoordError, OSError, ValueError):
            primary = None
            directories = ()
        with self.publisher.lock:
            deferred_process = self._process_deferred(workspace_id)
            if deferred_process is not None:
                return (
                    SyncResult(
                        sequence=sequence,
                        runtime_notices=(
                            *runtime_notices,
                            *deferred_process.runtime_notices,
                        ),
                    ),
                    False,
                    False,
                )
            current = self._state(workspace_id, revalidate=True)
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
                return (
                    SyncResult(
                        sequence=sequence,
                        deferred=("checkout snapshot",),
                        runtime_notices=runtime_notices,
                    ),
                    False,
                    False,
                )
            report, more, opaque = self._refresh_primary(
                workspace_id, primary, directories
            )
            return (
                SyncResult(
                    report.applied,
                    report.merged,
                    report.conflicted,
                    sequence,
                    report.deferred,
                    runtime_notices,
                ),
                more,
                opaque,
            )

    def _pending_journal(self, workspace_id: str) -> bool:
        with self.store.connection() as con:
            return (
                con.execute(
                    "SELECT 1 FROM isolated_sync_journal "
                    "WHERE workspace_id=? AND state='prepared' LIMIT 1",
                    (workspace_id,),
                ).fetchone()
                is not None
            )

    def _refresh_primary(
        self,
        workspace_id: str,
        primary: Manifest,
        opening_directories: Sequence[_CatchUp] = (),
    ) -> tuple[SyncResult, bool, bool]:
        """Reconcile a source snapshot captured without the publication lock."""
        state = self._state(workspace_id)
        baseline: Manifest = json.loads(state["manifest_json"])
        runtime_paths = json.loads(state["runtime_paths_json"])
        deferred = set(getattr(primary, "deferred", ()))
        removals: list[_CatchUp] = []
        creations: list[_CatchUp] = []
        screened: list[str] = []
        # An identical entry needs none of the checks below; skip it before
        # sorting, so an unchanged large checkout costs one comparison per path.
        differing = [
            path
            for path in baseline.keys() | primary.keys()
            if (entry := baseline.get(path)) is None or entry != primary.get(path)
        ]
        if opening_directories:
            store_candidate_content(self.publisher.candidate_dir, b"")
            chunk = _chunks(opening_directories)[0]
            try:
                report, _ = self._journal(
                    workspace_id,
                    int(state["source_sequence"]),
                    chunk,
                    existing_directories=True,
                )
            except _OpeningDirectoryChanged as exc:
                return (
                    SyncResult(
                        sequence=int(state["source_sequence"]), deferred=(exc.path,)
                    ),
                    False,
                    False,
                )
            normalization_waiting = self._pending_journal(workspace_id)
            return (
                SyncResult(
                    report.applied,
                    report.merged,
                    report.conflicted,
                    report.sequence,
                    tuple(differing) if normalization_waiting else (),
                ),
                not normalization_waiting,
                False,
            )
        for path in sorted(differing):
            if runtime_path_matches(path, runtime_paths):
                continue
            before, after = baseline.get(path), primary.get(path)
            if path in deferred:
                continue
            if (before or {}).get("screened") or (after or {}).get("screened"):
                if _identity(before) != _identity(after):
                    screened.append(path)
                continue
            if _identity(before).kind is ObjectKind.DIRECTORY_MARKER:
                try:
                    target = candidate_target(Path(state["source_path"]), path)
                except WorkspaceBrokerError:
                    # A source ancestor may now be a file or link. Its old
                    # directory descendants still need deepest-first removal.
                    target = None
                if target is not None and target.is_dir() and not target.is_symlink():
                    continue
            base, result = _identity(before), _identity(after)
            if base == result:
                continue
            if not base.absent and not result.absent and base.kind is not result.kind:
                # A batch cannot change a path's kind in place: remove the old
                # object, then create the new one after every removal.
                removals.append(_CatchUp(path, before, None))
                creations.append(_CatchUp(path, None, after))
            elif result.absent:
                removals.append(_CatchUp(path, before, None))
            else:
                creations.append(_CatchUp(path, before, after))
        removed = {step.path for step in removals}
        # Source scans contain leaves; explicit parent operations make a newly
        # added source subtree replayable in a different physical worktree. A
        # parent that was a file gets its directory once the file is removed.
        created = {step.path for step in creations}
        directories = []
        for step in creations:
            for parent in Path(step.path).parents:
                relative = parent.as_posix()
                if relative == "." or relative in created:
                    continue
                if relative in baseline and relative not in removed:
                    continue
                directories.append(_CatchUp(relative, None, None, directory=True))
                created.add(relative)
        # Bodies already live in the capture cache's immutable artifact store.
        if directories:
            store_candidate_content(self.publisher.candidate_dir, b"")
        # Retire opaque leaves before ordinary directory operations. It is a
        # complete journal too, so yield before preparing an ordinary one.
        opaque = self._prepare_screened(workspace_id, primary, screened)
        self.recover(workspace_id)
        if self._pending_journal(workspace_id):
            return (
                SyncResult(
                    opaque.applied,
                    opaque.merged,
                    opaque.conflicted,
                    int(state["source_sequence"]),
                    tuple(sorted(deferred | set(opaque.deferred) | set(differing))),
                ),
                False,
                True,
            )
        if self._state(workspace_id)["generation"] != state["generation"]:
            return opaque, True, True
        waiting = deferred | set(opaque.deferred)
        removals = [
            step
            for step in removals
            if not (
                _identity(step.before).kind is ObjectKind.DIRECTORY_MARKER
                and any(path.startswith(step.path + "/") for path in waiting)
            )
        ]
        sequence = int(state["source_sequence"])
        root = Path(state["canonical_path"])
        removals, blocked = _placeable(
            root,
            sorted(removals, key=lambda step: (-len(Path(step.path).parts), step.path)),
            creating=False,
        )
        if removals:
            steps = removals
            more_after = bool(creations or directories)
        else:
            steps, waiting_creations = _placeable(
                root,
                sorted(
                    [*directories, *creations],
                    key=lambda step: (
                        not step.directory,
                        len(Path(step.path).parts),
                        step.path,
                    ),
                ),
                creating=True,
                held=removed,
            )
            blocked |= waiting_creations
            more_after = False
        if not steps:
            return (
                SyncResult(
                    sequence=sequence,
                    deferred=tuple(sorted(waiting | blocked)),
                ),
                False,
                False,
            )
        chunk = _chunks(steps)[0]
        report, _ = self._journal(workspace_id, sequence, chunk)
        pending = self._pending_journal(workspace_id)
        remaining = {step.path for step in steps[len(chunk) :]}
        if pending and removals:
            remaining.update(step.path for step in creations)
        conflicted = tuple(dict.fromkeys((*report.conflicted, *opaque.conflicted)))
        return (
            SyncResult(
                tuple(path for path in report.applied if path not in conflicted),
                report.merged,
                conflicted,
                sequence,
                tuple(sorted(waiting | blocked | (remaining if pending else set()))),
            ),
            not pending and (bool(remaining) or more_after),
            False,
        )

    def _journal(
        self,
        workspace_id: str,
        sequence: int,
        steps: Sequence[_CatchUp],
        *,
        existing_directories: bool = False,
    ) -> tuple[SyncResult, tuple[str, ...]]:
        """Apply ordered catch-up steps as consecutive stored-batch journals.

        Each journal is recovered before the next is prepared, so it starts
        from the accepted manifest the previous one confirmed. Returns the
        paths left for a later safe point if a managed writer defers recovery.
        """
        applied: list[str] = []
        merged: list[str] = []
        conflicted: list[str] = []
        later: tuple[str, ...] = ()
        chunks = _chunks(steps)
        for index, chunk in enumerate(chunks):
            request = _request(
                "isolated-source-refresh",
                [self._batch_file(step) for step in chunk],
                ("*",),
                False,
                max_file_bytes=_SOURCE_FILE_BYTES,
                # Chunks keep several files within MAX_BATCH_BYTES; a larger
                # file and its original travel alone.
                max_total_bytes=2 * _SOURCE_FILE_BYTES,
            )
            report = (
                self._prepare(
                    workspace_id, sequence, request, existing_directories=True
                )
                if existing_directories
                else self._prepare(workspace_id, sequence, request)
            )
            self.recover(workspace_id)
            applied.extend(report.applied)
            merged.extend(report.merged)
            conflicted.extend(report.conflicted)
            with self.store.connection() as con:
                waiting = con.execute(
                    "SELECT 1 FROM isolated_sync_journal "
                    "WHERE workspace_id=? AND state='prepared' LIMIT 1",
                    (workspace_id,),
                ).fetchone()
            if waiting is not None:
                later = tuple(
                    step.path for rest in chunks[index + 1 :] for step in rest
                )
                break
        result = SyncResult(tuple(applied), tuple(merged), tuple(conflicted), sequence)
        return result, later

    def _batch_file(self, step: _CatchUp) -> BatchFile:
        if step.directory:
            return BatchFile(step.path, ABSENT, b"", DIRECTORY_MODE)
        base, result = _identity(step.before), _identity(step.after)
        theirs = self._body(step.after)
        original = (
            self._body(step.before)
            if base.kind in {ObjectKind.REGULAR, ObjectKind.SYMLINK}
            else None
        )
        return BatchFile(
            step.path,
            base,
            theirs,
            result.mode,
            original,
            result.permissions,
            bool((step.after or {}).get("binary") or (step.before or {}).get("binary"))
            or _binary(theirs),
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
            rows = con.execute(
                "SELECT path,step_json FROM isolated_sync_conflicts "
                "WHERE workspace_id=?",
                (workspace_id,),
            ).fetchall()
        return {row["path"]: json.loads(row["step_json"]) for row in rows}

    def _prepare(
        self,
        workspace_id: str,
        sequence: int,
        request_json: str,
        *,
        existing_directories: bool = False,
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
                max_bytes=_SOURCE_FILE_BYTES,
            )
            if existing_directories and current.kind is not ObjectKind.DIRECTORY_MARKER:
                # Normalization records an already-existing directory. It must
                # never recreate a subtree removed after the unlocked proof, or
                # store bytes from a private file that replaced that directory.
                raise _OpeningDirectoryChanged(path)
            if ours is not None:
                ensure_safe_content(ours)
            local_hash = (
                store_candidate_content(
                    self.publisher.candidate_dir, ours, max_bytes=_SOURCE_FILE_BYTES
                )
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
                            self.publisher.candidate_dir,
                            merge.content,
                            max_bytes=_SOURCE_FILE_BYTES,
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
        if self.process_writer_notice and self.process_writer_notice(workspace_id):
            return
        with self._authority_pin(workspace_id, recovery=True):
            self._recover(workspace_id)

    def _recover(self, workspace_id: str) -> None:
        with self.publisher.lock:
            if self.process_writer_notice and self.process_writer_notice(workspace_id):
                return
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
                        max_bytes=_SOURCE_FILE_BYTES,
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
                        max_bytes=_SOURCE_FILE_BYTES,
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
                            max_bytes=_SOURCE_FILE_BYTES,
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
                    # Preserve the exact last-step conflict semantics before
                    # retiring history. Artifact hashes and private staging tokens
                    # remain in the complete conflict step until a later step
                    # for this path resolves it.
                    for step in steps:
                        if step["disposition"] == "divergence_preserved":
                            con.execute(
                                "INSERT INTO isolated_sync_conflicts "
                                "(workspace_id,path,step_json) VALUES(?,?,?) "
                                "ON CONFLICT(workspace_id,path) DO UPDATE "
                                "SET step_json=excluded.step_json",
                                (workspace_id, step["path"], _encode(step)),
                            )
                        else:
                            con.execute(
                                "DELETE FROM isolated_sync_conflicts "
                                "WHERE workspace_id=? AND path=?",
                                (workspace_id, step["path"]),
                            )
                    con.execute(
                        "UPDATE isolated_sync_journal SET state='applied',"
                        "manifest_json='{}' WHERE workspace_id=? AND generation=?",
                        (workspace_id, journal["generation"]),
                    )
                    con.execute(
                        "DELETE FROM isolated_sync_journal "
                        "WHERE workspace_id=? AND state='applied' "
                        "AND generation NOT IN (SELECT generation FROM "
                        "isolated_sync_journal WHERE workspace_id=? "
                        "AND state='applied' ORDER BY generation DESC LIMIT ?)",
                        (workspace_id, workspace_id, _APPLIED_JOURNAL_WINDOW),
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
            session = self.store.get_session(session_id)
        if publication.state == "published":
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
                    "SELECT task_id FROM all_task_executions WHERE execution_id=?",
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
