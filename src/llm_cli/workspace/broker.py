"""Small, fail-closed filesystem primitives for shared-workspace publication.

The authority database decides whether a candidate may publish.  This module
does only the accompanying local filesystem work: retain candidate bodies in
private content-addressed storage, validate a safe repository-relative target,
and atomically replace a single regular file from checkout-external staging.
"""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import suppress
from pathlib import Path

from llm_cli.coordination.scopes import normalize_changed_path
from llm_cli.workspace.identity import EXECUTABLE_MODE, REGULAR_MODE

MAX_SHARED_TEXT_BYTES = 256 * 1024


class WorkspaceBrokerError(ValueError):
    """A shared-workspace filesystem operation cannot safely proceed."""


def candidate_content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def candidate_content_path(candidate_dir: Path, content_hash: str) -> Path:
    if len(content_hash) != 64 or any(
        char not in "0123456789abcdef" for char in content_hash
    ):
        raise WorkspaceBrokerError("candidate content hash is invalid")
    return candidate_dir / content_hash


def store_candidate_content(candidate_dir: Path, content: bytes) -> str:
    """Durably retain candidate bytes before exposing their candidate record."""

    if len(content) > MAX_SHARED_TEXT_BYTES:
        raise WorkspaceBrokerError(
            f"shared file content exceeds the {MAX_SHARED_TEXT_BYTES}-byte limit"
        )
    candidate_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_private_directory(candidate_dir)
    digest = candidate_content_hash(content)
    target = candidate_content_path(candidate_dir, digest)
    if target.exists():
        _require_regular_private_file(target)
        if target.read_bytes() != content:
            raise WorkspaceBrokerError(
                "candidate content address does not match its body"
            )
        return digest

    temporary = candidate_dir / f".{digest}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
    except FileExistsError:
        # A previous interrupted writer left only an untrusted temporary name.
        # Never reuse it; the caller can safely try again with a new process.
        raise WorkspaceBrokerError("candidate staging is temporarily busy") from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(candidate_dir)
    except BaseException:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise
    return digest


def load_candidate_content(candidate_dir: Path, content_hash: str) -> bytes:
    target = candidate_content_path(candidate_dir, content_hash)
    _require_regular_private_file(target)
    content = target.read_bytes()
    if candidate_content_hash(content) != content_hash:
        raise WorkspaceBrokerError("candidate body failed its content-address check")
    return content


def workspace_target(checkout_root: Path, relative_path: str) -> Path:
    """Return a safe target without resolving a missing leaf through a link."""

    normalized = normalize_changed_path(relative_path)
    root = checkout_root.resolve(strict=True)
    if not root.is_dir():
        raise WorkspaceBrokerError("workspace checkout is not a directory")
    target = root.joinpath(*normalized.split("/"))
    _require_real_parent(root, target.parent)
    return target


def atomic_replace_regular_file(
    *,
    checkout_root: Path,
    git_common_dir: Path,
    relative_path: str,
    content: bytes,
    mode: str,
    publication_id: str,
) -> None:
    """Write one fully prepared regular file then atomically install it.

    The staging directory sits in the checkout's Git common directory, outside
    the visible worktree so `git add -A` cannot capture temporary names.  A
    device check makes the final ``replace`` an atomic rename rather than a
    silent copy/delete fallback.
    """

    if mode not in {REGULAR_MODE, EXECUTABLE_MODE}:
        raise WorkspaceBrokerError("candidate has an unsupported regular-file mode")
    target = workspace_target(checkout_root, relative_path)
    if target.exists() and target.is_dir():
        raise WorkspaceBrokerError(
            "a directory cannot be replaced by a regular-file candidate"
        )
    stage_root = git_common_dir / "llm-coord-stage"
    stage_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_private_directory(stage_root)
    if stage_root.stat().st_dev != target.parent.stat().st_dev:
        raise WorkspaceBrokerError(
            "Git staging is not on the checkout filesystem; atomic "
            "publication is unavailable"
        )

    temporary = stage_root / f"{publication_id}.tmp"
    if temporary.exists() or temporary.is_symlink():
        # A prepared journal owns this exact opaque filename. If the prior
        # daemon died before rename, discarding only that registered temporary
        # is safe; candidate bytes remain in private durable storage.
        status = temporary.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise WorkspaceBrokerError("publication staging path is not a regular file")
        temporary.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
    except FileExistsError:
        raise WorkspaceBrokerError(
            "a prior publication with this ID still needs recovery"
        ) from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o755 if mode == EXECUTABLE_MODE else 0o644)
        _fsync_directory(stage_root)
        # The caller has just revalidated the old identity. Recheck safe parent
        # components at this final boundary so a normal editor cannot redirect
        # us through an accidentally introduced directory link.
        workspace_target(checkout_root, relative_path)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise


def _require_real_parent(root: Path, parent: Path) -> None:
    try:
        relative = parent.relative_to(root)
    except ValueError as exc:  # pragma: no cover - construction above pins this
        raise WorkspaceBrokerError("workspace target escaped its checkout") from exc
    current = root
    for component in relative.parts:
        current = current / component
        try:
            status = current.lstat()
        except FileNotFoundError as exc:
            raise WorkspaceBrokerError(
                "candidate parent directory does not exist"
            ) from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise WorkspaceBrokerError("candidate parent must be a real directory")


def _require_private_directory(path: Path) -> None:
    status = path.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise WorkspaceBrokerError("candidate staging path is not a directory")
    if status.st_mode & 0o077:
        os.chmod(path, 0o700)


def _require_regular_private_file(path: Path) -> None:
    try:
        status = path.lstat()
    except FileNotFoundError as exc:
        raise WorkspaceBrokerError("candidate content is no longer available") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise WorkspaceBrokerError("candidate content is not a regular file")
    if status.st_mode & 0o077:
        os.chmod(path, 0o600)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise WorkspaceBrokerError(
            f"could not sync directory {path}: {exc.strerror}"
        ) from exc
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "MAX_SHARED_TEXT_BYTES",
    "WorkspaceBrokerError",
    "atomic_replace_regular_file",
    "candidate_content_hash",
    "load_candidate_content",
    "store_candidate_content",
    "workspace_target",
]
