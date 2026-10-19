"""Read-only detection of tests writing to the account's actual authority root."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path


def _manifest(root: Path) -> dict[str, tuple[object, ...]]:
    entries: dict[str, tuple[object, ...]] = {}

    def visit(path: Path) -> None:
        try:
            status = path.lstat()
        except FileNotFoundError:
            if path == root:
                return
            raise
        relative = str(path.relative_to(root))
        details: tuple[object, ...] = (
            status.st_dev,
            status.st_ino,
            status.st_mode,
            status.st_uid,
            status.st_gid,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
        )
        if stat.S_ISLNK(status.st_mode):
            details += (os.readlink(path),)
        elif stat.S_ISREG(status.st_mode):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                digest = hashlib.sha256()
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        digest.update(chunk)
                details += (digest.hexdigest(),)
            finally:
                os.close(descriptor)
        entries[relative] = details
        if stat.S_ISDIR(status.st_mode):
            with os.scandir(path) as children:
                names = sorted(child.name for child in children)
            for name in names:
                visit(path / name)

    visit(root)
    return entries


@dataclass(frozen=True)
class AuthorityRootGuard:
    root: Path
    before: dict[str, tuple[object, ...]]

    @classmethod
    def capture(cls, root: Path) -> AuthorityRootGuard:
        return cls(root, _manifest(root))

    def assert_unchanged(self) -> None:
        after = _manifest(self.root)
        changed = sorted(
            path
            for path in self.before.keys() | after.keys()
            if self.before.get(path) != after.get(path)
        )
        assert not changed, (
            "tests changed the real account authority root "
            f"{self.root}; use install_test_authority or isolated_daemon_autostart. "
            f"Changed paths: {', '.join(changed[:10])}"
        )
