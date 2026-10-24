"""Explicit local runtime copies; only fixed disposition metadata leaves this module.

No file bytes, filenames or content identities are returned or persisted. Both
trees are traversed through no-follow descriptors. A complete bounded inventory
is checked again before atomically publishing a new, owner-only destination.
"""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import stat
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from llm_cli.agent.source_policy import contains_secret_material, runtime_path_matches
from llm_cli.coordination.scopes import normalize_changed_path
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.workspace.hydration import (
    HydrationBudgets,
    _bounded_git,
    _BudgetExceeded,
    _evidence,
    _ignored_paths,
    _likely_secret,
)
from llm_cli.workspace.isolated import _parent

_ADMIN = frozenset({".git", ".loupe", ".llm-coord", ".codex", ".agents"})


def _unsafe(message: str) -> LlmCoordError:
    return LlmCoordError(ErrorCode.REPOSITORY_UNSAFE, message)


def _check_time(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _unsafe("runtime export exceeds its time budget")


def _inventory(
    root: int, relative: str, budgets: HydrationBudgets, deadline: float
) -> dict[str, os.stat_result]:
    result: dict[str, os.stat_result] = {}
    apparent = allocated = metadata = 0
    device = os.fstat(root).st_dev

    def visit(parent: int, name: str, path: str, depth: int) -> None:
        nonlocal apparent, allocated, metadata
        _check_time(deadline)
        metadata += len(os.fsencode(path)) + 256
        if (
            len(result) >= budgets.max_entries
            or metadata > budgets.max_metadata_bytes
            or depth > budgets.max_depth
        ):
            raise _unsafe("runtime export exceeds its inventory budget")
        if name.casefold() in _ADMIN:
            raise _unsafe("runtime export contains administrative entries")
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if before.st_dev != device or not (
            stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode)
        ):
            raise _unsafe("runtime export refuses links, mounts and special files")
        result[path] = before
        if stat.S_ISREG(before.st_mode):
            apparent += before.st_size
            allocated += before.st_blocks * 512
            if (
                before.st_size > budgets.max_file_bytes
                or apparent > budgets.max_total_bytes
                or allocated > budgets.max_allocated_bytes
            ):
                raise _unsafe("runtime export exceeds its byte budget")
            return
        child = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        try:
            if _evidence(os.fstat(child)) != _evidence(before):
                raise _unsafe("runtime export source changed during inventory")
            with os.scandir(child) as entries:
                for entry in entries:
                    visit(child, entry.name, path + "/" + entry.name, depth + 1)
            if _evidence(os.fstat(child)) != _evidence(before):
                raise _unsafe("runtime export source changed during inventory")
        finally:
            os.close(child)

    with _parent(root, relative) as (parent, name):
        visit(parent, name, relative, 0)
    return result


def _same(before: dict[str, os.stat_result], after: dict[str, os.stat_result]) -> bool:
    return {path: _evidence(value) for path, value in before.items()} == {
        path: _evidence(value) for path, value in after.items()
    }


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _canonical(path: str) -> str:
    return unicodedata.normalize("NFD", path).casefold()


def _under(path: str, roots: set[str]) -> bool:
    current = _canonical(path)
    while current:
        if current in roots:
            return True
        current = current.rpartition("/")[0]
    return False


def _secret_contents(
    source: int,
    entries: dict[str, os.stat_result],
    deadline: float,
) -> bool:
    """Screen complete files locally, retaining only a bounded overlap window."""
    for path, before in entries.items():
        if not stat.S_ISREG(before.st_mode):
            continue
        with _parent(source, path) as (parent, leaf):
            descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent,
            )
            try:
                if _evidence(os.fstat(descriptor)) != _evidence(before):
                    raise _unsafe("runtime export source changed during screening")
                overlap = b""
                remaining = before.st_size
                sensitive = False
                while remaining:
                    _check_time(deadline)
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        raise _unsafe("runtime export source changed during screening")
                    remaining -= len(chunk)
                    value = overlap + chunk
                    sensitive = sensitive or contains_secret_material(value)
                    # Every current signature has a recognition prefix under
                    # 256 bytes. This covers signatures crossing read boundaries.
                    overlap = value[-1024:]
                if os.read(descriptor, 1) or (
                    _evidence(os.fstat(descriptor)) != _evidence(before)
                ):
                    raise _unsafe("runtime export source changed during screening")
                if sensitive:
                    return True
            finally:
                os.close(descriptor)
    return False


