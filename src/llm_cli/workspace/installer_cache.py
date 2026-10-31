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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from llm_cli.paths import reject_symlink_components

DEFAULT_MAX_BYTES = 512 * 1024**2
MAX_BYTES = 5 * 1024**3
MAX_ENTRIES = 200_000
MAX_NAMESPACES = 32


class CacheLimit(ValueError):
    """Cache growth requires idle pruning before reuse."""


class CacheError(ValueError):
    """A cache failure contains only an operator-safe explanation."""


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
def _lock(path: Path, *, shared: bool = False, wait: bool = True) -> Iterator[bool]:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _safe_file(descriptor)
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(descriptor, operation | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
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


def _inventory(root: Path, *, maximum_entries: int = MAX_ENTRIES) -> tuple[int, int]:
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
        _directory(self.root)
        # All locks are installer-only and acquired outside daemon/publication
        # barriers. Shared leases prevent eviction across simultaneous opens.
        self.prune()
        try:
            with _lock(self.root / "leases.lock", shared=True):
                with _lock(self.root / "metadata.lock"):
                    target = self._select(values)
                yield target
        finally:
            self.prune()

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

    def prune(self) -> bool:
        _directory(self.root)
        with _lock(self.root / "leases.lock", wait=False) as idle:
            if not idle:
                return False
            with _lock(self.root / "metadata.lock"):
                directories = []
                with os.scandir(self.root) as entries:
                    for entry in entries:
                        if _namespace(entry.name):
                            info = entry.stat(follow_symlinks=False)
                            if not stat.S_ISDIR(info.st_mode):
                                raise CacheError(
                                    "installer cache namespace identity changed"
                                )
                            directories.append((info.st_mtime_ns, entry.name))
                        elif entry.name.startswith(".cache-"):
                            info = entry.stat(follow_symlinks=False)
                            if (
                                not stat.S_ISREG(info.st_mode)
                                or info.st_uid != os.geteuid()
                                or info.st_mode & 0o077
                                or info.st_nlink != 1
                            ):
                                raise CacheError(
                                    "installer cache scratch identity changed"
                                )
                            (self.root / entry.name).unlink()
                        elif entry.name not in {
                            "namespace.key",
                            "head.json",
                            "leases.lock",
                            "metadata.lock",
                        }:
                            raise CacheError(
                                "installer cache contains unknown metadata"
                            )
                remaining_namespaces = len(directories)
                for _, name in sorted(directories):
                    try:
                        total, _ = _inventory(self.root)
                    except CacheLimit:
                        total = self.maximum_bytes + 1
                    if (
                        total <= self.maximum_bytes
                        and remaining_namespaces <= MAX_NAMESPACES
                    ):
                        break
                    head = self._head()
                    if head is not None and head["namespace"] == name:
                        # Stop reusing an oversized namespace before cleanup;
                        # an interrupted prune cannot make it current again.
                        (self.root / "head.json").unlink()
                    _remove(self.root, name)
                    remaining_namespaces -= 1
                return True
