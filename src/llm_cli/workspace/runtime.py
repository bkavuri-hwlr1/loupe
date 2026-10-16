"""Private dependency relocation and value-based runtime output protection."""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import stat
import tempfile
import time
import tomllib
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
_YAML_FIELD = re.compile(
    r"(?=(?:^|[\s{,])(?:[\"']?)([A-Za-z_][\w.-]*)(?:[\"']?)\s*:\s*"
    r"(\"(?:\\.|[^\"\\])*\"|'(?:''|[^'])*'|[^,}\]\r\n]+))"
)
_YAML_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}


def _yaml_scalar(value: str) -> str:
    value = value.strip()
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value.startswith('"') and value.endswith('"'):

        def escape(match: re.Match[str]) -> str:
            part = match[1]
            if part[0] in {"x", "u", "U"}:
                return chr(int(part[1:], 16))
            return _YAML_ESCAPES.get(part, "\\" + part)

        return re.sub(
            r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.)",
            escape,
            value[1:-1],
        )
    return value.split(" #", 1)[0].strip()


def dependency_path(relative: str) -> bool:
    return Path(relative).name.casefold() in _DEPENDENCIES


def relocated_venv_text(relative: str, body: bytes) -> bool:
    """Whether the venv adapter relocates this specific text file."""
    if len(body) > 1024 * 1024 or b"\0" in body:
        return False
    parts = Path(relative).parts
    for index, part in enumerate(parts):
        if part not in {".venv", "venv"}:
            continue
        tail = parts[index + 1 :]
        if not tail:
            continue
        name = tail[-1]
        return (
            tail == ("pyvenv.cfg",)
            or name.endswith((".pth", ".egg-link"))
            or name.startswith("__editable__")
            or name == "direct_url.json"
            or (
                len(tail) == 2
                and tail[0] == "bin"
                and (
                    body.startswith(b"#!") or name.startswith(("activate", "Activate"))
                )
            )
        )
    return False


def _trusted_python(source: Path, relative: str, destination: str) -> Path:
    """Pin a venv's declared native base interpreter without executing it."""
    from llm_cli.workspace.isolated import _error, _parent, _read, _source

    parts = Path(relative).parts
    if (
        len(parts) < 3
        or parts[-3] not in {".venv", "venv"}
        or parts[-2] != "bin"
        or not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", parts[-1])
    ):
        raise _error("dependency link escapes its private tree: " + relative)
    config_path = Path(*parts[:-2], "pyvenv.cfg").as_posix()
    with _source(source, config_path) as descriptor:
        config_before = _evidence(os.fstat(descriptor))
        config_body = _read(descriptor, 64 * 1024)
        try:
            config = dict(
                (key.strip().casefold(), value.strip())
                for line in config_body.decode("utf-8").splitlines()
                if "=" in line
                for key, value in (line.split("=", 1),)
            )
        except UnicodeError as exc:
            raise _error("virtualenv configuration is not valid text") from exc
        home = Path(config.get("home", ""))
        if not home.is_absolute():
            raise _error("virtualenv needs an absolute base-interpreter home")
        resolved = (source / relative).resolve(strict=True)
        if resolved.is_relative_to(source.resolve()) or not re.fullmatch(
            r"python(?:[0-9]+(?:\.[0-9]+)*)?", resolved.name
        ):
            raise _error(
                "virtualenv base interpreter is not an external Python executable"
            )
        candidates = {home / resolved.name, home / parts[-1]}
        declared = config.get("executable")
        if declared:
            executable = Path(declared)
            if not executable.is_absolute():
                raise _error("virtualenv declares a nonabsolute base executable")
            candidates.add(executable)
        if not any(
            candidate.resolve(strict=True) == resolved
            for candidate in candidates
            if candidate.exists()
        ):
            raise _error(
                "virtualenv interpreter does not match its pinned configuration"
            )
        with _source(resolved.parent, resolved.name) as executable_fd:
            executable_before = _evidence(os.fstat(executable_fd))
            if not executable_before["mode"] & 0o111:
                raise _error("virtualenv base Python is not executable")
            magic = os.read(executable_fd, 4)
            if magic not in {
                b"\x7fELF",
                b"\xcf\xfa\xed\xfe",
                b"\xfe\xed\xfa\xcf",
                b"\xce\xfa\xed\xfe",
                b"\xfe\xed\xfa\xce",
                b"\xca\xfe\xba\xbe",
                b"\xbe\xba\xfe\xca",
                b"\xca\xfe\xba\xbf",
                b"\xbf\xba\xfe\xca",
            }:
                raise _error("virtualenv base Python must be a native executable")
            if executable_before != _evidence(os.fstat(executable_fd)):
                raise _error("virtualenv base Python changed during inspection")
            with _parent(resolved.parent, resolved.name) as (parent, name):
                if executable_before != _evidence(
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                ):
                    raise _error(
                        "virtualenv base Python path changed during inspection"
                    )
        if (
            config_before != _evidence(os.fstat(descriptor))
            or _read(descriptor, 64 * 1024) != config_body
        ):
            raise _error("virtualenv configuration changed during inspection")
        with _parent(source, config_path) as (parent, name):
            if config_before != _evidence(
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            ):
                raise _error("virtualenv configuration path changed during inspection")
    return resolved


