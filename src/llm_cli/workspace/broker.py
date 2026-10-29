"""Small, fail-closed filesystem primitives for shared-workspace publication.

The authority database decides whether a candidate may publish.  This module
does only the accompanying local filesystem work: retain candidate bodies in
private content-addressed storage, validate a safe repository-relative target,
and atomically replace a single regular file from checkout-external staging.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import stat
from contextlib import suppress
from pathlib import Path

from llm_cli.coordination.scopes import normalize_changed_path
from llm_cli.workspace.identity import (
    EXECUTABLE_MODE,
    REGULAR_MODE,
    SYMLINK_MODE,
    FileIdentity,
    ObjectKind,
    content_identity,
    read_identified_path,
)

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


def store_candidate_content(
    candidate_dir: Path, content: bytes, *, max_bytes: int = MAX_SHARED_TEXT_BYTES
) -> str:
    """Durably retain candidate bytes before exposing their candidate record."""

    if len(content) > max_bytes:
        raise WorkspaceBrokerError(
            f"shared file content exceeds the {max_bytes}-byte limit"
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


def read_candidate_path(
    path: Path, *, max_bytes: int = MAX_SHARED_TEXT_BYTES
) -> tuple[FileIdentity, bytes | None]:
    """Capture ordinary bytes or the link itself, never its referent."""
    identity, content = read_identified_path(path, max_bytes=max_bytes)
    if identity.kind is ObjectKind.SYMLINK:
        content = os.fsencode(os.readlink(path))
        if (
            len(content) > max_bytes
            or content_identity(ObjectKind.SYMLINK, SYMLINK_MODE, content)
            != identity.digest
            or read_identified_path(path)[0] != identity
        ):
            raise WorkspaceBrokerError("symlink changed during observation")
    return identity, content


def validate_symlink_target(root: Path, relative_path: str, content: bytes) -> str:
    """Validate a relative link without following any part of its destination."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceBrokerError("symlink target must be UTF-8") from exc
    if len(content) > 1000:
        raise WorkspaceBrokerError("symlink target exceeds the 1000-byte path limit")
    if not text or "\x00" in text or "\\" in text or text.startswith("/"):
        raise WorkspaceBrokerError("symlink target must be a relative contained path")
    destination = posixpath.normpath(
        posixpath.join(posixpath.dirname(relative_path), text)
    )
    if destination == "." or destination == relative_path:
        raise WorkspaceBrokerError("symlink cannot target itself or the checkout root")
    try:
        destination = normalize_changed_path(destination)
    except ValueError as exc:
        raise WorkspaceBrokerError(
            "symlink target escapes the checkout or a protected path"
        ) from exc
    from llm_cli.agent.source_policy import _private_path

    if _private_path(destination) or any(
        part in {".loupe", ".llm-coord", ".codex", ".agents"}
        for part in Path(destination).parts
    ):
        raise WorkspaceBrokerError("symlink target is a protected path")
    target = root
    for part in Path(destination).parts:
        target /= part
        if target.is_symlink():
            raise WorkspaceBrokerError("symlink target cannot traverse another symlink")
        if (target / ".git").exists():
            raise WorkspaceBrokerError(
                "symlink target cannot enter a nested repository"
            )
    return destination


