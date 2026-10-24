"""Durable isolated session creation from exact source and private runtime.

Creation never commits the user's dirty tree or writes its index. Every phase
has an intent in the control database. Recovery preserves ambiguous paths and
quarantines an incomplete creation instead of recursively deleting evidence.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import stat
import sys
import tempfile
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from threading import RLock
from typing import Any

from llm_cli.agent.source_policy import (
    _private_path,
    contains_secret_material,
    ensure_safe_content,
    excluded_paths,
    register_runtime_paths,
    runtime_path,
    runtime_path_matches,
)
from llm_cli.config.models import RebuildConfig
from llm_cli.coordination.models import CheckoutRecord, WorkspaceRecord
from llm_cli.coordination.scopes import normalize_changed_path
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.git.worktrees import (
    create_managed_worktree,
    list_git_worktrees,
    prune_managed_worktrees,
    resume_managed_worktree,
)
from llm_cli.ids import new_id
from llm_cli.storage.connection import immediate_transaction
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.broker import validate_symlink_target
from llm_cli.workspace.external import ExternalWorkspaceScanner
from llm_cli.workspace.hydration import (
    HydrationEntry,
    _bounded_git,
    _evidence,
    plan_hydration,
)
from llm_cli.workspace.identity import (
    ABSENT,
    SYMLINK_MODE,
    ObjectKind,
    content_identity,
)

_MAX_SOURCE_FILE = 256 * 1024 * 1024
_LOG = logging.getLogger(__name__)
_ADMIN = frozenset({".git", ".loupe", ".llm-coord", ".codex", ".agents"})


@dataclass(frozen=True, slots=True)
class IsolatedState:
    workspace_id: str
    session_id: str
    base_oid: str
    source_sequence: int
    source_manifest: dict[str, dict[str, Any]]
    runtime_paths: tuple[str, ...]
    runtime_files: tuple[str, ...]
    notices: tuple[str, ...]
    source_fingerprint: str
    creation_token: str
    stage: str


def _error(message: str) -> LlmCoordError:
    return LlmCoordError(ErrorCode.REPOSITORY_UNSAFE, message)


@contextmanager
def _parent(
    root: Path | int, relative: str, *, create: bool = False
) -> Iterator[tuple[int, str]]:
    """Keep every ancestor pinned; no descendant symlink may be followed."""
    normalized = normalize_changed_path(relative)
    if normalized != relative or any(part in _ADMIN for part in Path(relative).parts):
        raise _error("isolated source names an administrative or noncanonical path")
    descriptor = (
        os.dup(root)
        if isinstance(root, int)
        else os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    )
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=descriptor)
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


@contextmanager
def _source(root: Path, relative: str) -> Iterator[int]:
    with _parent(root, relative) as (parent, name):
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise _error("isolated input must be a regular file")
            yield descriptor
        finally:
            os.close(descriptor)


def _read(descriptor: int, limit: int) -> bytes:
    if os.fstat(descriptor).st_size > limit:
        raise _error("isolated source exceeds its file budget")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while chunk := os.read(descriptor, min(1024 * 1024, limit + 1 - total)):
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise _error("isolated input grew beyond its file budget")
    return b"".join(chunks)


def _passive(root: Path, arguments: list[str]) -> bytes:
    code, data = _bounded_git(
        root,
        arguments,
        deadline=time.monotonic() + 30,
        limit=16 * 1024 * 1024,
    )
    if code not in {0, 1}:
        raise _error("could not pin isolated source policy")
    return data


def _source_paths(root: Path) -> tuple[str, ...]:
    raw = _passive(
        root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    )
    paths = tuple(sorted({os.fsdecode(path) for path in raw.split(b"\0") if path}))
    if len(paths) > 20_000:
        raise _error("isolated source exceeds its 20,000-path budget")
    return paths


def _store_source(candidate_dir: Path, body: bytes) -> str:
    ensure_safe_content(body)
    digest = hashlib.sha256(body).hexdigest()
    candidate_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if candidate_dir.is_symlink() or candidate_dir.stat().st_mode & 0o077:
        raise _error("isolated source artifact directory must be private")
    path = candidate_dir / digest
    if path.exists():
        with _source(candidate_dir, digest) as descriptor:
            if _read(descriptor, _MAX_SOURCE_FILE) != body:
                raise _error("isolated source artifact failed its digest check")
        return digest
    temporary = candidate_dir / ("." + new_id("source") + ".tmp")
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(candidate_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()

    return digest


class _SourceChanged(Exception):
    """A source save crossed a no-follow snapshot boundary; retry it later."""


def _capture_path(root: Path, relative: str) -> tuple[bytes, os.stat_result, bool]:
    with _parent(root, relative) as (parent, name):
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        is_link = stat.S_ISLNK(before.st_mode)
        if is_link:
            body = os.fsencode(os.readlink(name, dir_fd=parent))
            destination = validate_symlink_target(root, relative, body)
            if excluded_paths(root, [destination]):
                raise _error("source link points at protected or runtime input")
        else:
            with _source(root, relative) as descriptor:
                if _evidence(before) != _evidence(os.fstat(descriptor)):
                    raise _SourceChanged
                body = _read(descriptor, _MAX_SOURCE_FILE)
                if _evidence(before) != _evidence(os.fstat(descriptor)):
                    raise _SourceChanged
        if _evidence(before) != _evidence(
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        ):
            raise _SourceChanged
    return body, before, is_link


class SourceManifest(dict[str, dict[str, Any]]):
    """A snapshot with path-only deferrals, never failed or screened bytes."""

    deferred: tuple[str, ...] = ()


@dataclass
class SourceCaptureCache:
    """Cache regular source only while its pinned filesystem identity matches."""

    max_entries: int = 40_000
    entries: OrderedDict[tuple[Path, str], tuple[dict[str, int], dict[str, Any]]] = (
        field(default_factory=OrderedDict)
    )
    lock: RLock = field(default_factory=RLock, repr=False)

    def lookup(
        self, key: tuple[Path, str]
    ) -> tuple[dict[str, int], dict[str, Any]] | None:
        with self.lock:
            result = self.entries.get(key)
            if result is not None:
                self.entries.move_to_end(key)
            return result

    def prune(self, root: Path, present: set[str]) -> None:
        with self.lock:
            for key in tuple(self.entries):
                if key[0] == root and key[1] not in present:
                    self.entries.pop(key)

    def remember(
        self, key: tuple[Path, str], status: os.stat_result, entry: dict[str, Any]
    ) -> None:
        with self.lock:
            self.entries[key] = (_evidence(status), dict(entry))
            self.entries.move_to_end(key)
            while len(self.entries) > max(0, self.max_entries):
                self.entries.popitem(last=False)


def capture_source(
    root: Path,
    candidate_dir: Path,
    *,
    protected_paths: Sequence[str] = (),
    cache: SourceCaptureCache | None = None,
    defer_changed: bool = False,
) -> SourceManifest:
    """Capture ordinary current working bytes, including dirty/untracked files.

    Missing tracked files retain an absent identity. Screened source retains
    only its identity: creation may copy it privately, but its bytes never enter
    ordinary artifacts or model-facing proposals.
    """
    manifest = SourceManifest()
    deferred: list[str] = []
    total = 0
    source_paths = _source_paths(root)
    excluded = excluded_paths(root, list(source_paths))
    for relative in source_paths:
        if runtime_path(root, relative):
            continue
        if runtime_path_matches(relative, protected_paths):
            if defer_changed:
                deferred.append(relative)
                continue
            raise _error(
                "runtime path became source; explicit reclassification is required"
            )
        if (
            relative in excluded
            or _private_path(relative)
            or any(part in _ADMIN for part in Path(relative).parts)
        ):
            if defer_changed:
                deferred.append(relative)
                continue
            raise _error(f"protected source cannot be snapshotted: {relative}")
        key = (root, relative)
        cached = cache.lookup(key) if cache is not None else None
        if cached is not None:
            try:
                with _parent(root, relative) as (parent, name):
                    status = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISREG(status.st_mode) and _evidence(status) == cached[0]:
                    manifest[relative] = dict(cached[1])
                    total += int(cached[1]["size"])
                    if total > 1024**3:
                        raise _error("isolated source exceeds its 1 GiB budget")
                    continue
            except OSError:
                pass
        for attempt in range(1 if defer_changed else 3):
            try:
                body, before, is_link = _capture_path(root, relative)
                break
            except _SourceChanged:
                if defer_changed:
                    deferred.append(relative)
                    break
                if attempt == 2:
                    raise _error(
                        f"source kept changing during isolated snapshot: {relative}; "
                        "retry after the current save finishes"
                    ) from None
            except FileNotFoundError:
                manifest[relative] = {
                    "kind": "absent",
                    "mode": ABSENT.mode,
                    "digest": ABSENT.digest,
                    "content_hash": None,
                    "size": 0,
                    "permissions": None,
                }
                break
            except (OSError, LlmCoordError) as exc:
                if defer_changed:
                    deferred.append(relative)
                    break
                if isinstance(exc, LlmCoordError):
                    raise
                raise _error(
                    "source requires a link, submodule, or special adapter: " + relative
                ) from exc
        if relative in manifest or relative in deferred:
            continue
        total += len(body)
        if total > 1024**3:
            raise _error("isolated source exceeds its 1 GiB budget")
        screened = contains_secret_material(body)
        content_hash = None if screened else _store_source(candidate_dir, body)
        kind = ObjectKind.SYMLINK if is_link else ObjectKind.REGULAR
        mode = (
            SYMLINK_MODE
            if is_link
            else ("100755" if before.st_mode & 0o111 else "100644")
        )
        manifest[relative] = {
            "kind": str(kind),
            "mode": mode,
            "digest": content_identity(kind, mode, body),
            "content_hash": content_hash,
            "size": len(body),
            "permissions": None if is_link else stat.S_IMODE(before.st_mode) & 0o777,
            **({"screened": True} if screened else {}),
        }
        if cache is not None and not is_link:
            cache.remember(key, before, manifest[relative])
    manifest.deferred = tuple(deferred)
    if cache is not None:
        cache.prune(root, set(source_paths))
    return manifest


def source_fingerprint(root: Path, policy_paths: tuple[str, ...] = ()) -> str:
    """Pin config origins and full ignore/attribute/exclude file identities."""
    config = _passive(root, ["config", "--null", "--show-origin", "--list"])
    parts = config.split(b"\0")
    paths: set[Path] = set()
    for origin in parts[::2]:
        if origin.startswith(b"file:"):
            path = Path(os.fsdecode(origin[5:])).expanduser()
            paths.add(path if path.is_absolute() else root / path)
    common = run_git(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    assert isinstance(common.stdout, str)
    paths.update(
        {
            Path(common.stdout.strip()) / "info/exclude",
            Path(common.stdout.strip()) / "info/attributes",
        }
    )
    for setting in ("core.excludesfile", "core.attributesfile"):
        value = _passive(root, ["config", "--null", "--path", "--get", setting]).rstrip(
            b"\0"
        )
        if value:
            path = Path(os.fsdecode(value)).expanduser()
            paths.add(path if path.is_absolute() else root / path)
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    paths.update({config_home / "git/ignore", config_home / "git/attributes"})
    paths.update(root / path for path in policy_paths)
    paths.update(
        root / path
        for path in _source_paths(root)
        if Path(path).name in {".gitignore", ".gitattributes"}
    )
    digest = hashlib.sha256(config)
    for path in sorted(paths):
        digest.update(os.fsencode(path))
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            digest.update(b"absent")
            continue
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise _error("source policy file is not regular")
            body = _read(descriptor, 4 * 1024 * 1024)
            if _evidence(before) != _evidence(os.fstat(descriptor)) or _evidence(
                before
            ) != _evidence(path.lstat()):
                raise _error("source policy changed during capture")
            digest.update(json.dumps(_evidence(before), sort_keys=True).encode())
            digest.update(body)
        finally:
            os.close(descriptor)
    return digest.hexdigest()


@cache
def _darwin_clone() -> Any:
    libc = ctypes.CDLL(None, use_errno=True)
    clone = getattr(libc, "fclonefileat", None)
    if clone is not None:
        clone.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        clone.restype = ctypes.c_int
    return clone


def _clone(source_fd: int, destination_parent: int, name: str) -> bool:
    """Try a descriptor-based clone; no hardlinks or shared directory links."""
    if sys.platform == "darwin":
        clone = _darwin_clone()
        return (
            clone is not None
            and clone(source_fd, destination_parent, os.fsencode(name), 0) == 0
        )
    if sys.platform.startswith("linux"):
        import fcntl

        target = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=destination_parent,
        )
        try:
            try:
                fcntl.ioctl(target, 0x40049409, source_fd)
                return True
            except OSError:
                os.unlink(name, dir_fd=destination_parent)
                return False
        finally:
            os.close(target)
    return False


def _write_body(parent: int, name: str, body: bytes, permissions: int) -> None:
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
    )
    with os.fdopen(descriptor, "wb") as handle:
        # Preserve sparse zero chunks without ever sharing mutable blocks.
        for start in range(0, len(body), 1024 * 1024):
            chunk = body[start : start + 1024 * 1024]
            if chunk and not chunk.strip(b"\0"):
                handle.seek(len(chunk), os.SEEK_CUR)
            else:
                handle.write(chunk)
        handle.truncate(len(body))
        handle.flush()
        os.fchmod(handle.fileno(), permissions)
        os.fsync(handle.fileno())
    os.fsync(parent)


def copy_runtime_file(source: Path, target: Path, entry: HydrationEntry) -> None:
    """Copy one authorized file, compare stable bytes, persist no raw digest."""
    with _source(source, entry.path) as descriptor:
        before = os.fstat(descriptor)
        if _evidence(before) != entry.observation:
            raise _error("runtime changed since its hydration inventory")
        body = _read(descriptor, _MAX_SOURCE_FILE)
        with _parent(target, entry.path, create=True) as (parent, name):
            cloned = False
            if (
                entry.action == "clone_or_copy"
                and os.fstat(parent).st_dev == before.st_dev
            ):
                # Probe only daemon-owned bytes on the actual device pair.
                # A broken clone implementation must never corrupt user input.
                with tempfile.TemporaryDirectory(
                    prefix=".loupe-cow-", dir=target
                ) as temporary:
                    probe_root = Path(temporary)
                    probe_source = probe_root / "source"
                    probe_source.write_bytes(b"loupe private clone probe")
                    with _source(probe_root, "source") as probe_source_fd:
                        probe_parent = os.open(
                            probe_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                        )
                        try:
                            if _clone(probe_source_fd, probe_parent, "target"):
                                (probe_root / "target").write_bytes(
                                    b"changed destination"
                                )
                                if (
                                    _read(probe_source_fd, 1024)
                                    != b"loupe private clone probe"
                                ):
                                    raise _error(
                                        "filesystem clone did not isolate writes"
                                    )
                                cloned = _clone(descriptor, parent, name)
                        finally:
                            os.close(probe_parent)
            permissions = stat.S_IMODE(before.st_mode) & (
                0o600 if entry.action == "private_copy" else 0o700
            )
            if cloned:
                copied = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    os.fchmod(copied, permissions)
                    if _read(copied, _MAX_SOURCE_FILE) != body:
                        raise _error("runtime clone did not match its source")
                    os.fsync(copied)
                finally:
                    os.close(copied)
                os.fsync(parent)
            else:
                _write_body(parent, name, body, permissions)
        if (
            _evidence(before) != _evidence(os.fstat(descriptor))
            or _read(descriptor, _MAX_SOURCE_FILE) != body
        ):
            raise _error("runtime changed while hydration copied it")
        with _parent(source, entry.path) as (parent, name):
            if _evidence(before) != _evidence(
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            ):
                raise _error("runtime path changed while hydration copied it")


def copy_pinned_runtime_file(
    source: Path,
    target: Path,
    relative: str,
    *,
    copy_observer: Callable[[str, os.stat_result, os.stat_result], None] | None = None,
) -> None:
    """Copy a pinned runtime input privately, independent of later ignore edits."""
    from llm_cli.workspace.runtime import copy_dependency_tree

    with _parent(source, relative) as (parent, name):
        status = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISDIR(status.st_mode):
        copy_dependency_tree(
            source, target, relative, pinned_private=True, copy_observer=copy_observer
        )
        return
    with _source(source, relative) as descriptor:
        before = os.fstat(descriptor)
        body = _read(descriptor, _MAX_SOURCE_FILE)
        with _parent(target, relative, create=True) as (parent, name):
            _write_body(parent, name, body, stat.S_IMODE(before.st_mode) & 0o700)
        if _evidence(before) != _evidence(os.fstat(descriptor)):
            raise _error("isolated runtime changed during command snapshot")
        with _parent(source, relative) as (parent, name):
            if _evidence(before) != _evidence(
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            ):
                raise _error("isolated runtime path changed during command snapshot")


def copy_runtime_files(source: Path, target: Path, paths: tuple[str, ...]) -> None:
    """Re-copy explicitly hydrated files into a private disposable command tree."""
    register_runtime_paths(target, paths)
    for relative in paths:
        copy_pinned_runtime_file(source, target, relative)


class IsolatedWorkspaceManager:
    """Session-owned linked worktrees with conservative crash preservation."""

    def __init__(
        self,
        store: ControlStore,
        managed_root: Path,
        candidate_dir: Path,
        publication_lock: RLock,
        ensure_available: Callable[[str], None],
        *,
        rebuild: tuple[RebuildConfig, ...] = (),
        rebuild_protected: tuple[Path, ...] = (),
    ) -> None:
        self.store = store
        self.managed_root = managed_root
        self.candidate_dir = candidate_dir
        self.lock = publication_lock
        self.ensure_available = ensure_available
        self.rebuild = rebuild
        self.rebuild_protected = rebuild_protected
        self._opening_sessions: set[str] = set()
        self.fault: Callable[[str], None] = lambda stage: None
        from llm_cli.workspace.runtime_refresh import RuntimeRefresher

        self.runtime = RuntimeRefresher(store, managed_root, publication_lock)

    def get(self, workspace_id: str) -> IsolatedState | None:
        with self.store.connection() as con:
            row = con.execute(
                "SELECT * FROM isolated_workspaces WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
        if row is None:
            return None
        return IsolatedState(
            workspace_id,
            str(row["session_id"]),
            str(row["base_oid"]),
            int(row["source_sequence"]),
            json.loads(row["source_manifest_json"]),
            tuple(json.loads(row["runtime_paths_json"])),
            tuple(json.loads(row["runtime_files_json"])),
            tuple(json.loads(row["notices_json"])),
            str(row["source_fingerprint"]),
            str(row["creation_token"]),
            str(row["stage"]),
        )

    def _stage(self, workspace_id: str, stage: str) -> None:
        with self.store.connection() as con, immediate_transaction(con):
            con.execute(
                "UPDATE isolated_workspaces SET stage=? WHERE workspace_id=?",
                (stage, workspace_id),
            )
        self.fault(stage)

    def _quarantine(self, workspace_id: str, *, closed: bool = False) -> None:
        with self.store.connection() as con, immediate_transaction(con):
            con.execute(
                "UPDATE isolated_workspaces SET stage='attention' WHERE workspace_id=?",
                (workspace_id,),
            )
            con.execute(
                "UPDATE workspaces SET state='quarantined', quarantine_reason=?, "
                "state_version=state_version+1 WHERE workspace_id=?",
                (
                    (
                        "closed isolated workspace needs cleanup; files preserved "
                        "in worktree or "
                        if closed
                        else "isolated creation interrupted; files preserved "
                        "in worktree or "
                    )
                    + str(self.managed_root / (".recovery-" + workspace_id)),
                    workspace_id,
                ),
            )

    def validate(self, workspace_id: str) -> IsolatedState:
        state = self.get(workspace_id)
        workspace = self.store.get_workspace(workspace_id)
        if (
            state is None
            or workspace is None
            or state.stage != "ready"
            or workspace.state != "active"
        ):
            raise _error("isolated workspace needs recovery; its files were preserved")
        checkout = self.store.get_checkout(workspace.checkout_id)
        assert checkout is not None
        managed = resume_managed_worktree(
            Path(checkout.canonical_path),
            managed_root=self.managed_root,
            registered_path=Path(workspace.canonical_path),
            task_id=workspace_id,
            base_oid=state.base_oid,
        )
        marker = managed.git_dir / "loupe-isolated-owner"
        with _source(managed.git_dir, marker.name) as descriptor:
            if _read(descriptor, 1024).decode() != state.creation_token:
                raise _error("isolated worktree ownership changed")
        self.runtime.recover(workspace_id)
        state = self.get(workspace_id)
        assert state is not None
        register_runtime_paths(managed.path, state.runtime_paths)
        return state

    def _creation_file_matches(
        self,
        root: Path,
        relative: str,
        state: IsolatedState,
        primary: Path,
    ) -> bool:
        expected = state.source_manifest.get(relative)
        if expected is not None and expected["kind"] == "symlink":
            with _parent(root, relative) as (parent, leaf):
                body = os.fsencode(os.readlink(leaf, dir_fd=parent))
        else:
            with _source(root, relative) as descriptor:
                body = _read(descriptor, _MAX_SOURCE_FILE)
        if expected is not None:
            return bool(
                expected["digest"]
                == content_identity(
                    ObjectKind(expected["kind"]), expected["mode"], body
                )
            )
        if relative in state.runtime_files:
            with _source(primary, relative) as descriptor:
                return _read(descriptor, _MAX_SOURCE_FILE) == body
        return False

    @staticmethod
    def _cleanup_files(
        root: Path, known: set[str], *, git_pointer: bool = False
    ) -> tuple[str, ...]:
        parents = {
            parent.as_posix()
            for relative in known
            for parent in Path(relative).parents
            if parent != Path(".")
        }
        result: list[str] = []
        for directory, directories, files in os.walk(root, followlinks=False):
            base = Path(directory)
            for name in list(directories):
                relative = (base / name).relative_to(root).as_posix()
                if (base / name).is_symlink():
                    files.append(name)
                    directories.remove(name)
                elif relative not in parents:
                    raise _error("cleanup found an unknown directory; preserving it")
            for name in files:
                relative = (base / name).relative_to(root).as_posix()
                if git_pointer and relative == ".git":
                    continue
                if relative not in known:
                    raise _error("cleanup found an unknown file; preserving it")
                result.append(relative)
        return tuple(sorted(result))

    @staticmethod
    def _remove_private_entry(parent: int, name: str) -> None:
        """Unlink disposable private material without traversing any symlink."""
        try:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(before.st_mode):
            os.unlink(name, dir_fd=parent)
        else:
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            try:
                pinned = os.fstat(child)
                if (before.st_dev, before.st_ino) != (pinned.st_dev, pinned.st_ino):
                    raise _error("private cleanup directory changed; preserving it")
                for entry in os.listdir(child):
                    IsolatedWorkspaceManager._remove_private_entry(child, entry)
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                    raise _error("private cleanup directory changed; preserving it")
                os.rmdir(name, dir_fd=parent)
            finally:
                os.close(child)

    def _private_material(self, state: IsolatedState) -> set[str]:
        paths = (
            set(state.runtime_paths)
            | set(state.runtime_files)
            | {
                relative
                for relative, entry in state.source_manifest.items()
                if entry.get("screened")
            }
        )
        with self.store.connection() as con:
            if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='isolated_sync_state'"
            ).fetchone():
                synced = con.execute(
                    "SELECT manifest_json,runtime_paths_json FROM isolated_sync_state "
                    "WHERE workspace_id=?",
                    (state.workspace_id,),
                ).fetchone()
                if synced:
                    paths.update(json.loads(synced["runtime_paths_json"]))
                    paths.update(
                        relative
                        for relative, entry in json.loads(
                            synced["manifest_json"]
                        ).items()
                        if entry.get("screened")
                    )
        roots: list[str] = []
        for relative in sorted(paths):
            if roots and relative.startswith(roots[-1] + "/"):
                continue
            roots.append(relative)
        return set(roots)

    def _scrub_private(self, root: Path, state: IsolatedState) -> None:
        # These paths were privately materialized by Loupe. They are disposable
        # runtime, never recovery evidence or undo history. Screened source is
        # likewise kept out of the ordinary source recovery archive.
        paths = self._private_material(state)
        for relative in sorted(paths, key=lambda item: (len(Path(item).parts), item)):
            try:
                with _parent(root, relative) as (parent, leaf):
                    self._remove_private_entry(parent, leaf)
                    os.fsync(parent)
            except FileNotFoundError:
                pass

    def _scrub_removed_archives(self) -> None:
        with self.store.connection() as con:
            rows = con.execute(
                "SELECT workspace_id,canonical_path,checkout_id FROM workspaces WHERE "
                "mode='isolated' AND state='removed'"
            ).fetchall()
        for row in rows:
            workspace_id = str(row["workspace_id"])
            expected = self.managed_root.resolve() / workspace_id
            if Path(row["canonical_path"]) != expected:
                continue
            archive = self.managed_root / (".recovery-" + workspace_id)
            if not archive.exists() and not archive.is_symlink():
                continue
            try:
                checkout = self.store.get_checkout(str(row["checkout_id"]))
                if checkout is None:
                    continue
                from llm_cli.workspace.authority import managed_pin

                with managed_pin(self.store, Path(checkout.canonical_path)):
                    if archive.is_symlink() or archive.stat().st_mode & 0o077:
                        continue
                    with _source(archive, "owner.json") as descriptor:
                        owner = json.loads(_read(descriptor, 32 * 1024 * 1024))
                    if not isinstance(owner, dict):
                        continue
                    if not isinstance(owner.get("token"), str) or not owner[
                        "token"
                    ].startswith("owner_"):
                        continue
                    private_paths = owner.get("private_paths", [])
                    if not isinstance(private_paths, list) or any(
                        not isinstance(relative, str)
                        or normalize_changed_path(relative) != relative
                        or any(part in _ADMIN for part in Path(relative).parts)
                        for relative in private_paths
                    ):
                        continue
                    paths = set(private_paths)
                    data = archive / "files"
                    if data.is_symlink():
                        continue
                    # Older archives predate the pinned-path owner metadata. Only
                    # unmistakably private names can be retired without guessing
                    # which ordinary file was copied.
                    for directory, directories, files in os.walk(
                        data, followlinks=False
                    ):
                        base = Path(directory)
                        for name in list(directories) + files:
                            relative = (base / name).relative_to(data).as_posix()
                            if _private_path(relative):
                                paths.add(relative)
                                if name in directories:
                                    directories.remove(name)
                    for relative in sorted(
                        paths, key=lambda item: (len(Path(item).parts), item)
                    ):
                        with (
                            suppress(FileNotFoundError),
                            _parent(data, relative) as (parent, leaf),
                        ):
                            self._remove_private_entry(parent, leaf)
                            os.fsync(parent)
            except (LlmCoordError, OSError, TypeError, ValueError):
                # A corrupt or replaced archive is evidence, not authority to
                # remove an arbitrary path.
                continue

    def _cleanup_needed(self, workspace_id: str, session_id: str) -> bool:
        if self.runtime.busy(workspace_id):
            return False
        with self.store.connection() as con:
            session = con.execute(
                "SELECT state FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if session is None or session[0] != "closed":
                return False
            if con.execute(
                "SELECT 1 FROM tasks WHERE session_id=? AND "
                "state NOT IN ('completed','failed','cancelled') LIMIT 1",
                (session_id,),
            ).fetchone():
                return False
            if con.execute(
                "SELECT 1 FROM task_workflows w JOIN tasks t ON t.task_id=w.task_id "
                "WHERE t.session_id=? AND w.proposal_json IS NOT NULL AND "
                "w.status NOT IN ('published','undone','discarded') LIMIT 1",
                (session_id,),
            ).fetchone():
                return False
            if con.execute(
                "SELECT 1 FROM all_task_executions e "
                "JOIN tasks t ON t.task_id=e.task_id "
                "WHERE t.session_id=? AND "
                "e.state NOT IN ('published','failed') LIMIT 1",
                (session_id,),
            ).fetchone():
                return False
            if (
                con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='isolated_sync_journal'"
                ).fetchone()
                and con.execute(
                    "SELECT 1 FROM isolated_sync_journal WHERE workspace_id=? "
                    "AND state='prepared' LIMIT 1",
                    (workspace_id,),
                ).fetchone()
            ):
                return False
            return (
                con.execute(
                    "SELECT 1 FROM workspace_batches WHERE workspace_id=? AND "
                    "state IN ('applying','operator_attention') LIMIT 1",
                    (workspace_id,),
                ).fetchone()
                is None
            )

    def _orphaned_creation(self, state: IsolatedState) -> bool:
        """An unregistered creation must have no durable runtime/source users."""
        workspace_id = state.workspace_id
        with self.store.connection() as con:
            references = (
                (
                    "SELECT 1 FROM sessions WHERE session_id=? OR workspace_id=? "
                    "LIMIT 1",
                    (state.session_id, workspace_id),
                ),
                (
                    "SELECT 1 FROM tasks WHERE session_id=? LIMIT 1",
                    (state.session_id,),
                ),
                (
                    "SELECT 1 FROM all_task_executions WHERE workspace_id=? LIMIT 1",
                    (workspace_id,),
                ),
                (
                    "SELECT 1 FROM isolated_runtime_holds WHERE workspace_id=? LIMIT 1",
                    (workspace_id,),
                ),
                (
                    "SELECT 1 FROM workspace_candidates WHERE workspace_id=? LIMIT 1",
                    (workspace_id,),
                ),
                (
                    "SELECT 1 FROM workspace_batches WHERE workspace_id=? LIMIT 1",
                    (workspace_id,),
                ),
                (
                    "SELECT 1 FROM workspace_publications WHERE workspace_id=? LIMIT 1",
                    (workspace_id,),
                ),
                (
                    "SELECT 1 FROM isolated_sync_journal WHERE workspace_id=? "
                    "AND state='prepared' LIMIT 1",
                    (workspace_id,),
                ),
                (
                    "SELECT 1 FROM isolated_runtime_journal WHERE workspace_id=? "
                    "AND state='prepared' LIMIT 1",
                    (workspace_id,),
                ),
            )
            return not any(
                con.execute(sql, parameters).fetchone()
                for sql, parameters in references
            )

    def _retire_failed_creation(self, workspace_id: str) -> bool:
        # Preserve the caller's original error if cleanup itself fails. Git and
        # token verification must precede every private-file removal; unknown
        # ordinary source stays available in the quarantined tree or archive.
        try:
            state = self.get(workspace_id)
            if state is not None and self._orphaned_creation(state):
                return self._clean_incomplete_owned(workspace_id)
        except Exception:
            _LOG.warning(
                "failed isolated creation could not be cleaned; "
                "its private copies remain quarantined",
                exc_info=True,
            )
        return False

    def cleanup_closed(self, session_id: str | None = None) -> None:
        """Retire closed sessions only after proposals and live actions release them."""
        with self.lock:
            with self.store.connection() as con:
                rows = con.execute(
                    "SELECT workspace_id,session_id FROM isolated_workspaces "
                    "WHERE stage='ready'" + (" AND session_id=?" if session_id else ""),
                    (session_id,) if session_id else (),
                ).fetchall()
            for row in rows:
                workspace_id = str(row["workspace_id"])
                if not self._cleanup_needed(workspace_id, str(row["session_id"])):
                    continue

                try:
                    if not self._clean_incomplete(workspace_id, closed=True):
                        self._quarantine(workspace_id, closed=True)
                except (LlmCoordError, OSError, ValueError) as exc:
                    if isinstance(exc, LlmCoordError) and exc.code in {
                        ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE,
                        ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED,
                        ErrorCode.CHECKOUT_IDENTITY_CHANGED,
                    }:
                        continue
                    self._quarantine(workspace_id, closed=True)

    def _clean_incomplete(self, workspace_id: str, *, closed: bool = False) -> bool:
        authority = self.store.authority
        if authority is None:
            return self._clean_incomplete_owned(workspace_id, closed=closed)
        workspace = self.store.get_workspace(workspace_id)
        state = self.get(workspace_id)
        checkout = self.store.get_checkout(workspace.checkout_id) if workspace else None
        if state is None or checkout is None:
            return False
        primary = Path(checkout.canonical_path)
        # Match batch/runtime publication ordering before entering authority.
        with self.lock, authority.pin(primary, recovery=True) as pin:
            kind, reference = "isolated_cleanup", workspace_id
            if pin.owner.marker["state"] != "clean":
                with self.store.connection() as con:
                    journal = con.execute(
                        "SELECT kind,reference_id FROM authority_journals WHERE "
                        "journal_id=?",
                        (pin.owner.marker["journal_id"],),
                    ).fetchone()
                if journal is not None and tuple(journal) == (
                    "isolated_create",
                    state.session_id,
                ):
                    kind, reference = "isolated_create", state.session_id
            with authority.mutation(
                primary, kind, reference, recovery=True
            ) as operation:
                result = self._clean_incomplete_owned(workspace_id, closed=closed)
                if result:
                    operation.confirm()
                return result

    def _clean_incomplete_owned(
        self, workspace_id: str, *, closed: bool = False
    ) -> bool:
        if closed:
            with self.store.connection() as con:
                con.execute(
                    "UPDATE workspaces SET "
                    "state='cleaning',state_version=state_version+1 WHERE "
                    "workspace_id=?",
                    (workspace_id,),
                )
        state = self.get(workspace_id)
        workspace = self.store.get_workspace(workspace_id)
        assert state is not None and workspace is not None
        path = Path(workspace.canonical_path)
        checkout = self.store.get_checkout(workspace.checkout_id)
        assert checkout is not None
        primary = Path(checkout.canonical_path)
        archive = self.managed_root / (".recovery-" + workspace_id)
        archive_data = archive / "files"
        owner: dict[str, Any] | None = None
        if archive.exists() or archive.is_symlink():
            with _source(archive, "owner.json") as descriptor:
                owner = json.loads(_read(descriptor, 32 * 1024 * 1024))
            if (
                not isinstance(owner, dict)
                or owner.get("token") != state.creation_token
                or not isinstance(owner.get("root_identity"), list)
                or len(owner["root_identity"]) != 2
            ):
                return False
            self._scrub_private(archive_data, state)
        if path.exists() or path.is_symlink():
            root_status = path.lstat()
            if owner is not None:
                if [root_status.st_dev, root_status.st_ino] != owner["root_identity"]:
                    return False
                # A crash can happen after removing the Git pointer but before
                # rmdir. Restore only in the same pinned directory inode.
                if not (path / ".git").exists():
                    with _source(archive, "git-pointer") as descriptor:
                        pointer = _read(descriptor, 4096)
                    root_fd = os.open(
                        path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    )
                    try:
                        _write_body(root_fd, ".git", pointer, 0o600)
                    finally:
                        os.close(root_fd)
            managed = resume_managed_worktree(
                primary,
                managed_root=self.managed_root,
                registered_path=path,
                task_id=workspace_id,
                base_oid=state.base_oid,
            )
            try:
                with _source(managed.git_dir, "loupe-isolated-owner") as descriptor:
                    if _read(descriptor, 1024).decode() != state.creation_token:
                        return False
            except FileNotFoundError:
                # The locked registration itself can prove an otherwise empty
                # worktree in the add-return / private-marker crash window.
                record = next(
                    (item for item in list_git_worktrees(primary) if item.path == path),
                    None,
                )
                if (
                    state.stage != "planned"
                    or record is None
                    or record.lock_reason != "llm-coord:" + workspace_id
                    or set(os.listdir(path)) != {".git"}
                ):
                    return False
            self._scrub_private(path, state)
            if not closed and self._orphaned_creation(state):
                # Known runtime generations are disposable too, even when an
                # unknown source edit prevents retiring the worktree itself.
                self.runtime.cleanup(workspace_id)
            known = set(state.source_manifest) | set(state.runtime_files)
            if closed:
                with self.store.connection() as con:
                    known.update(
                        str(row[0])
                        for row in con.execute(
                            "SELECT relative_path FROM workspace_candidates "
                            "WHERE workspace_id=?",
                            (workspace_id,),
                        )
                    )
                    if con.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='isolated_sync_state'"
                    ).fetchone():
                        synced = con.execute(
                            "SELECT manifest_json FROM isolated_sync_state "
                            "WHERE workspace_id=?",
                            (workspace_id,),
                        ).fetchone()
                        if synced:
                            known.update(json.loads(synced[0]))
            originals = self._cleanup_files(path, known, git_pointer=True)
            archived = self._cleanup_files(archive_data, known) if owner else ()
            if set(originals) & set(archived):
                return False
            if not closed and not all(
                self._creation_file_matches(path, item, state, primary)
                for item in originals
            ):
                return False
            if not closed and not all(
                self._creation_file_matches(archive_data, item, state, primary)
                for item in archived
            ):
                return False
            root_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                pinned = os.fstat(root_fd)
                if (pinned.st_dev, pinned.st_ino) != (
                    root_status.st_dev,
                    root_status.st_ino,
                ):
                    return False
                pointer_fd = os.open(
                    ".git", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd
                )
                try:
                    pointer = _read(pointer_fd, 4096)
                finally:
                    os.close(pointer_fd)
                if owner is None:
                    archive.mkdir(mode=0o700)
                    archive_data.mkdir(mode=0o700)
                    owner = {
                        "token": state.creation_token,
                        "root_identity": [root_status.st_dev, root_status.st_ino],
                        "private_paths": sorted(self._private_material(state)),
                    }
                    archive_fd = os.open(
                        archive, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    )
                    try:
                        _write_body(
                            archive_fd, "owner.json", json.dumps(owner).encode(), 0o600
                        )
                        _write_body(archive_fd, "git-pointer", pointer, 0o600)
                    finally:
                        os.close(archive_fd)
                self.fault("cleanup_verified")
                for relative in originals:
                    with (
                        _parent(root_fd, relative) as (source_parent, leaf),
                        _parent(archive_data, relative, create=True) as (
                            destination_parent,
                            destination_leaf,
                        ),
                    ):
                        # The archive is private and the destination is new.
                        # Never overwrite an earlier recovered copy.
                        try:
                            os.stat(
                                destination_leaf,
                                dir_fd=destination_parent,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            pass
                        else:
                            return False
                        os.rename(
                            leaf,
                            destination_leaf,
                            src_dir_fd=source_parent,
                            dst_dir_fd=destination_parent,
                        )
                        os.fsync(source_parent)
                        os.fsync(destination_parent)
                    self.fault("cleanup_archived")
                    if not closed and not self._creation_file_matches(
                        archive_data, relative, state, primary
                    ):
                        return False
                # Retain archived bytes, including any late write through an
                # already-open descriptor. Cleanup only removes empty names.
                parents = {
                    parent
                    for relative in known
                    for parent in Path(relative).parents
                    if parent != Path(".")
                }
                for parent in sorted(
                    parents, key=lambda item: len(item.parts), reverse=True
                ):
                    with (
                        suppress(FileNotFoundError),
                        _parent(root_fd, parent.as_posix()) as (parent_fd, leaf),
                    ):
                        os.rmdir(leaf, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                if set(os.listdir(root_fd)) != {".git"}:
                    return False
                current = path.lstat()
                if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                    return False
                os.unlink(".git", dir_fd=root_fd)
                try:
                    path.rmdir()
                except OSError:
                    _write_body(root_fd, ".git", pointer, 0o600)
                    return False
            finally:
                os.close(root_fd)
        # Pruning only touches missing managed registrations, never an existing
        # directory created by a user after rmdir. Foreign stale entries abort.
        prune_managed_worktrees(primary, managed_root=self.managed_root)
        self.runtime.cleanup(workspace_id)
        with self.store.connection() as con, immediate_transaction(con):
            con.execute(
                "UPDATE workspaces SET state='removed',state_version=state_version+1,"
                "quarantine_reason=? WHERE workspace_id=?",
                (
                    (
                        "Closed session source evidence retained at "
                        if closed
                        else "Interrupted creation output retained at "
                    )
                    + str(archive),
                    workspace_id,
                ),
            )
            con.execute(
                "DELETE FROM isolated_workspaces WHERE workspace_id=?", (workspace_id,)
            )
        return True

    @contextmanager
    def _opening(self, session_id: str) -> Iterator[None]:
        with self.lock:
            if session_id in self._opening_sessions:
                raise _error("isolated session setup is already running")
            self._opening_sessions.add(session_id)
        try:
            yield
        finally:
            with self.lock:
                self._opening_sessions.discard(session_id)

    def recover(self) -> None:
        with self.lock:
            self._scrub_removed_archives()
            self.cleanup_closed()
            with self.store.connection() as con:
                rows = con.execute(
                    "SELECT workspace_id,stage FROM isolated_workspaces"
                ).fetchall()
        for row in rows:
            workspace_id = str(row["workspace_id"])
            try:
                workspace = self.store.get_workspace(workspace_id)
                checkout = (
                    self.store.get_checkout(workspace.checkout_id)
                    if workspace else None
                )
                state = self.get(workspace_id)
                if checkout is None or state is None:
                    continue
                with self.lock:
                    state = self.get(workspace_id)
                    if state is None or state.session_id in self._opening_sessions:
                        continue
                    if (
                        state.stage == "attention"
                        and not self._orphaned_creation(state)
                    ):
                        continue
                from llm_cli.workspace.authority import managed_pin

                with managed_pin(
                    self.store, Path(checkout.canonical_path), recovery=True
                ):
                    if state.stage == "ready":
                        with self.lock:
                            self.validate(workspace_id)
                    elif state.stage in {
                        "materialized", "rebuilding", "hydrated",
                    } and self._recover_rebuild(workspace_id):
                        pass
                    else:
                        with self.lock:
                            if not self._clean_incomplete(workspace_id):
                                self._quarantine(workspace_id)
            except (LlmCoordError, OSError, ValueError) as exc:
                if isinstance(exc, LlmCoordError) and exc.code in {
                    ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE,
                    ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED,
                    ErrorCode.CHECKOUT_IDENTITY_CHANGED,
                }:
                    continue
                with self.lock:
                    self._quarantine(workspace_id)

    def _verify_rebuild_owner(
        self, state: IsolatedState, workspace: WorkspaceRecord,
        git_dir: Path, primary: Path,
    ) -> None:
        if state.stage not in {"materialized", "rebuilding", "hydrated"}:
            raise _error("dependency rebuild state changed before finalization")
        managed = resume_managed_worktree(
            primary,
            managed_root=self.managed_root,
            registered_path=Path(workspace.canonical_path),
            task_id=state.workspace_id,
            base_oid=state.base_oid,
        )
        if managed.git_dir != git_dir:
            raise _error("dependency rebuild administration changed")
        with _source(git_dir, "loupe-isolated-owner") as descriptor:
            if _read(descriptor, 1024).decode() != state.creation_token:
                raise _error("dependency rebuild ownership changed")

    def _activate(self, workspace_id: str) -> None:
        state = self.get(workspace_id)
        workspace = self.store.get_workspace(workspace_id)
        assert state is not None and workspace is not None
        self._stage(workspace_id, "hydrated")
        register_runtime_paths(Path(workspace.canonical_path), state.runtime_paths)
        with self.store.connection() as con, immediate_transaction(con):
            con.execute(
                "UPDATE workspaces SET state='active',state_version=state_version+1,"
                "last_verified_at=? WHERE workspace_id=?",
                (self.store._now(None), workspace_id),
            )
            con.execute(
                "UPDATE isolated_workspaces SET stage='ready' WHERE workspace_id=?",
                (workspace_id,),
            )
        self.fault("ready")

    def _complete_rebuild(
        self, workspace_id: str, git_dir: Path, primary: Path,
        *, recovery: bool = False,
    ) -> None:
        from llm_cli.workspace.authority import managed_mutation
        from llm_cli.workspace.rebuild import discard_interrupted, run_pending

        state = self.get(workspace_id)
        workspace = self.store.get_workspace(workspace_id)
        assert state is not None and workspace is not None
        private = Path(workspace.canonical_path)
        try:
            with self.lock, managed_mutation(
                self.store, primary, "isolated_create", state.session_id,
                recovery=recovery,
            ) as operation:
                self._verify_rebuild_owner(state, workspace, git_dir, primary)
                register_runtime_paths(private, state.runtime_paths)
                discard_interrupted(private, git_dir, state.creation_token)
                if capture_source(private, self.candidate_dir) != state.source_manifest:
                    raise _error(
                        "isolated source changed before dependency rebuild recovery"
                    )
                with self.store.connection() as con:
                    con.execute(
                        "UPDATE isolated_workspaces SET stage='rebuilding' "
                        "WHERE workspace_id=?", (workspace_id,),
                    )
                if operation is not None:
                    operation.confirm()
            # Only the ordinary ownership pin survives here. No publication or
            # authority mutation lock is held while the private installer runs.
            self.fault("rebuilding")
            successful, notices = run_pending(
                private, primary, git_dir, state.creation_token,
                runtime_files=state.runtime_files,
                source_paths=tuple(
                    path for path, entry in state.source_manifest.items()
                    if entry.get("screened")
                ),
                fault=self.fault,
                protected=self.rebuild_protected,
            )
            with self.lock, managed_mutation(
                self.store, primary, "isolated_create", state.session_id,
                recovery=recovery,
            ) as operation:
                current = self.get(workspace_id)
                if (
                    current is None or current.stage != "rebuilding"
                    or current.creation_token != state.creation_token
                    or current.source_manifest != state.source_manifest
                ):
                    raise _error("dependency rebuild state changed during installation")
                self._verify_rebuild_owner(current, workspace, git_dir, primary)
                if capture_source(private, self.candidate_dir) != state.source_manifest:
                    raise _error("dependency installer changed isolated source")
                with self.store.connection() as con, immediate_transaction(con):
                    con.execute(
                        "UPDATE isolated_workspaces SET "
                        "runtime_files_json=?,notices_json=? WHERE workspace_id=?",
                        (
                            json.dumps(
                                sorted(set(state.runtime_files) | set(successful))
                            ),
                            json.dumps(
                                tuple(dict.fromkeys((*state.notices, *notices)))
                            ),
                            workspace_id,
                        ),
                    )
                # Preserve opening consent and copied-runtime generation zero.
                self.runtime.finish_creation(workspace_id, successful)
                self._stage(workspace_id, "hydrated")
                self.fault("rebuilt")
                self._activate(workspace_id)
                if operation is not None:
                    operation.confirm()
        except Exception:
            # Finish only this exact failed operation; an ordinary pin must
            # refuse the dirty marker left by its failed validation phase.
            with self.lock, managed_mutation(
                self.store, primary, "isolated_create", state.session_id,
                recovery=True,
            ) as operation:
                if not self._retire_failed_creation(workspace_id):
                    self._quarantine(workspace_id)
                if operation is not None:
                    operation.confirm()
            raise

    def _recover_rebuild(self, workspace_id: str) -> bool:
        from llm_cli.workspace.rebuild import read_journal

        state = self.get(workspace_id)
        workspace = self.store.get_workspace(workspace_id)
        assert state is not None and workspace is not None
        checkout = self.store.get_checkout(workspace.checkout_id)
        assert checkout is not None and workspace.git_dir is not None
        primary = Path(checkout.canonical_path)
        git_dir = Path(workspace.git_dir)
        self._verify_rebuild_owner(state, workspace, git_dir, primary)
        if read_journal(git_dir, state.creation_token) is None:
            return False
        with self._opening(state.session_id):
            self._complete_rebuild(workspace_id, git_dir, primary, recovery=True)
        return True

    def open(
        self,
        checkout: CheckoutRecord,
        session_id: str,
        *,
        approved_secrets: tuple[str, ...] = (),
    ) -> tuple[WorkspaceRecord, tuple[str, ...]]:
        from llm_cli.workspace.authority import managed_mutation, managed_pin

        primary = Path(checkout.canonical_path)
        with self._opening(session_id), managed_pin(self.store, primary):
            # Reusing a session ID must never select another checkout's private
            # workspace, even before a daemon session row has been registered.
            with self.lock, self.store.connection() as con:
                prior = con.execute(
                    "SELECT w.checkout_id FROM isolated_workspaces i "
                    "JOIN workspaces w ON w.workspace_id=i.workspace_id "
                    "WHERE i.session_id=?", (session_id,),
                ).fetchone()
                if prior is not None and prior[0] != checkout.checkout_id:
                    raise _error("isolated session belongs to another checkout")
            with self.lock, managed_mutation(
                self.store, primary, "isolated_create", session_id
            ) as operation:
                try:
                    result = self._open_owned(
                        checkout, session_id, approved_secrets=approved_secrets
                    )
                except Exception:
                    with self.store.connection() as con:
                        planned = con.execute(
                            "SELECT 1 FROM isolated_workspaces WHERE session_id=?",
                            (session_id,),
                        ).fetchone()
                    if planned is None and operation is not None:
                        operation.confirm()
                    raise
                if operation is not None:
                    operation.confirm()
            workspace, notices = result
            state = self.get(workspace.workspace_id)
            assert state is not None
            if state.stage == "rebuilding":
                assert workspace.git_dir is not None
                self._complete_rebuild(
                    workspace.workspace_id, Path(workspace.git_dir), primary
                )
                ready_workspace = self.store.get_workspace(workspace.workspace_id)
                state = self.get(state.workspace_id)
                assert ready_workspace is not None and state is not None
                workspace = ready_workspace
                notices = state.notices
            return workspace, notices

    def _open_owned(
        self,
        checkout: CheckoutRecord,
        session_id: str,
        *,
        approved_secrets: tuple[str, ...] = (),
    ) -> tuple[WorkspaceRecord, tuple[str, ...]]:
        with self.lock:
            with self.store.connection() as con:
                prior = con.execute(
                    "SELECT workspace_id FROM isolated_workspaces WHERE session_id=?",
                    (session_id,),
                ).fetchone()
            if prior is not None:
                state = self.validate(str(prior[0]))
                if set(approved_secrets) - set(state.runtime_files):
                    raise _error("existing isolated session cannot add runtime consent")
                workspace = self.store.get_workspace(state.workspace_id)
                assert workspace is not None
                return workspace, state.notices
            shared = self.store.ensure_shared_workspace(checkout)
            self.ensure_available(shared.workspace_id)
            scanner = ExternalWorkspaceScanner(self.store)
            for session in self.store.list_sessions(checkout_id=checkout.checkout_id):
                if session.workspace_mode == "shared" and session.state in {
                    "active",
                    "disconnected",
                }:
                    scanner.scan_session(session.session_id)
            root = Path(checkout.canonical_path)
            head = run_git(
                root, ["rev-parse", "--verify", "HEAD^{commit}"], check=False
            )
            if head.returncode:
                raise _error(
                    "ISOLATED_UNBORN_UNSUPPORTED: commit an initial source baseline "
                    "before opening isolated mode"
                )
            assert isinstance(head.stdout, str)
            base_oid = head.stdout.strip()
            plan = plan_hydration(
                root, approved_secret_paths=approved_secrets, rebuild=self.rebuild
            )
            policy_paths = tuple(
                entry.path
                for entry in plan.entries
                if Path(entry.path).name in {".gitignore", ".gitattributes"}
            )
            fingerprint = source_fingerprint(root, policy_paths)
            source = capture_source(root, self.candidate_dir)
            if not plan.complete:
                raise _error(
                    "isolated hydration inventory is incomplete: "
                    + ", ".join(plan.issues)
                )
            consent = {
                entry.path for entry in plan.entries if entry.action == "private_copy"
            }
            tree_roots = tuple(
                entry.path for entry in plan.entries if entry.action == "private_tree"
            )
            for relative in set(approved_secrets) - consent:
                if runtime_path_matches(relative, tree_roots) and _private_path(
                    relative
                ):
                    with _source(root, relative):
                        consent.add(relative)
            if set(approved_secrets) - consent:
                raise _error(
                    "--approve-secret must name an existing ignored secret regular "
                    "file exactly"
                )
            copies = [
                entry
                for entry in plan.entries
                if entry.action in {"private_copy", "clone_or_copy", "private_tree"}
            ]
            notices = tuple(
                (
                    f"Optional secret runtime omitted: {entry.path}. "
                    f"Reopen with --approve-secret {entry.path} to copy this file."
                    if entry.action == "needs_consent"
                    else f"Runtime omitted: {entry.path} ({entry.reason}). "
                    "No setup command was run."
                )
                for entry in plan.entries
                if entry.path not in source
                and entry.action
                not in {
                    "private_copy",
                    "clone_or_copy",
                    "private_tree",
                    "create_directory",
                    "rebuild",
                    "exclude_administration",
                }
            )
            runtime = tuple(
                sorted(
                    {
                        entry.path
                        for entry in plan.entries
                        if entry.action != "exclude_administration"
                        and entry.path not in source
                        and not any(
                            path.startswith(entry.path + "/") for path in source
                        )
                    }
                )
            )
            workspace_id = new_id("workspace")
            token = new_id("owner")
            destination = self.managed_root.resolve() / workspace_id
            now = self.store._now(None)
            with self.store.connection() as con, immediate_transaction(con):
                sequence = int(
                    con.execute(
                        "SELECT next_event_sequence FROM checkout_heads WHERE "
                        "checkout_id=?",
                        (checkout.checkout_id,),
                    ).fetchone()[0]
                )
                con.execute(
                    "INSERT INTO "
                    "workspaces(workspace_id,checkout_id,kind,mode,state,canonical_path,"
                    "source_workspace_id,workspace_revision,created_at,updated_at) "
                    "VALUES (?,?,'linked_worktree','isolated','creating',?,?,?,?,?)",
                    (
                        workspace_id,
                        checkout.checkout_id,
                        str(destination),
                        shared.workspace_id,
                        shared.workspace_revision,
                        now,
                        now,
                    ),
                )
                con.execute(
                    "INSERT INTO isolated_workspaces VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,'planned')",
                    (
                        workspace_id,
                        session_id,
                        base_oid,
                        sequence,
                        json.dumps(source, sort_keys=True),
                        json.dumps(runtime),
                        json.dumps([entry.path for entry in copies]),
                        json.dumps(notices),
                        fingerprint,
                        token,
                    ),
                )
            self.fault("planned")
            try:
                managed = create_managed_worktree(
                    root,
                    managed_root=self.managed_root,
                    task_id=workspace_id,
                    base_oid=base_oid,
                    checkout=False,
                )
                managed.path.chmod(0o700)
                marker = managed.git_dir / "loupe-isolated-owner"
                with _parent(managed.git_dir, marker.name) as (parent, name):
                    _write_body(parent, name, token.encode(), 0o600)
                with self.store.connection() as con:
                    con.execute(
                        "UPDATE workspaces SET git_dir=? WHERE workspace_id=?",
                        (str(managed.git_dir), workspace_id),
                    )
                self._stage(workspace_id, "registered")
                required = (
                    sum(int(entry["size"]) for entry in source.values())
                    + plan.planned_estimated_copy_bytes
                )
                space = os.statvfs(managed.path)
                if required > space.f_bavail * space.f_frsize:
                    raise _error(
                        "insufficient free space for private source and runtime copies"
                    )
                for relative, entry in source.items():
                    if entry["kind"] == "absent":
                        continue
                    if entry.get("screened"):
                        # Screened fixtures are usable by local checks, but never
                        # retained in the shared artifact store or shown to models.
                        if entry["kind"] == "symlink":
                            with _parent(root, relative) as (parent, name):
                                body = os.fsencode(os.readlink(name, dir_fd=parent))
                        else:
                            with _source(root, relative) as descriptor:
                                body = _read(descriptor, _MAX_SOURCE_FILE)
                        if (
                            content_identity(
                                ObjectKind(entry["kind"]), entry["mode"], body
                            )
                            != entry["digest"]
                        ):
                            raise _error(
                                "source changed during isolated creation: " + relative
                            )
                    else:
                        with _source(
                            self.candidate_dir, entry["content_hash"]
                        ) as descriptor:
                            body = _read(descriptor, _MAX_SOURCE_FILE)
                        if hashlib.sha256(body).hexdigest() != entry["content_hash"]:
                            raise _error("isolated source artifact changed")
                    with _parent(managed.path, relative, create=True) as (parent, name):
                        if entry["kind"] == "symlink":
                            validate_symlink_target(managed.path, relative, body)
                            os.symlink(os.fsdecode(body), name, dir_fd=parent)
                            os.fsync(parent)
                        else:
                            _write_body(parent, name, body, int(entry["permissions"]))
                if capture_source(managed.path, self.candidate_dir) != source:
                    raise _error("isolated materialized source manifest did not match")
                self._stage(workspace_id, "materialized")
                allocated = 0
                for runtime_entry in copies:
                    if runtime_entry.action == "private_tree":
                        from llm_cli.workspace.runtime import copy_dependency_tree

                        tree_notices = copy_dependency_tree(
                            root,
                            managed.path,
                            runtime_entry.path,
                            approved_secret_paths=approved_secrets,
                        )
                        notices += tree_notices
                        continue
                    copy_runtime_file(root, managed.path, runtime_entry)
                    with _source(managed.path, runtime_entry.path) as descriptor:
                        status = os.fstat(descriptor)
                        allocated += getattr(status, "st_blocks", 0) * 512
                    if allocated > plan.budgets.max_allocated_bytes:
                        raise _error("runtime copies exceeded their allocation budget")
                with self.store.connection() as con:
                    con.execute(
                        "UPDATE isolated_workspaces SET notices_json=? "
                        "WHERE workspace_id=?",
                        (json.dumps(notices), workspace_id),
                    )
                if (
                    source_fingerprint(root, policy_paths) != fingerprint
                    or capture_source(root, self.candidate_dir) != source
                ):
                    raise _error(
                        "source or ignore/attribute/config policy changed during "
                        "isolated creation"
                    )
                fresh = plan_hydration(
                    root, approved_secret_paths=approved_secrets, rebuild=self.rebuild
                )
                if not fresh.complete or [
                    entry
                    for entry in fresh.entries
                    if entry.action != "exclude_administration"
                ] != [
                    entry
                    for entry in plan.entries
                    if entry.action != "exclude_administration"
                ]:
                    raise _error("runtime inventory changed during isolated creation")
                if (
                    run_git(
                        root, ["rev-parse", "--verify", "HEAD^{commit}"]
                    ).stdout.strip()
                    != base_oid
                ):
                    raise _error("HEAD changed during isolated creation")
                if self.rebuild:
                    from llm_cli.workspace.rebuild import prepare

                    self.runtime.initialize(
                        workspace_id, approved_secrets,
                        rebuilt_roots=tuple(spec.root for spec in self.rebuild),
                    )
                    prepare(managed.git_dir, token, self.rebuild)
                    self.fault("rebuild_prepared")
                    with self.store.connection() as con:
                        con.execute(
                            "UPDATE isolated_workspaces SET stage='rebuilding' "
                            "WHERE workspace_id=?", (workspace_id,),
                        )
                    workspace = self.store.get_workspace(workspace_id)
                    assert workspace is not None
                    return workspace, notices
                self.runtime.initialize(workspace_id, approved_secrets)
                self._activate(workspace_id)
            except Exception:
                if not self._retire_failed_creation(workspace_id):
                    self._quarantine(workspace_id)
                raise
            workspace = self.store.get_workspace(workspace_id)
            assert workspace is not None
            return workspace, notices
