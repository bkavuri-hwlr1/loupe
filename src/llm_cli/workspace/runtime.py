"""Private dependency relocation and value-based runtime output protection."""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from urllib.parse import unquote, urlsplit

from llm_cli.workspace.hydration import _evidence, _likely_secret

_DEPENDENCIES = frozenset({".venv", "venv", "node_modules"})
_ADMIN = frozenset({".git", ".loupe", ".llm-coord", ".codex", ".agents"})
_MAX_FILE = 256 * 1024 * 1024
_MAX_BYTES = 5 * 1024**3
_MAX_ENTRIES = 200_000
_SECRET_KEY = re.compile(
    r"(?i)(?:secret|token|password|passwd|credential|api.?key|access.?key|authorization)"
)
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([^=:#]+)\s*[=:]\s*(.*?)\s*$")


def dependency_path(relative: str) -> bool:
    return Path(relative).name.casefold() in _DEPENDENCIES


def _trusted_python(source: Path, relative: str, destination: str) -> Path:
    """Accept only the host interpreter's base runtime as an external venv link."""
    from llm_cli.workspace.isolated import _error

    parts = Path(relative).parts
    if (
        len(parts) != 3
        or parts[0] not in {".venv", "venv"}
        or parts[1] != "bin"
        or not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", parts[2])
    ):
        raise _error("dependency link escapes its private tree: " + relative)
    resolved = (source / relative).resolve(strict=True)
    base = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    if resolved != base or not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise _error(
            "virtualenv interpreter needs an explicit portability adapter: " + relative
        )
    return resolved


def copy_dependency_tree(
    source: Path,
    target: Path,
    relative: str,
    *,
    approved_secret_paths: tuple[str, ...] = (),
    pinned_private: bool = False,
) -> tuple[str, ...]:
    """Copy a dependency root without writable aliases, following no tree links.

    Known credential files keep exact-file consent. Internal relative links
    remain internal; the running Python's external venv interpreter is copied
    through a narrowly validated read-only link. Absolute script paths move to the
    private destination. Nested administration, special nodes and mounts are
    rejected rather than followed.
    """
    from llm_cli.workspace.isolated import (
        _clone,
        _error,
        _parent,
        _read,
        _write_body,
    )

    if not dependency_path(relative):
        raise _error("no directory hydration adapter for " + relative)
    count = total = 0
    deadline = time.monotonic() + 60
    notices: list[str] = []
    clone_capability: bool | None = None
    original_prefix = os.fsencode(source)
    private_prefix = os.fsencode(target)

    def budget(size: int = 0) -> None:
        nonlocal count, total
        count += 1
        total += size
        if count > _MAX_ENTRIES or total > _MAX_BYTES or time.monotonic() > deadline:
            raise _error(
                "dependency hydration exceeded its entry, byte, or time budget"
            )

    def clone(descriptor: int, parent: int, name: str) -> bool:
        nonlocal clone_capability
        if clone_capability is None:
            # Probe only disposable bytes once for this device pair. A broken
            # clone implementation must never reach the user's dependencies.
            with tempfile.TemporaryDirectory(
                prefix=".loupe-cow-", dir=target
            ) as directory:
                probe = Path(directory)
                (probe / "source").write_bytes(b"private clone probe")
                source_fd = os.open(probe / "source", os.O_RDONLY | os.O_NOFOLLOW)
                destination_fd = os.open(
                    probe, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
                try:
                    clone_capability = _clone(source_fd, destination_fd, "target")
                    if clone_capability:
                        (probe / "target").write_bytes(b"destination changed")
                        if _read(source_fd, 1024) != b"private clone probe":
                            raise _error(
                                "filesystem clone did not isolate dependency writes"
                            )
                finally:
                    os.close(source_fd)
                    os.close(destination_fd)
        return bool(clone_capability) and _clone(descriptor, parent, name)

    def regular(descriptor: int, parent: int, name: str, path: str, mode: int) -> None:
        before = os.fstat(descriptor)
        budget(before.st_size)
        if before.st_size > _MAX_FILE:
            raise _error("dependency file exceeds its byte budget: " + path)
        available = os.fstatvfs(parent)
        if before.st_size > available.f_bavail * available.f_frsize:
            raise _error("insufficient free space for private dependencies")
        # Only small text launchers/configuration need relocation. Binary wheels
        # and executables retain their exact bytes and private COW blocks.
        body = None
        if Path(relative).name in {".venv", "venv"} and before.st_size <= 1024 * 1024:
            candidate = _read(descriptor, _MAX_FILE)
            if b"\0" not in candidate and original_prefix in candidate:
                body = candidate.replace(original_prefix, private_prefix)
        if body is not None:
            _write_body(parent, name, body, mode & 0o700)
        elif not clone(descriptor, parent, name):
            _write_body(parent, name, _read(descriptor, _MAX_FILE), mode & 0o700)
        else:
            os.chmod(name, mode & 0o700, dir_fd=parent, follow_symlinks=False)
        if _evidence(before) != _evidence(os.fstat(descriptor)):
            raise _error("dependency changed during hydration: " + path)

    def walk(descriptor: int, destination: int, prefix: str, device: int) -> None:
        budget()
        before = _evidence(os.fstat(descriptor))
        with os.scandir(descriptor) as entries:
            for entry in entries:
                path = prefix + "/" + entry.name
                observed = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                if entry.name.casefold() in _ADMIN:
                    raise _error("dependency tree contains administration: " + path)
                if (
                    _likely_secret(path)
                    and path not in approved_secret_paths
                    and not pinned_private
                ):
                    if len(notices) < 10:
                        notices.append(
                            "Credential runtime omitted without exact-file consent: "
                            + path
                        )
                    continue
                if observed.st_dev != device:
                    raise _error("dependency tree crosses a filesystem mount: " + path)
                if stat.S_ISDIR(observed.st_mode):
                    child = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                    os.mkdir(entry.name, 0o700, dir_fd=destination)
                    copied = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=destination,
                    )
                    try:
                        if _evidence(os.fstat(child)) != _evidence(observed):
                            raise _error(
                                "dependency directory changed during hydration: " + path
                            )
                        walk(child, copied, path, device)
                    finally:
                        os.close(child)
                        os.close(copied)
                elif stat.S_ISREG(observed.st_mode):
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                    try:
                        if _evidence(os.fstat(child)) != _evidence(observed):
                            raise _error(
                                "dependency file changed during hydration: " + path
                            )
                        regular(
                            child,
                            destination,
                            entry.name,
                            path,
                            stat.S_IMODE(observed.st_mode),
                        )
                    finally:
                        os.close(child)
                elif stat.S_ISLNK(observed.st_mode):
                    budget()
                    link = os.readlink(entry.name, dir_fd=descriptor)
                    resolved = posixpath.normpath(
                        posixpath.join(posixpath.dirname(path), link)
                    )
                    if not link.startswith("/") and (
                        resolved == relative or resolved.startswith(relative + "/")
                    ):
                        os.symlink(link, entry.name, dir_fd=destination)
                    else:
                        executable = _trusted_python(source, path, link)
                        # This exact base interpreter is trusted, read-only in
                        # the command sandbox, and independent of the checkout.
                        os.symlink(str(executable), entry.name, dir_fd=destination)
                else:
                    raise _error("dependency tree contains a special file: " + path)
                if _evidence(observed) != _evidence(
                    os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                ):
                    raise _error("dependency path changed during hydration: " + path)
        if before != _evidence(os.fstat(descriptor)):
            raise _error("dependency directory changed during hydration: " + prefix)
        os.fsync(destination)

    with (
        _parent(source, relative) as (parent, name),
        _parent(target, relative, create=True) as (destination, copied_name),
    ):
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        observed = os.fstat(descriptor)
        os.mkdir(copied_name, 0o700, dir_fd=destination)
        copied = os.open(
            copied_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=destination,
        )
        try:
            walk(descriptor, copied, relative, observed.st_dev)
            if _evidence(observed) != _evidence(
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            ):
                raise _error("dependency root changed during hydration: " + relative)
        finally:
            os.close(descriptor)
            os.close(copied)
    return tuple(notices)


def _files(root: Path, relative: str) -> Iterator[tuple[str, bytes]]:
    """Read a bounded private tree, never following a descendant symlink."""
    from llm_cli.workspace.isolated import _error, _parent, _read, _source

    target = root / relative
    if not target.is_dir():
        with _source(root, relative) as descriptor:
            yield relative, _read(descriptor, _MAX_FILE)
        return
    count = total = 0

    def walk(descriptor: int, prefix: str) -> Iterator[tuple[str, bytes]]:
        nonlocal count, total
        with os.scandir(descriptor) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_ENTRIES:
                    raise _error(
                        "private runtime exceeds output-screening entry budget"
                    )
                status = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                path = prefix + "/" + entry.name
                if stat.S_ISDIR(status.st_mode):
                    child = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                    try:
                        yield from walk(child, path)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(status.st_mode):
                    total += status.st_size
                    if total > _MAX_BYTES:
                        raise _error(
                            "private runtime exceeds output-screening byte budget"
                        )
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                    try:
                        yield path, _read(child, _MAX_FILE)
                    finally:
                        os.close(child)

    with _parent(root, relative) as (parent, name):
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        try:
            yield from walk(descriptor, relative)
        finally:
            os.close(descriptor)


def private_output_values(root: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
    """Keep actual private values in memory only; ordinary ignored files are inert."""
    values: set[str] = set()

    def add(value: object) -> None:
        if isinstance(value, str) and value.strip():
            values.add(value.strip())
            values.update(line.strip() for line in value.splitlines() if line.strip())
        elif isinstance(value, dict):
            for item in value.values():
                add(item)
        elif isinstance(value, list):
            for item in value:
                add(item)

    for relative in paths:
        for path, body in _files(root, relative):
            if b"\0" in body:
                continue
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                continue
            private = _likely_secret(path)
            if private:
                add(text)
                with suppress(ValueError):
                    words = shlex.split(text, comments=True)
                    for index, word in enumerate(words[:-1]):
                        if word.casefold() in {"login", "password", "account"}:
                            add(words[index + 1])
                for line in text.splitlines():
                    with suppress(ValueError):
                        parsed = urlsplit(line.strip())
                        if parsed.scheme and parsed.hostname:
                            add(unquote(parsed.password or ""))
                            add(unquote(parsed.username or ""))
            with suppress(ValueError):
                structured = json.loads(text)
                if private:
                    add(structured)
                elif isinstance(structured, dict):
                    for key, value in structured.items():
                        if _SECRET_KEY.search(key):
                            add(value)
            for line in text.splitlines():
                assignment = _ASSIGNMENT.match(line)
                if assignment and (private or _SECRET_KEY.search(assignment[1])):
                    add(assignment[2].strip().strip("\"'"))
                    with suppress(ValueError):
                        add(" ".join(shlex.split(assignment[2], comments=True)))
    return tuple(sorted(values, key=len, reverse=True))
