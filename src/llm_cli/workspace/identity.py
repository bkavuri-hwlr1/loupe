"""Exact content identity for coordinated paths.

Divergence detection, optimistic publication, and the change ledger all rest on
one question: is the byte-for-byte state at this path still what a session
believed it was?  A Git blob ID cannot answer it.  Ignored and untracked files
have no blob, working-tree filters and EOL conversion mean a blob need not
materialize to the bytes on disk, and a blob says nothing about the executable
bit or about a path being absent.

So identity is computed over the tuple that actually decides equality -- kind,
normalized mode, and content -- and is versioned, because changing how it is
computed changes what every stored identity means.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

IDENTITY_VERSION = "1"

# Git's mode vocabulary is reused because publication has to agree with it, but
# it is normalized here so that a filesystem's extra permission bits cannot
# change an identity without changing anything Git or a reader would observe.
REGULAR_MODE = "100644"
EXECUTABLE_MODE = "100755"
SYMLINK_MODE = "120000"
DIRECTORY_MODE = "040000"
GITLINK_MODE = "160000"

_MAX_IDENTIFIED_BYTES = 256 * 1024 * 1024
_READ_ATTEMPTS = 3
_READ_CHUNK_BYTES = 64 * 1024


class ObjectKind(StrEnum):
    """What a coordinated path currently is."""

    ABSENT = "absent"
    REGULAR = "regular"
    SYMLINK = "symlink"
    DIRECTORY_MARKER = "directory_marker"
    GITLINK = "gitlink"


class IdentityError(ValueError):
    """A path cannot be given an exact identity."""


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """The exact observed state of one path."""

    kind: ObjectKind
    mode: str
    digest: str
    size: int = 0
    # Access permissions accompany the snapshot for publication, but do not
    # change Git/content identity or invalidate legacy observations.
    permissions: int | None = field(default=None, compare=False)

    @property
    def absent(self) -> bool:
        return self.kind is ObjectKind.ABSENT


def content_identity(kind: ObjectKind, mode: str, content: bytes = b"") -> str:
    """Return the versioned identity digest for an exact observed state.

    The parts are NUL-separated and the version leads, so no combination of
    kind, mode, and content can collide with a different combination, and a
    future change to this scheme cannot be mistaken for the current one.
    """

    if kind is ObjectKind.ABSENT and content:
        raise IdentityError("an absent path cannot carry content")
    digest = hashlib.sha256()
    for part in (IDENTITY_VERSION, str(kind), mode):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    digest.update(content)
    return digest.hexdigest()


ABSENT = FileIdentity(
    kind=ObjectKind.ABSENT,
    mode="",
    digest=content_identity(ObjectKind.ABSENT, ""),
)


def identify_path(path: Path) -> FileIdentity:
    """Observe one path exactly, without following it out of place.

    ``lstat`` rather than ``stat``: a symlink's identity is the link itself,
    not whatever it currently points at.  Resolving here would make two
    different workspace states look identical.
    """

    identity, _ = read_identified_path(path)
    return identity


def read_identified_path(
    path: Path, *, max_bytes: int = _MAX_IDENTIFIED_BYTES
) -> tuple[FileIdentity, bytes | None]:
    """Return an identity and its regular-file bytes from one validated read.

    Never open a leaf symlink. Compare the opened descriptor with the path's
    metadata before and after a bounded read, retrying editor replacements or
    in-place writes. A changing path fails closed after three attempts. This
    is an observed snapshot, not a lock against later external changes; callers
    must still compare its identity before publishing a replacement.

    Nonregular paths retain their usual identity and return no body. Parent
    path safety remains the caller's responsibility, as for ``identify_path``.
    """

    if max_bytes < 0:
        raise IdentityError("the read limit must not be negative")
    for _ in range(_READ_ATTEMPTS):
        try:
            return _read_identified_path_once(path, max_bytes=max_bytes)
        except _ObservationChanged:
            continue
        except OSError as exc:
            # The path may disappear, become a symlink before open, or stop
            # being a symlink before readlink. Re-observe its new kind.
            if exc.errno in {errno.ENOENT, errno.ELOOP, errno.EINVAL}:
                continue
            raise IdentityError(
                f"{path} could not be observed: {exc.strerror}"
            ) from exc
    raise IdentityError(f"{path} changed while being read; retry the read")


class _ObservationChanged(Exception):
    """Metadata changed during an attempt; discard its bytes and retry."""


def _read_identified_path_once(
    path: Path, *, max_bytes: int
) -> tuple[FileIdentity, bytes | None]:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return ABSENT, None

    if stat.S_ISLNK(status.st_mode):
        target = os.fsencode(os.readlink(path))
        _require_same_observation(status, path.lstat())
        identity = FileIdentity(
            kind=ObjectKind.SYMLINK,
            mode=SYMLINK_MODE,
            digest=content_identity(ObjectKind.SYMLINK, SYMLINK_MODE, target),
            size=len(target),
        )
        return identity, None
    if stat.S_ISDIR(status.st_mode):
        if (path / ".git").exists():
            # A nested repository is a gitlink to Git, not a directory of
            # files, and publishing into one would cross a repository boundary.
            identity = FileIdentity(
                kind=ObjectKind.GITLINK,
                mode=GITLINK_MODE,
                digest=content_identity(
                    ObjectKind.GITLINK, GITLINK_MODE, os.fsencode(str(path.name))
                ),
            )
        else:
            identity = FileIdentity(
                kind=ObjectKind.DIRECTORY_MARKER,
                mode=DIRECTORY_MODE,
                digest=content_identity(ObjectKind.DIRECTORY_MARKER, DIRECTORY_MODE),
            )
        _require_same_observation(status, path.lstat())
        return identity, None
    if not stat.S_ISREG(status.st_mode):
        raise IdentityError(f"{path} is neither a regular file, directory, nor symlink")
    if status.st_size > max_bytes:
        raise IdentityError(f"{path} exceeds the {max_bytes}-byte read limit")
    if not hasattr(os, "O_NOFOLLOW"):
        raise IdentityError("this platform cannot safely read without following links")

    # O_NONBLOCK prevents a FIFO swapped in after lstat from hanging open.
    # Its descriptor metadata will fail the regular-file observation check.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _require_same_observation(status, os.fstat(descriptor))
        chunks: list[bytes] = []
        byte_count = 0
        while byte_count <= max_bytes:
            chunk = os.read(
                descriptor, min(_READ_CHUNK_BYTES, max_bytes + 1 - byte_count)
            )
            if not chunk:
                break
            chunks.append(chunk)
            byte_count += len(chunk)
        if byte_count > max_bytes:
            raise IdentityError(f"{path} exceeds the {max_bytes}-byte read limit")
        _require_same_observation(status, os.fstat(descriptor))
        _require_same_observation(status, path.lstat())
        if byte_count != status.st_size:
            raise _ObservationChanged
    finally:
        os.close(descriptor)

    mode = EXECUTABLE_MODE if status.st_mode & stat.S_IXUSR else REGULAR_MODE
    content = b"".join(chunks)
    identity = FileIdentity(
        kind=ObjectKind.REGULAR,
        mode=mode,
        digest=content_identity(ObjectKind.REGULAR, mode, content),
        size=len(content),
        permissions=stat.S_IMODE(status.st_mode) & 0o777,
    )
    return identity, content


def _require_same_observation(before: os.stat_result, after: os.stat_result) -> None:
    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise _ObservationChanged


__all__ = [
    "ABSENT",
    "DIRECTORY_MODE",
    "EXECUTABLE_MODE",
    "GITLINK_MODE",
    "IDENTITY_VERSION",
    "REGULAR_MODE",
    "SYMLINK_MODE",
    "FileIdentity",
    "IdentityError",
    "ObjectKind",
    "content_identity",
    "identify_path",
    "read_identified_path",
]