@contextmanager
def _destination_parent(
    destination: Path,
) -> Iterator[tuple[int, str, Callable[[], None], dict[tuple[int, int], int]]]:
    """Keep every named destination ancestor pinned, including its parent link."""
    if (
        not destination.is_absolute()
        or destination.name in {"", ".", ".."}
        or any(part in {".", ".."} for part in destination.parts)
        or any(part.casefold() in _ADMIN for part in destination.parts)
    ):
        raise _unsafe("runtime export destination must be a safe absolute path")
    chain: list[tuple[int, str, int]] = []
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptors = [descriptor]
    ancestry = {_identity(os.fstat(descriptor)): 0}
    try:
        for part in destination.parts[1:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            chain.append((descriptor, part, child))
            descriptors.append(child)
            descriptor = child
            ancestry[_identity(os.fstat(child))] = len(descriptors) - 1

        def verify() -> None:
            for parent, name, child in chain:
                if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != (
                    _identity(os.fstat(child))
                ):
                    raise _unsafe("runtime export destination changed during copy")

        verify()
        yield descriptor, destination.name, verify, ancestry
    finally:
        for opened in reversed(descriptors):
            os.close(opened)


def _relative_within(
    destination: Path,
    root: Path,
    ancestry: dict[tuple[int, int], int],
) -> str | None:
    """Classify physical ancestry, conservatively reserving spelling aliases."""
    resolved = root.resolve()
    with suppress(FileNotFoundError):
        depth = ancestry.get(_identity(os.stat(resolved, follow_symlinks=False)))
        if depth is not None:
            return Path(*destination.parts[depth + 1 :]).as_posix()
    candidate, protected = _canonical(str(destination)), _canonical(str(resolved))
    if candidate == protected or candidate.startswith(protected.rstrip("/") + "/"):
        return Path(*destination.parts[len(resolved.parts) :]).as_posix()
    return None


def _rename_new(parent: int, source: str, destination: str) -> None:
    """Atomically publish a file or tree without replacing a raced destination."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(library, "renameatx_np"):
        rename = library.renameatx_np
        flag = 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        flag = 1  # RENAME_NOREPLACE
    else:
        raise _unsafe("atomic runtime export is unavailable on this platform")
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(source), parent, os.fsencode(destination), flag):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _remove(parent: int, name: str) -> None:
    """Remove only the staging entry owned by this operation, without links."""
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISDIR(info.st_mode):
        child = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        try:
            with os.scandir(child) as entries:
                for entry in entries:
                    _remove(child, entry.name)
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=parent)
    else:
        os.unlink(name, dir_fd=parent)


def _copy_file(
    source: int,
    path: str,
    parent: int,
    name: str,
    before: os.stat_result,
    deadline: float,
) -> None:
    with _parent(source, path) as (source_parent, leaf):
        descriptor = os.open(
            leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_parent
        )
        try:
            if _evidence(os.fstat(descriptor)) != _evidence(before):
                raise _unsafe("runtime export source changed before copy")
            output = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            try:
                remaining = before.st_size
                while remaining:
                    _check_time(deadline)
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        raise _unsafe("runtime export source changed during copy")
                    remaining -= len(chunk)
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(output, view) :]
                if os.read(descriptor, 1) or (
                    _evidence(os.fstat(descriptor)) != _evidence(before)
                ):
                    raise _unsafe("runtime export source changed during copy")
                os.fchmod(output, 0o700 if before.st_mode & 0o111 else 0o600)
                os.fsync(output)
            finally:
                os.close(output)
        finally:
            os.close(descriptor)


def _copy_tree(
    source: int,
    relative: str,
    entries: dict[str, os.stat_result],
    parent: int,
    stage: str,
    deadline: float,
) -> None:
    os.mkdir(stage, 0o700, dir_fd=parent)
    tree = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        for path, info in sorted(entries.items()):
            _check_time(deadline)
            if path == relative:
                continue
            tail = path[len(relative) + 1 :]
            with _parent(tree, tail) as (target, leaf):
                if stat.S_ISDIR(info.st_mode):
                    os.mkdir(leaf, 0o700, dir_fd=target)
                else:
                    _copy_file(source, path, target, leaf, info, deadline)
        os.fsync(tree)
    finally:
        os.close(tree)


def _checkout_roots(
    checkout: Path, supplied: Sequence[Path], deadline: float, limit: int
) -> tuple[Path, ...]:
    code, output = _bounded_git(
        checkout,
        ["worktree", "list", "--porcelain", "-z"],
        deadline=deadline,
        limit=limit,
    )
    if code:
        raise _unsafe("runtime export cannot verify linked checkout destinations")
    roots = {checkout.resolve(), *(path.resolve() for path in supplied)}
    for field in output.split(b"\0"):
        if field.startswith(b"worktree "):
            roots.add(Path(os.fsdecode(field[9:])).resolve())
    return tuple(roots)


def export_runtime(
    root: Path,
    relative: str,
    destination: Path,
    *,
    checkout: Path,
    runtime_paths: Sequence[str],
    source_paths: Sequence[str],
    shared_checkouts: Sequence[Path] = (),
    protected_roots: Sequence[Path] = (),
    yes: bool = False,
    allow_secret: bool = False,
    into_checkout: bool = False,
    budgets: HydrationBudgets | None = None,
) -> dict[str, Any]:
    """Preview or copy one runtime entry. Existing destinations are never changed."""
    budgets = budgets or HydrationBudgets()
    deadline = time.monotonic() + budgets.max_seconds
    source = -1
    stage: str | None = None
    try:
        if normalize_changed_path(relative) != relative or any(
            part.casefold() in _ADMIN for part in Path(relative).parts
        ):
            raise _unsafe("runtime export requires one canonical runtime path")
        source = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        root_identity = _identity(os.fstat(source))
        entries = _inventory(source, relative, budgets, deadline)
        paths = tuple(entries)
        accepted = {_canonical(path) for path in source_paths}
        reserved = {_canonical(path) for path in runtime_paths}
        if any(_under(path, accepted) for path in paths):
            raise _unsafe("runtime export cannot copy accepted source")
        unknown = [path for path in paths if not _under(path, reserved)]
        if unknown:
            ignored = _ignored_paths(
                root, unknown, deadline=deadline, limit=budgets.max_metadata_bytes
            )
            if set(unknown) - ignored:
                raise _unsafe("runtime export requires ignored or reserved runtime")
        kind = "directory" if stat.S_ISDIR(entries[relative].st_mode) else "file"
        sensitive = any(map(_likely_secret, paths)) or _secret_contents(
            source,
            entries,
            deadline,
        )
        result: dict[str, Any] = {
            "state": "confirmation_required",
            "kind": kind,
            "bytes": sum(
                info.st_size for info in entries.values() if stat.S_ISREG(info.st_mode)
            ),
            "entries": len(entries),
            "sensitivity": "likely_secret" if sensitive else "unclassified",
            "requires_yes": not yes,
            "requires_allow_secret": sensitive and not allow_secret,
        }

        def validate_policy(ancestry: dict[tuple[int, int], int]) -> None:
            for protected in (root, *protected_roots):
                if _relative_within(destination, protected, ancestry) is not None:
                    raise _unsafe("runtime export destination is coordinator storage")
            roots = _checkout_roots(
                checkout, shared_checkouts, deadline, budgets.max_metadata_bytes
            )
            for shared in roots:
                tail = _relative_within(destination, shared, ancestry)
                if tail is None:
                    continue
                if not into_checkout:
                    raise _unsafe(
                        "runtime export into a checkout requires --into-checkout"
                    )
                code, tracked = _bounded_git(
                    shared,
                    ["ls-files", "-z"],
                    deadline=deadline,
                    limit=budgets.max_metadata_bytes,
                )
                if code:
                    raise _unsafe("runtime export cannot verify tracked destinations")
                for name in tracked.split(b"\0"):
                    path = os.fsdecode(name)
                    if path and (
                        runtime_path_matches(path, (tail,))
                        or runtime_path_matches(tail, (path,))
                    ):
                        raise _unsafe("runtime export cannot overwrite tracked source")
                if tail not in _ignored_paths(
                    shared,
                    [tail],
                    deadline=deadline,
                    limit=budgets.max_metadata_bytes,
                ):
                    raise _unsafe(
                        "runtime export into a checkout requires an ignored destination"
                    )

        with _destination_parent(destination) as (parent, leaf, verify, ancestry):
            validate_policy(ancestry)
            try:
                os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise _unsafe("runtime export destination already exists")
            if not yes or result["requires_allow_secret"]:
                return result
            stage = ".loupe-runtime-export-" + secrets.token_hex(16)
            published_identity: tuple[int, int] | None = None
            try:
                if kind == "directory":
                    _copy_tree(source, relative, entries, parent, stage, deadline)
                else:
                    _copy_file(
                        source, relative, parent, stage, entries[relative], deadline
                    )
                if not _same(entries, _inventory(source, relative, budgets, deadline)):
                    raise _unsafe("runtime export source changed before publication")
                if _identity(os.stat(root, follow_symlinks=False)) != root_identity:
                    raise _unsafe(
                        "runtime export source root changed before publication"
                    )
                validate_policy(ancestry)
                verify()
                staged_identity = _identity(
                    os.stat(stage, dir_fd=parent, follow_symlinks=False)
                )
                _rename_new(parent, stage, leaf)
                stage = None
                published_identity = staged_identity
                verify()
                validate_policy(ancestry)
                if _identity(os.stat(leaf, dir_fd=parent, follow_symlinks=False)) != (
                    staged_identity
                ):
                    raise _unsafe(
                        "runtime export destination changed during publication"
                    )
                os.fsync(parent)
                published_identity = None
            finally:
                if published_identity is not None:
                    # Roll back only our own inode after failed final checks.
                    with suppress(FileNotFoundError):
                        if (
                            _identity(
                                os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                            )
                            == published_identity
                        ):
                            _remove(parent, leaf)
                if stage is not None:
                    with suppress(FileNotFoundError):
                        _remove(parent, stage)
            return {**result, "state": "exported"}
    except _BudgetExceeded as exc:
        raise _unsafe("runtime export exceeds its metadata or time budget") from exc
    except (OSError, ValueError) as exc:
        # OS exceptions include filenames; never send them to logs/RPC clients.
        message = (
            "runtime export destination already exists"
            if isinstance(exc, OSError) and exc.errno == errno.EEXIST
            else "runtime export refused an unsafe or changed filesystem entry"
        )
        raise _unsafe(message) from exc
    finally:
        if source >= 0:
            os.close(source)