def atomic_replace_symlink(
    *,
    checkout_root: Path,
    git_common_dir: Path,
    relative_path: str,
    content: bytes,
    publication_id: str,
    expected_base: FileIdentity | None = None,
) -> None:
    """Stage an exact link outside the checkout and install without following it."""
    validate_symlink_target(checkout_root, relative_path, content)
    target = workspace_target(checkout_root, relative_path)
    before, _ = read_candidate_path(target)
    if expected_base is not None and before != expected_base:
        raise WorkspaceBrokerError("symlink base changed before staging")
    if before.kind not in {ObjectKind.ABSENT, ObjectKind.SYMLINK}:
        raise WorkspaceBrokerError(
            "symlink replacement requires an absent path or symlink"
        )
    stage_root = git_common_dir / "llm-coord-stage"
    stage_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_private_directory(stage_root)
    if stage_root.stat().st_dev != target.parent.stat().st_dev:
        raise WorkspaceBrokerError("symlink staging is not on the checkout filesystem")
    temporary = stage_root / f"{publication_id}.tmp"
    if temporary.is_symlink():
        if os.fsencode(os.readlink(temporary)) != content:
            raise WorkspaceBrokerError("symlink staging has an unrecognized target")
        temporary.unlink()
    elif temporary.exists():
        raise WorkspaceBrokerError("symlink staging path has an unexpected type")
    parent_fd = _open_publication_parent(checkout_root, relative_path)
    try:
        os.symlink(os.fsdecode(content), temporary)
        _fsync_directory(stage_root)
        workspace_target(checkout_root, relative_path)
        validate_symlink_target(checkout_root, relative_path, content)
        if (
            read_candidate_path(target)[0] != before
            or os.fstat(parent_fd).st_ino != target.parent.stat().st_ino
        ):
            raise WorkspaceBrokerError("symlink target changed during staging")
        os.replace(temporary, target.name, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise
    finally:
        os.close(parent_fd)


def atomic_replace_regular_file(
    *,
    checkout_root: Path,
    git_common_dir: Path,
    relative_path: str,
    content: bytes,
    mode: str,
    publication_id: str,
    permissions: int | None = None,
    expected_base: FileIdentity | None = None,
) -> None:
    """Write one fully prepared regular file then atomically install it.

    The staging directory sits in the checkout's Git common directory, outside
    the visible worktree so `git add -A` cannot capture temporary names.  A
    device check makes the final ``replace`` an atomic rename rather than a
    silent copy/delete fallback.
    """

    if mode not in {REGULAR_MODE, EXECUTABLE_MODE}:
        raise WorkspaceBrokerError("candidate has an unsupported regular-file mode")
    if permissions is not None and (
        type(permissions) is not int or not 0 <= permissions <= 0o777
    ):
        raise WorkspaceBrokerError("candidate filesystem permissions are invalid")
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
        # A brand-new file has no prior access policy to preserve. Keep it
        # private, and let open apply the daemon's umask without changing the
        # process-wide umask in this multithreaded service.
        descriptor = os.open(
            temporary, flags, 0o700 if mode == EXECUTABLE_MODE else 0o600
        )
    except FileExistsError:
        raise WorkspaceBrokerError(
            "a prior publication with this ID still needs recovery"
        ) from None
    parent_fd: int | None = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            parent_fd = _open_publication_parent(checkout_root, relative_path)
            handle.write(content)
            handle.flush()
            workspace_target(checkout_root, relative_path)
            if (
                expected_base is not None
                and read_candidate_path(
                    target, max_bytes=_base_read_limit(expected_base)
                )[0]
                != expected_base
            ):
                raise WorkspaceBrokerError("file base changed during staging")
            try:
                current = target.lstat()
            except FileNotFoundError:
                current = None
            access = permissions
            if current is not None:
                if not stat.S_ISREG(current.st_mode):
                    raise WorkspaceBrokerError(
                        "publication target is not a regular file"
                    )
                live_access = stat.S_IMODE(current.st_mode) & 0o777
                access = live_access if access is None else access & live_access
            if access is not None:
                # Only the Git executable bit may be deliberately changed.
                # Preserve tighter access set by the user since observation.
                access = (
                    access | stat.S_IXUSR
                    if mode == EXECUTABLE_MODE
                    else access & ~stat.S_IXUSR
                )
                os.fchmod(handle.fileno(), access)
            os.fsync(handle.fileno())
            _fsync_directory(stage_root)
            # Refuse a replacement/chmod observed during final preparation.
            # Shared writers hold a common lock; external editors do not.
            workspace_target(checkout_root, relative_path)
            try:
                latest = target.lstat()
            except FileNotFoundError:
                latest = None
            fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if (current is None) != (latest is None) or (
                current is not None
                and latest is not None
                and any(
                    getattr(current, field) != getattr(latest, field)
                    for field in fields
                )
            ):
                raise WorkspaceBrokerError("publication target changed during staging")
            current_parent = target.parent.stat()
            opened_parent = os.fstat(parent_fd)
            if (current_parent.st_dev, current_parent.st_ino) != (
                opened_parent.st_dev,
                opened_parent.st_ino,
            ):
                raise WorkspaceBrokerError("publication parent changed during staging")
            os.replace(temporary, target.name, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _base_read_limit(expected: FileIdentity) -> int:
    # Isolated catch-up replaces source files larger than a shared edit may be.
    return max(MAX_SHARED_TEXT_BYTES, expected.size)


def atomic_delete_file(root: Path, relative_path: str, expected: FileIdentity) -> None:
    """Delete the observed leaf through pinned real parents, never its referent."""
    target = workspace_target(root, relative_path)
    parent_fd = _open_publication_parent(root, relative_path)
    try:
        current, _ = read_candidate_path(target, max_bytes=_base_read_limit(expected))
        if current != expected:
            raise WorkspaceBrokerError("file base changed before deletion")
        workspace_target(root, relative_path)
        current_parent = target.parent.stat()
        opened_parent = os.fstat(parent_fd)
        if (current_parent.st_dev, current_parent.st_ino) != (
            opened_parent.st_dev,
            opened_parent.st_ino,
        ):
            raise WorkspaceBrokerError("publication parent changed before deletion")
        os.unlink(target.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _open_publication_parent(root: Path, relative_path: str) -> int:
    """Pin every parent component without following replacement directory links."""
    parts = Path(normalize_changed_path(relative_path)).parts[:-1]
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
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