def workspace_dependency_link(root: Path, relative: str, link: str) -> str:
    """Allow dependency links into ordinary private workspace source only."""
    from llm_cli.agent.source_policy import excluded_paths
    from llm_cli.workspace.broker import validate_symlink_target
    from llm_cli.workspace.isolated import _error

    try:
        destination = validate_symlink_target(root, relative, os.fsencode(link))
    except ValueError as exc:
        raise _error(str(exc)) from exc
    if destination in excluded_paths(root, [destination]):
        raise _error("dependency workspace link targets excluded source: " + relative)
    target = root / destination
    if not target.exists() or not (target.is_file() or target.is_dir()):
        raise _error("dependency workspace link target is unavailable: " + relative)
    return destination


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

    def budget(size: int = 0, *, entries: int = 1) -> None:
        nonlocal count, total
        count += entries
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
        budget(before.st_size, entries=0)
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
            if relocated_venv_text(path, candidate) and original_prefix in candidate:
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
                budget()
                path = prefix + "/" + entry.name
                observed = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                if entry.name.casefold() in _ADMIN:
                    raise _error("dependency tree contains administration: " + path)
                if (
                    _likely_secret(path)
                    and path not in approved_secret_paths
                    and not pinned_private
                    and not (
                        stat.S_ISDIR(observed.st_mode)
                        and any(
                            approved.startswith(path + "/")
                            for approved in approved_secret_paths
                        )
                    )
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
                    link = os.readlink(entry.name, dir_fd=descriptor)
                    resolved = posixpath.normpath(
                        posixpath.join(posixpath.dirname(path), link)
                    )
                    if not link.startswith("/") and (
                        resolved == relative or resolved.startswith(relative + "/")
                    ):
                        os.symlink(link, entry.name, dir_fd=destination)
                    elif (
                        not link.startswith("/")
                        and Path(relative).name == "node_modules"
                    ):
                        workspace_dependency_link(source, path, link)
                        # Source materialization already privately copied this
                        # target; never point back at primary workspace bytes.
                        workspace_dependency_link(target, path, link)
                        os.symlink(link, entry.name, dir_fd=destination)
                    else:
                        executable = _trusted_python(source, path, link)
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
        elif isinstance(value, (int, float, bool)):
            values.add(str(value))
            values.add(json.dumps(value))
        elif isinstance(value, dict):
            for item in value.values():
                add(item)
        elif isinstance(value, list):
            for item in value:
                add(item)

    def sensitive_fields(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and _SECRET_KEY.search(key):
                    add(item)
                else:
                    sensitive_fields(item)
        elif isinstance(value, list):
            for item in value:
                sensitive_fields(item)

    for relative in paths:
        for path, body in _files(root, relative):
            private = _likely_secret(path)
            if b"\0" in body and not private:
                continue
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                if not private:
                    continue
                # Match exactly what the supervisor's replacement decoder can
                # emit; private binary-ish inputs cannot bypass literal output
                # protection merely by containing a NUL or invalid UTF-8 byte.
                text = body.decode("utf-8", errors="replace")
            if private:
                add(text)
                text = text.replace("\0", "\n").replace("\ufffd", "\n")
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
            for parse in (json.loads, tomllib.loads):
                with suppress(ValueError):
                    structured = parse(text)
                    if private:
                        add(structured)
                    else:
                        sensitive_fields(structured)
            for line in text.splitlines():
                if ":" in line and _SECRET_KEY.search(line):
                    for field in _YAML_FIELD.finditer(line):
                        if _SECRET_KEY.search(field[1]):
                            with suppress(ValueError):
                                add(_yaml_scalar(field[2]))
                assignment = _ASSIGNMENT.match(line)
                if assignment and (private or _SECRET_KEY.search(assignment[1])):
                    add(assignment[2].strip().strip("\"'"))
                    with suppress(ValueError):
                        add(" ".join(shlex.split(assignment[2], comments=True)))
    return tuple(sorted(values, key=len, reverse=True))
