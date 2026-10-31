"""Explicit, bounded dependency installation in an owned private worktree.

The journal holds configuration and path/stat receipts only. Installer output
passes the normal command screener before it can become an opening notice.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import stat
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

from llm_cli.config.models import RebuildConfig
from llm_cli.workspace.hydration import _evidence

JOURNAL = "loupe-rebuild.json"
INSTALLER_HOME = "loupe-rebuild-home"
_MAX_ENTRIES = 200_000
_MAX_BYTES = 5 * 1024**3
_MAX_FILE = 256 * 1024**2
_MAX_DEPTH = 64
_ADMIN = frozenset({".git", ".loupe", ".llm-coord", ".codex", ".agents"})


def write_journal(git_dir: Path, journal: dict[str, Any]) -> None:
    from llm_cli.workspace.isolated import _parent, _write_body

    temporary = JOURNAL + ".new"
    with _parent(git_dir, temporary) as (parent, name):
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=parent)
        _write_body(
            parent,
            name,
            json.dumps(journal, sort_keys=True, ensure_ascii=False).encode(),
            0o600,
        )
        os.replace(name, JOURNAL, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)


def read_journal(git_dir: Path, token: str) -> dict[str, Any] | None:
    from llm_cli.workspace.isolated import _error, _read, _source

    try:
        with _source(git_dir, JOURNAL) as descriptor:
            value = json.loads(_read(descriptor, 512 * 1024))
    except FileNotFoundError:
        return None
    if (
        not isinstance(value, dict)
        or value.get("token") != token
        or value.get("home") != INSTALLER_HOME
    ):
        raise _error("dependency rebuild ownership changed")
    # Apply the same strict parser on recovery; corrupt metadata grants no paths
    # or installer permissions, even inside the private Git directory.
    from llm_cli.config.loader import _rebuild

    specs = value.get("specs")
    if not isinstance(specs, list):
        raise _error("dependency rebuild journal is invalid")
    try:
        parsed = _rebuild(
            {
                "rebuild": {
                    item["root"]: {
                        key: entry for key, entry in item.items() if key != "root"
                    }
                    for item in specs
                }
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _error("dependency rebuild journal is invalid") from exc
    if len(parsed) != len(specs) or not isinstance(value.get("results"), dict):
        raise _error("dependency rebuild journal is invalid")
    cache = value.get("cache", {"enabled": False, "max_bytes": 512 * 1024**2})
    if (
        not isinstance(cache, dict)
        or set(cache) != {"enabled", "max_bytes"}
        or type(cache["enabled"]) is not bool
        or type(cache["max_bytes"]) is not int
        or not 64 * 1024 <= cache["max_bytes"] <= 5 * 1024**3
    ):
        raise _error("dependency rebuild cache journal is invalid")
    value["cache"] = cache
    return value


def prepare(
    git_dir: Path,
    token: str,
    specs: Sequence[RebuildConfig],
    *,
    cache: bool = False,
    cache_max_bytes: int = 512 * 1024**2,
) -> None:
    write_journal(
        git_dir,
        {
            "token": token,
            "home": INSTALLER_HOME,
            "specs": [asdict(spec) for spec in specs],
            "cache": {"enabled": cache, "max_bytes": cache_max_bytes},
            "results": {},
        },
    )


def configured_roots(git_dir: Path, token: str) -> tuple[str, ...]:
    journal = read_journal(git_dir, token)
    return tuple(item["root"] for item in journal["specs"]) if journal else ()


def _remove(private: Path, relative: str) -> None:
    from llm_cli.workspace.isolated import IsolatedWorkspaceManager, _parent

    try:
        with _parent(private, relative) as (parent, name):
            IsolatedWorkspaceManager._remove_private_entry(parent, name)
            os.fsync(parent)
    except FileNotFoundError:
        pass


def discard_interrupted(private: Path, git_dir: Path, token: str) -> None:
    """Scrub owned partial inputs even when late source edits prevent replay."""
    journal = read_journal(git_dir, token)
    if journal is None:
        return
    _remove(git_dir, INSTALLER_HOME)
    for spec in journal["specs"]:
        if spec["root"] not in journal["results"]:
            _remove(private, spec["root"])


@contextmanager
def _installer_home(git_dir: Path) -> Iterator[Path]:
    """One journal-owned disposable home, including after a hard process exit."""
    from llm_cli.workspace.isolated import _parent

    _remove(git_dir, INSTALLER_HOME)
    with _parent(git_dir, INSTALLER_HOME) as (parent, name):
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    try:
        yield git_dir / INSTALLER_HOME
    finally:
        _remove(git_dir, INSTALLER_HOME)


def inspect_tree(
    private: Path, relative: str, *, validate: bool, reject_hardlinks: bool = False
) -> dict[str, str | int]:
    """Count sizes without following links; validate final dependency links."""
    from llm_cli.workspace.isolated import _error, _parent
    from llm_cli.workspace.runtime import _trusted_python, workspace_dependency_link

    identity = hashlib.sha256()
    count = logical = allocated = 0
    deadline = time.monotonic() + 5

    def walk(parent: int, name: str, path: str, device: int, depth: int) -> None:
        nonlocal count, logical, allocated
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        count += 1
        identity.update(path.encode() + b"\0")
        identity.update(json.dumps(_evidence(before), sort_keys=True).encode() + b"\0")
        if count > _MAX_ENTRIES or depth > _MAX_DEPTH or time.monotonic() > deadline:
            raise _error(
                "dependency rebuild exceeded entry, depth or observation time limit"
            )
        if before.st_dev != device or name.casefold() in _ADMIN:
            raise _error("dependency rebuild contains a mount or administration")
        if stat.S_ISREG(before.st_mode):
            if validate and reject_hardlinks and before.st_nlink != 1:
                raise _error("dependency rebuild contains a shared hardlink")
            logical += before.st_size
            allocated += getattr(before, "st_blocks", 0) * 512
            if before.st_size > _MAX_FILE or max(logical, allocated) > _MAX_BYTES:
                raise _error("dependency rebuild exceeded its size limit")
        elif stat.S_ISDIR(before.st_mode):
            descriptor = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            try:
                opened = os.fstat(descriptor)
                # Installers legitimately add entries between stat() and open().
                # Pin the same directory during live budget observations; full
                # metadata stability is required only for the finished receipt.
                if (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode)) != (
                    before.st_dev,
                    before.st_ino,
                    stat.S_IFMT(before.st_mode),
                ) or (validate and _evidence(opened) != _evidence(before)):
                    raise _error("dependency rebuild directory changed")
                names: list[str] = []
                with os.scandir(descriptor) as children:
                    for observed_child in children:
                        names.append(observed_child.name)
                        if (
                            count + len(names) > _MAX_ENTRIES
                            or time.monotonic() > deadline
                        ):
                            raise _error(
                                "dependency rebuild inventory exceeded its limit"
                            )
                for child in sorted(names):
                    walk(descriptor, child, path + "/" + child, device, depth + 1)
                if validate and _evidence(os.fstat(descriptor)) != _evidence(before):
                    raise _error("dependency rebuild directory changed")
            finally:
                os.close(descriptor)
        elif stat.S_ISLNK(before.st_mode):
            if validate:
                link = os.readlink(name, dir_fd=parent)
                resolved = posixpath.normpath(
                    posixpath.join(posixpath.dirname(path), link)
                )
                if not link.startswith("/") and (
                    resolved == relative or resolved.startswith(relative + "/")
                ):
                    pass
                elif not link.startswith("/") and Path(relative).name == "node_modules":
                    workspace_dependency_link(private, path, link)
                else:
                    _trusted_python(private, path, link)
        else:
            raise _error("dependency rebuild contains a special file")
        if validate and _evidence(
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        ) != _evidence(before):
            raise _error("dependency rebuild path changed")

    with _parent(private, relative) as (parent, name):
        root = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(root.st_mode):
            raise _error("dependency rebuild did not create a private directory")
        walk(parent, name, relative, root.st_dev, 0)
    return {
        "identity": identity.hexdigest(),
        "entries": count,
        "bytes": logical,
        "allocated": allocated,
    }


def run_pending(
    private: Path,
    primary: Path,
    git_dir: Path,
    token: str,
    *,
    runtime_files: tuple[str, ...],
    source_paths: tuple[str, ...],
    fault: Callable[[str], None],
    protected: tuple[Path, ...] = (),
    cache_root: Path | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    from llm_cli.execution.checks import supervised_run
    from llm_cli.execution.sandbox import (
        SandboxPolicy,
        available_sandbox,
        protected_home_paths,
        uses_reaper,
        wrap,
    )
    from llm_cli.workspace.isolated import _error, _parent
    from llm_cli.workspace.runtime import private_output_values

    journal = read_journal(git_dir, token)
    if journal is None:
        return (), ()
    _remove(git_dir, INSTALLER_HOME)
    cache_enabled = bool(journal["cache"]["enabled"])
    from llm_cli.workspace.installer_cache import CacheError, CacheLimit, InstallerCache

    cache = (
        InstallerCache(cache_root, journal["cache"]["max_bytes"])
        if cache_enabled and cache_root is not None
        else None
    )
    successful: list[str] = []
    notices: list[str] = []
    for raw in journal["specs"]:
        spec = RebuildConfig(
            raw["root"], tuple(raw["argv"]), raw["network"], raw["timeout_seconds"]
        )
        prior = journal["results"].get(spec.root)
        if prior is not None:
            if prior["state"] == "succeeded":
                if (
                    inspect_tree(
                        private,
                        spec.root,
                        validate=True,
                        reject_hardlinks=cache_enabled,
                    )
                    != prior["receipt"]
                ):
                    raise _error("completed dependency rebuild changed before recovery")
                successful.append(spec.root)
                notices.append("Dependency rebuilt privately: " + spec.root)
            else:
                notices.append(
                    "Dependency rebuild failed; partial tree removed: " + spec.root
                )
            continue
        # A missing result means the prior installer may have stopped at any
        # point. Remove its private root before replay, never primary bytes.
        _remove(private, spec.root)
        fault("rebuild_removed:" + spec.root)
        output = ""
        failure_reason = "installer failed"
        try:
            with _parent(private, spec.root, create=True) as (parent, name):
                os.mkdir(name, 0o700, dir_fd=parent)
                os.fsync(parent)
            sandbox = available_sandbox()
            if sandbox is None:
                failure_reason = "command sandbox unavailable"
                raise _error(failure_reason)
            values = private_output_values(
                private,
                runtime_files,
                source_paths=source_paths,
                strict_private=cache_enabled,
            )
            with _installer_home(git_dir) as home, ExitStack() as resources:
                if cache_enabled and cache is None:
                    failure_reason = (
                        "installer cache unavailable; disable isolated.rebuild.cache"
                    )
                    raise _error(failure_reason)
                cache_lease = (
                    resources.enter_context(
                        cache.lease(values, timeout=spec.timeout_seconds)
                    )
                    if cache
                    else None
                )
                cache_path = cache_lease.path if cache_lease else None
                home = home.resolve()
                home.chmod(0o700)
                (home / "tmp").mkdir(mode=0o700)
                env = {
                    "PATH": os.environ.get("PATH") or os.defpath,
                    "HOME": str(home),
                    "TMPDIR": str(home / "tmp"),
                    "XDG_CACHE_HOME": str(home / "cache"),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "TERM": "dumb",
                    "NO_COLOR": "1",
                    "GIT_TERMINAL_PROMPT": "0",
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
                if cache_path is not None:
                    for key, directory in (
                        ("XDG_CACHE_HOME", "xdg"),
                        ("UV_CACHE_DIR", "uv"),
                        ("npm_config_cache", "npm"),
                        ("PIP_CACHE_DIR", "pip"),
                    ):
                        target = cache_path / directory
                        target.mkdir(mode=0o700, exist_ok=True)
                        env[key] = str(target)
                    env["UV_LINK_MODE"] = "copy"
                limit_error: list[str] = []
                next_observation = 0.0

                def over_limit(
                    root: str = spec.root, errors: list[str] = limit_error
                ) -> bool:
                    nonlocal next_observation
                    if time.monotonic() < next_observation:
                        return bool(errors)
                    started = time.monotonic()
                    try:
                        inspect_tree(private, root, validate=False)
                        if cache is not None and cache.over_limit():
                            errors.append("cache")
                    except OSError:
                        # Package managers atomically rename intermediate files.
                        # Retry live observations; the final tree must validate.
                        pass
                    except ValueError:
                        errors.append("invalid")
                    except Exception:
                        errors.append("limit")
                    next_observation = time.monotonic() + max(
                        0.25, min(2.0, (time.monotonic() - started) * 10)
                    )
                    return bool(errors)

                policy = SandboxPolicy(
                    writable=(
                        private / spec.root,
                        home,
                        *((cache_path,) if cache_path else ()),
                    ),
                    protected=(
                        primary,
                        git_dir,
                        *(
                            (cache_root,)
                            if cache_enabled and cache_root is not None
                            else ()
                        ),
                        *protected_home_paths(Path.home()),
                        *protected,
                    ),
                    # The installer's disposable home is inside protected admin.
                    readable=(private,),
                    network=spec.network,
                )
                fault("rebuild_running:" + spec.root)
                code, output, _, stopped = supervised_run(
                    wrap(sandbox, policy, spec.argv, cwd=private),
                    cwd=private,
                    env=env,
                    deadline=time.monotonic() + spec.timeout_seconds,
                    remaining=64 * 1024,
                    cancelled=over_limit,
                    on_output=lambda text: None,
                    reaper=uses_reaper(sandbox),
                    sensitive_values=values,
                    supervisor_fds=cache_lease.supervisor_fds if cache_lease else (),
                )
                # Nothing streams from installers. Re-screen before retaining a
                # notice, including copied values edited privately during a run.
                captured, output = output, ""
                current_values = private_output_values(
                    private,
                    runtime_files,
                    source_paths=source_paths,
                    strict_private=cache_enabled,
                )
                if (
                    cache is not None
                    and cache_path is not None
                    and set(values) != set(current_values)
                ):
                    cache.invalidate(cache_path)
                for value in sorted(
                    set((*values, *current_values)), key=len, reverse=True
                ):
                    if value:
                        captured = captured.replace(value, "[private value]")
                output = captured
                if stopped is not None or code != 0 or limit_error:
                    failure_reason = (
                        "dependency size, entry or depth limit exceeded"
                        if limit_error
                        else "installer timed out"
                        if stopped == "timed_out"
                        else "installer exited unsuccessfully"
                    )
                    raise _error(failure_reason)
                failure_reason = "installed tree failed dependency validation"
                receipt = inspect_tree(
                    private, spec.root, validate=True, reject_hardlinks=cache_enabled
                )
            fault("rebuild_installed:" + spec.root)
            journal["results"][spec.root] = {"state": "succeeded", "receipt": receipt}
            successful.append(spec.root)
            notices.append("Dependency rebuilt privately: " + spec.root)
        except Exception as error:
            if isinstance(error, (CacheError, CacheLimit)):
                failure_reason = (
                    "installer cache needs recovery; run "
                    "loupe workspace cache prune --all "
                    "or disable isolated.rebuild.cache and retry"
                )
            _remove(private, spec.root)
            failure = (
                "Dependency rebuild failed; partial tree removed: "
                + spec.root
                + " ("
                + failure_reason
                + ")."
            )
            if output.strip():
                failure += "\n" + output[-2048:]
            notices.append(failure)
            journal["results"][spec.root] = {"state": "failed"}
        write_journal(git_dir, journal)
        fault("rebuild_recorded:" + spec.root)
    return tuple(successful), tuple(notices)
