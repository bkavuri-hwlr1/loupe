"""Opt-in private installer artifacts, shared leases and idle-only eviction.

No cache body or private value enters SQLite. A private HMAC distinguishes input
sets; each change advances a namespace that is never reused after rotation.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from llm_cli.paths import reject_symlink_components

DEFAULT_MAX_BYTES = 512 * 1024**2
MAX_BYTES = 5 * 1024**3
MAX_ENTRIES = 200_000
MAX_NAMESPACES = 32

_GUARDS: dict[Path, tuple[int, int]] = {}
_GUARDS_LOCK = threading.RLock()


class CacheLimit(ValueError):
    """Cache growth requires idle pruning before reuse."""


class CacheError(ValueError):
    """A cache failure contains only an operator-safe explanation."""


@dataclass(frozen=True)
class CacheLease:
    path: Path
    supervisor_fds: tuple[int, ...]


def _directory(path: Path) -> None:
    reject_symlink_components(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    status = path.lstat()
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_mode & 0o077
    ):
        raise CacheError("installer cache needs an owned private directory")


def _safe_file(descriptor: int) -> os.stat_result:
    status = os.fstat(descriptor)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_mode & 0o077
        or status.st_nlink != 1
    ):
        raise CacheError("installer cache metadata identity changed")
    return status


@contextmanager
def _lock(
    path: Path, *, shared: bool = False, wait: bool = True, timeout: float | None = None
) -> Iterator[int | None]:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _safe_file(descriptor)
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            try:
                fcntl.flock(
                    descriptor,
                    operation
                    | (fcntl.LOCK_NB if not wait or deadline is not None else 0),
                )
                break
            except BlockingIOError:
                if not wait:
                    yield None
                    return
                if deadline is not None and time.monotonic() >= deadline:
                    raise CacheError(
                        "installer cache is busy; retry after other rebuilds finish "
                        "or disable isolated.rebuild.cache"
                    ) from None
                time.sleep(0.025)
        yield descriptor
    finally:
        os.close(descriptor)


def _read(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        status = _safe_file(descriptor)
        if status.st_size > maximum:
            raise CacheError("installer cache metadata exceeds its bound")
        body = os.read(descriptor, maximum + 1)
        if len(body) != status.st_size or len(body) > maximum:
            raise CacheError("installer cache metadata changed")
        return body
    finally:
        os.close(descriptor)


def _atomic(path: Path, body: bytes) -> None:
    name = ".cache-" + secrets.token_hex(16)
    descriptor = os.open(
        path.parent / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(path.parent / name, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        (path.parent / name).unlink(missing_ok=True)


def _namespace(name: object) -> bool:
    return (
        isinstance(name, str)
        and len(name) == 35
        and name.startswith("ns-")
        and all(char in "0123456789abcdef" for char in name[3:])
    )


def _inventory(
    root: Path, *, maximum_entries: int = MAX_ENTRIES, reject_hardlinks: bool = False
) -> tuple[int, int]:
    count = size = 0
    deadline = time.monotonic() + 5
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    device = os.fstat(descriptor).st_dev

    def walk(parent: int, depth: int) -> None:
        nonlocal count, size
        with os.scandir(parent) as entries:
            for entry in entries:
                count += 1
                if count > maximum_entries or depth > 64 or time.monotonic() > deadline:
                    raise CacheLimit("installer cache exceeded its observation limit")
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != device:
                    raise CacheError("installer cache contains a mount")
                if stat.S_ISDIR(info.st_mode):
                    size += getattr(info, "st_blocks", 0) * 512
                    child = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent,
                    )
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            raise CacheError("installer cache directory changed")
                        walk(child, depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    if reject_hardlinks and info.st_nlink != 1:
                        raise CacheError(
                            "installer cache has a shared hardlink; run "
                            "loupe workspace cache prune --all "
                            "or disable isolated.rebuild.cache"
                        )
                    size += max(info.st_size, getattr(info, "st_blocks", 0) * 512)
                elif stat.S_ISLNK(info.st_mode):
                    size += info.st_size
                else:
                    raise CacheError("installer cache contains a special file")

    try:
        walk(descriptor, 0)
    finally:
        os.close(descriptor)
    return size, count


def _remove(root: Path, name: str) -> None:
    # Exclusive lease ownership proves no installer uses this namespace.
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    device = os.fstat(parent).st_dev
    deadline = time.monotonic() + 30

    def remove(directory: int, leaf: str, depth: int) -> None:
        if depth > 64 or time.monotonic() > deadline:
            raise CacheError("installer cache cleanup needs another idle prune")
        try:
            before = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        if before.st_dev != device:
            raise CacheError("installer cache contains a mount")
        if not stat.S_ISDIR(before.st_mode):
            os.unlink(leaf, dir_fd=directory)
            return
        child = os.open(
            leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
        )
        try:
            pinned = os.fstat(child)
            if (pinned.st_dev, pinned.st_ino) != (before.st_dev, before.st_ino):
                raise CacheError("installer cache cleanup directory changed")
            with os.scandir(child) as entries:
                for entry in entries:
                    remove(child, entry.name, depth + 1)
            named = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise CacheError("installer cache cleanup directory changed")
            os.rmdir(leaf, dir_fd=directory)
        finally:
            os.close(child)

    try:
        remove(parent, name, 0)
        os.fsync(parent)
    finally:
        os.close(parent)


class InstallerCache:
    def __init__(self, root: Path, maximum_bytes: int) -> None:
        if (
            type(maximum_bytes) is not int
            or not 64 * 1024 <= maximum_bytes <= MAX_BYTES
        ):
            raise CacheError("installer cache byte cap must be 64 KiB..5 GiB")
        self.root, self.maximum_bytes = root, maximum_bytes

    def _head(self) -> dict[str, str] | None:
        try:
            value = json.loads(_read(self.root / "head.json", 1024))
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError) as exc:
            raise CacheError("installer cache metadata needs recovery") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"fingerprint", "namespace"}
            or not _namespace(value["namespace"])
            or not isinstance(value["fingerprint"], str)
            or len(value["fingerprint"]) != 64
            or any(c not in "0123456789abcdef" for c in value["fingerprint"])
        ):
            raise CacheError("installer cache metadata needs recovery")
        return value

    def _select(self, values: tuple[str, ...]) -> Path:
        marker = self.root / "protection.required"
        if not marker.exists() and not marker.is_symlink():
            _atomic(marker, b"1\n")
        elif _read(marker, 16) != b"1\n":
            raise CacheError("installer cache metadata needs recovery")
        key_path = self.root / "namespace.key"
        try:
            key = _read(key_path, 32)
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            _atomic(key_path, key)
        if len(key) != 32:
            raise CacheError("installer cache metadata needs recovery")
        fingerprint = hmac.new(
            key, json.dumps(sorted(set(values))).encode(), hashlib.sha256
        ).hexdigest()
        head = self._head()
        if head is None or head["fingerprint"] != fingerprint:
            head = {
                "fingerprint": fingerprint,
                "namespace": "ns-" + secrets.token_hex(16),
            }
            _directory(self.root / head["namespace"])
            _atomic(self.root / "head.json", json.dumps(head).encode())
        target = self.root / head["namespace"]
        _directory(target)
        return target

    @contextmanager
    def activity(self, values: tuple[str, ...]) -> Iterator[Path]:
        with self.lease(values) as lease:
            yield lease.path

    @contextmanager
    def lease(
        self, values: tuple[str, ...], *, timeout: float = 60
    ) -> Iterator[CacheLease]:
        if not 0 < timeout <= 300:
            raise CacheError(
                "installer cache settle timeout must be positive and bounded"
            )
        _directory(self.root)
        self.prune()
        try:
            with _lock(self.root / "leases.lock", shared=True) as descriptor:
                assert descriptor is not None
                with _lock(self.root / "metadata.lock"):
                    target = self._select(values)
                # Same-input installers share artifacts serially: a live
                # builder may temporarily create aliases, so another writer
                # must validate only after that builder has fully stopped.
                with _lock(
                    self.root / ("install-" + target.name + ".lock"), timeout=timeout
                ) as selected:
                    assert selected is not None
                    _inventory(target, reject_hardlinks=True)
                    # Keep both leases alive in the supervisor after daemon
                    # death. The installer itself must not inherit either FD.
                    retained: list[int] = []
                    try:
                        for fd in (descriptor, selected):
                            retained.append(fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 64))
                        yield CacheLease(target, tuple(retained))
                    finally:
                        for fd in retained:
                            os.close(fd)
        finally:
            # Release SH before waiting for EX. Other installers do likewise,
            # so all writers can finish without a lock-upgrade deadlock.
            # A finished workspace cannot become ready until shared artifacts
            # have been checked without a live installer mutating them.
            with (
                _lock(self.root / "leases.lock", timeout=timeout),
                _lock(self.root / "metadata.lock"),
            ):
                self._prune_owned()

    def invalidate(self, target: Path) -> None:
        """A changed input set cannot leave a reusable contaminated head."""
        with _lock(self.root / "metadata.lock"):
            head = self._head()
            if head is not None and head["namespace"] == target.name:
                (self.root / "head.json").unlink()
                descriptor = os.open(
                    self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

    def over_limit(self) -> bool:
        """Observe cache growth alongside the existing installer size checks."""
        size, _ = _inventory(self.root)
        with os.scandir(self.root) as entries:
            namespaces = sum(_namespace(entry.name) for entry in entries)
        return size > self.maximum_bytes or namespaces > MAX_NAMESPACES

    def prune(self, *, all_artifacts: bool = False) -> bool:
        _directory(self.root)
        with _lock(self.root / "leases.lock", wait=False) as idle:
            if idle is None:
                return False
            with _lock(self.root / "metadata.lock"):
                self._prune_owned(all_artifacts=all_artifacts)
                return True

    def _prune_owned(self, *, all_artifacts: bool = False) -> None:
        directories = []
        namespace_locks: list[str] = []
        with os.scandir(self.root) as entries:
            for entry in entries:
                if _namespace(entry.name):
                    info = entry.stat(follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode) and not all_artifacts:
                        raise CacheError("installer cache namespace identity changed")
                    directories.append((info.st_mtime_ns, entry.name))
                elif (
                    entry.name.startswith("install-")
                    and entry.name.endswith(".lock")
                    and _namespace(entry.name[8:-5])
                ):
                    _read(self.root / entry.name, 0)
                    namespace_locks.append(entry.name)
                elif entry.name.startswith(".cache-"):
                    info = entry.stat(follow_symlinks=False)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077
                        or info.st_nlink != 1
                    ):
                        raise CacheError("installer cache scratch identity changed")
                    (self.root / entry.name).unlink()
                elif entry.name not in {
                    "namespace.key",
                    "head.json",
                    "leases.lock",
                    "metadata.lock",
                    "protection.required",
                }:
                    raise CacheError("installer cache contains unknown metadata")
        remaining = len(directories)
        if all_artifacts:
            marker = self.root / "protection.required"
            if not marker.exists() and not marker.is_symlink():
                _atomic(marker, b"1\n")
            # Explicit recovery never parses corrupt head/key contents. Removing
            # these exact metadata names never follows a link or changes a target.
            for name in ("head.json", "namespace.key"):
                (self.root / name).unlink(missing_ok=True)
        for _, name in sorted(directories):
            try:
                total, _ = _inventory(self.root, reject_hardlinks=not all_artifacts)
            except CacheLimit:
                total = self.maximum_bytes + 1
            if (
                not all_artifacts
                and total <= self.maximum_bytes
                and remaining <= MAX_NAMESPACES
            ):
                break
            if not all_artifacts:
                head = self._head()
                if head is not None and head["namespace"] == name:
                    (self.root / "head.json").unlink()
            _remove(self.root, name)
            (self.root / ("install-" + name + ".lock")).unlink(missing_ok=True)
            remaining -= 1
        for name in namespace_locks:
            if all_artifacts or not (self.root / name[8:-5]).exists():
                (self.root / name).unlink(missing_ok=True)
        # Validate even an empty cache's metadata and every settled namespace.
        _inventory(self.root, reject_hardlinks=True)
        if all_artifacts:
            with _GUARDS_LOCK:
                enabled = _GUARDS.get(self.root, (0, 0))[1] > 0
            if not enabled:
                (self.root / "protection.required").unlink(missing_ok=True)
            descriptor = os.open(
                self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def register_guard(root: Path, *, enabled: bool) -> Callable[[], None]:
    """Pin check protection for one service lifetime, including purge gaps."""
    protected = enabled or requires_guard(root)
    if enabled:
        _directory(root)
        marker = root / "protection.required"
        if not marker.exists() and not marker.is_symlink():
            _atomic(marker, b"1\n")
    if not protected:
        return lambda: None
    with _GUARDS_LOCK:
        guards, consent = _GUARDS.get(root, (0, 0))
        _GUARDS[root] = guards + 1, consent + int(enabled)
    released = False

    def release() -> None:
        nonlocal released
        with _GUARDS_LOCK:
            if released:
                return
            released = True
            guards, consent = _GUARDS[root]
            if guards == 1:
                del _GUARDS[root]
            else:
                _GUARDS[root] = guards - 1, consent - int(enabled)

    return release


def requires_guard(root: Path) -> bool:
    """Only a proven absent or empty lock-only cache permits legacy checks."""
    with _GUARDS_LOCK:
        if _GUARDS.get(root, (0, 0))[0]:
            return True
    try:
        info = root.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        return True
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name not in {"leases.lock", "metadata.lock"}:
                    return True
                current = entry.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_uid != os.geteuid()
                    or current.st_mode & 0o077
                    or current.st_nlink != 1
                ):
                    return True
    except OSError:
        return True
    return False


def prune_profile_cache(
    root: Path, maximum_bytes: int, all_artifacts: bool
) -> dict[str, object]:
    """Explicit operator recovery; no daemon/publication barrier is held."""
    from llm_cli.errors import ErrorCode, LlmCoordError

    if type(all_artifacts) is not bool:
        raise ValueError("cache prune all must be boolean")
    if not root.exists() and not root.is_symlink():
        return {"state": "empty", "pruned": True, "all": all_artifacts}
    try:
        idle = InstallerCache(root, maximum_bytes).prune(all_artifacts=all_artifacts)
    except (CacheError, CacheLimit, OSError, ValueError):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "installer cache could not be pruned; disable it with "
            "loupe config set isolated.rebuild.cache false and retry",
        ) from None
    return {
        "state": "pruned" if idle else "busy",
        "pruned": idle,
        "all": all_artifacts,
        "notice": ""
        if idle
        else "Installer cache is in use; retry "
        "loupe workspace cache prune after current rebuilds finish.",
    }
