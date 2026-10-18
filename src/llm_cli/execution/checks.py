"""Configured verification commands against disposable source snapshots."""

from __future__ import annotations

import codecs
import hashlib
import hmac
import json
import os
import posixpath
import re
import selectors
import shutil
import stat
import subprocess
import sys
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import RLock
from typing import Any

from llm_cli.agent.source_policy import (
    contains_secret_material,
    ensure_safe_content,
    register_runtime_paths,
    runtime_path,
)
from llm_cli.agent.tools import TaskCancelled, ToolOutcome
from llm_cli.coordination.scopes import normalize_changed_path
from llm_cli.errors import LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.git.worktrees import (
    ManagedWorktree,
    create_managed_worktree,
    remove_managed_worktree,
)
from llm_cli.ids import new_id
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.batches import BatchFile, candidate_target
from llm_cli.workspace.broker import validate_symlink_target
from llm_cli.workspace.identity import (
    ABSENT,
    DIRECTORY_MODE,
    EXECUTABLE_MODE,
    REGULAR_MODE,
    SYMLINK_MODE,
    FileIdentity,
    ObjectKind,
    content_identity,
    read_identified_path,
)
from llm_cli.workspace.workflow import TaskWorkflow, encode_files

# Runtime equality evidence must not reveal guessable hashes of private inputs.
# A fresh process invalidates old runtime-dependent checks; source-only checks
# retain their previous identity across restarts.
_RUNTIME_EVIDENCE_KEY = os.urandom(32)
_OUTPUT_LIMIT = 10 * 1024 * 1024
# Attempts at building a check's snapshot when Git fails while doing so.
_PREPARE_ATTEMPTS = 3
# Output still arriving this long after the supervisor exits comes from a
# process that left the command's process group, and is not waited for.
_DRAIN_SECONDS = 1.0
# The supervisor's stop sequence takes a few seconds. A stopped run waits
# this long for it before abandoning the supervisor.
_STOP_SECONDS = 15.0


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def validate_config(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) - {"checks", "runtime_paths", "setup"}:
        raise ValueError(
            "Checks configuration accepts checks, runtime_paths, and setup only."
        )
    checks = value.get("checks", {})
    if not isinstance(checks, dict) or len(checks) > 50:
        raise ValueError("checks must contain at most 50 named tables")
    normalized: dict[str, Any] = {"checks": {}, "runtime_paths": [], "setup": []}
    for name, spec in checks.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 80
            or not isinstance(spec, dict)
        ):
            raise ValueError("invalid named check")
        if set(spec) - {"argv", "cwd", "timeout", "required", "env"}:
            raise ValueError(f"unknown check setting for {name}")
        argv = _argv(spec.get("argv"))
        cwd = spec.get("cwd", ".")
        if not isinstance(cwd, str):
            raise ValueError("check cwd must be a relative path")
        if cwd != ".":
            cwd = normalize_changed_path(cwd)
        timeout = spec.get("timeout", 600)
        if type(timeout) is not int or not 1 <= timeout <= 7200:
            raise ValueError("check timeout must be 1..7200 seconds")
        required = spec.get("required", True)
        env = spec.get("env", {})
        if (
            not isinstance(required, bool)
            or not isinstance(env, dict)
            or any(
                not isinstance(k, str)
                or not k
                or "=" in k
                or "\0" in k
                or not isinstance(v, str)
                or "\0" in v
                for k, v in env.items()
            )
        ):
            raise ValueError("invalid check environment or required flag")
        normalized["checks"][name] = dict(
            argv=argv, cwd=cwd, timeout=timeout, required=required, env=env
        )
    runtime = value.get("runtime_paths", [])
    setup = value.get("setup", [])
    if not isinstance(runtime, list) or not isinstance(setup, list):
        raise ValueError("runtime_paths and setup must be arrays")
    normalized["runtime_paths"] = [
        normalize_changed_path(p) for p in runtime if isinstance(p, str)
    ]
    if len(normalized["runtime_paths"]) != len(runtime):
        raise ValueError("runtime paths must be strings")
    normalized["setup"] = [_argv(command) for command in setup]
    return normalized


def _argv(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 100
        or any(
            not isinstance(arg, str) or "\0" in arg or len(arg) > 8192 for arg in value
        )
        or not value[0]
    ):
        raise ValueError("command argv must be a nonempty array of strings")
    return value


# Secret classification depends only on exact captured bytes. Keep its boolean
# result, never a body, across repeated checks of the same source identity.
_SOURCE_SCREENING: dict[str, bool] = {}


@contextmanager
def _source_reader(
    root: Path,
) -> Iterator[Callable[[str], tuple[FileIdentity, bytes | None]]]:
    """Share pinned nofollow parents within one bounded source capture."""
    from llm_cli.workspace.isolated import _read

    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    parents: OrderedDict[str, int] = OrderedDict()
    observed: dict[str, tuple[int, int]] = {}

    def parent(relative: str) -> int:
        if not relative:
            return root_fd
        if relative in parents:
            parents.move_to_end(relative)
            return parents[relative]
        prefix, _, name = relative.rpartition("/")
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent(prefix),
        )
        try:
            status = os.fstat(descriptor)
            evidence = (status.st_dev, status.st_ino)
            if relative in observed and observed[relative] != evidence:
                raise ValueError("source parent changed during capture")
            try:
                os.stat(".git", dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("source snapshot cannot enter a nested repository")
            observed[relative] = evidence
            parents[relative] = descriptor
            if len(parents) > 128:
                _, expired = parents.popitem(last=False)
                os.close(expired)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def read(relative: str) -> tuple[FileIdentity, bytes | None]:
        relative = normalize_changed_path(relative)
        prefix, _, name = relative.rpartition("/")
        try:
            directory = parent(prefix)
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return ABSENT, None
        if stat.S_ISLNK(before.st_mode):
            body = os.fsencode(os.readlink(name, dir_fd=directory))
            mode, kind = SYMLINK_MODE, ObjectKind.SYMLINK
        elif stat.S_ISREG(before.st_mode):
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            try:
                if _stat_signature(os.fstat(descriptor)) != _stat_signature(before):
                    raise ValueError("source changed during capture")
                body = _read(descriptor, 256 * 1024 * 1024)
                if len(body) != before.st_size or _stat_signature(
                    os.fstat(descriptor)
                ) != _stat_signature(before):
                    raise ValueError("source changed during capture")
            finally:
                os.close(descriptor)
            mode = EXECUTABLE_MODE if before.st_mode & stat.S_IXUSR else REGULAR_MODE
            kind = ObjectKind.REGULAR
        else:
            raise ValueError(
                "source snapshot cannot include special files or repositories"
            )
        if _stat_signature(
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        ) != _stat_signature(before):
            raise ValueError("source path changed during capture")
        return FileIdentity(
            kind,
            mode,
            content_identity(kind, mode, body),
            len(body),
            stat.S_IMODE(before.st_mode),
        ), body

    try:
        yield read
        pinned, current = os.fstat(root_fd), root.lstat()
        if (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("source root changed during capture")
        for relative, evidence in observed.items():
            target = candidate_target(root, relative)
            status = target.lstat()
            if (status.st_dev, status.st_ino) != evidence or not stat.S_ISDIR(
                status.st_mode
            ):
                raise ValueError("source parent changed during capture")
            if (target / ".git").exists() or (target / ".git").is_symlink():
                raise ValueError("source snapshot cannot enter a nested repository")
    finally:
        for descriptor in parents.values():
            os.close(descriptor)
        os.close(root_fd)


def source_snapshot(
    root: Path,
    *,
    contents: bool = False,
    _modes: dict[str, str] | None = None,
    _screened: set[str] | None = None,
) -> tuple[str, dict[str, bytes | None]]:
    result = run_git(
        root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    )
    assert isinstance(result.stdout, str)
    paths = sorted(
        path
        for path in set(filter(None, result.stdout.split("\0")))
        if not runtime_path(root, path)
    )
    if len(paths) > 100_000:
        raise ValueError("source snapshot exceeds 100,000 paths")
    manifest: list[tuple[str, str, str, str]] = []
    bodies: dict[str, bytes | None] = {}
    total = 0
    with _source_reader(root) as read:
        for path in paths:
            identity, body = read(path)
            if identity.kind is ObjectKind.SYMLINK:
                validate_symlink_target(root, path, body or b"")
            if identity.kind not in {
                ObjectKind.REGULAR,
                ObjectKind.SYMLINK,
                ObjectKind.ABSENT,
            }:
                raise ValueError(
                    "Snapshot cannot include special files "
                    f"or nested repositories: {path}"
                )
            total += identity.size
            if total > 1024**3:
                raise ValueError("source snapshot exceeds 1 GiB")
            manifest.append((path, str(identity.kind), identity.mode, identity.digest))
            if contents:
                bodies[path] = body
            if _modes is not None:
                _modes[path] = identity.mode
            if _screened is not None and body is not None:
                screened = _SOURCE_SCREENING.get(identity.digest)
                if screened is None:
                    screened = contains_secret_material(body)
                    if len(_SOURCE_SCREENING) >= 40_000:
                        _SOURCE_SCREENING.clear()
                    _SOURCE_SCREENING[identity.digest] = screened
                if screened:
                    _screened.add(path)
    return digest(json.dumps(manifest)), bodies


def materialize_snapshot_file(
    root: Path, path: str, body: bytes | None, mode: str
) -> None:
    """Install captured source without following a link from the old snapshot."""
    target = candidate_target(root, path)
    if mode == DIRECTORY_MODE:
        target.mkdir(parents=True, exist_ok=True)
        return
    if target.is_symlink() or target.is_file():
        target.unlink()
    if body is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == SYMLINK_MODE:
        validate_symlink_target(root, path, body)
        target.symlink_to(os.fsdecode(body))
    else:
        target.write_bytes(body)
        target.chmod(0o755 if mode == "100755" else 0o644)


# A check fingerprints the same private runtime several times; a dependency tree
# can hold thousands of files. Unchanged files, by exact path and full stat
# signature, reuse their digest; any write changes ctime and rehashes.
_RUNTIME_DIGESTS: dict[str, tuple[tuple[int, ...], str, int]] = {}
_RUNTIME_DIGESTS_LIMIT = 400_000


def _stat_signature(status: os.stat_result) -> tuple[int, ...]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _remember_runtime_copy(
    source: Path,
    target: Path,
    relative: str,
    before: os.stat_result,
    installed: os.stat_result,
) -> None:
    """Carry verified private-copy evidence to its fresh destination identity."""
    cached = _RUNTIME_DIGESTS.get(os.fsdecode(source) + "/" + relative)
    if cached is None or cached[0] != _stat_signature(before):
        return
    if before.st_size <= 1024 * 1024 < installed.st_size:
        return
    if len(_RUNTIME_DIGESTS) >= _RUNTIME_DIGESTS_LIMIT:
        _RUNTIME_DIGESTS.clear()
    _RUNTIME_DIGESTS[os.fsdecode(target) + "/" + relative] = (
        _stat_signature(installed),
        cached[1],
        installed.st_size,
    )


def runtime_identity(
    root: Path, paths: list[str], *, workspace_root: Path | None = None
) -> str:
    """Bind stable private inputs with descriptor-based, cached observations."""
    from llm_cli.workspace.isolated import _parent, _read
    from llm_cli.workspace.runtime import (
        _trusted_python,
        is_retained_runtime,
        relocated_venv_text,
        workspace_dependency_link,
    )

    manifest: list[tuple[str, str]] = []
    total = count = 0
    root_name = os.fsdecode(root)

    def observe(parent: int, name: str, relative: str, runtime_root: str) -> None:
        nonlocal count, total
        count += 1
        if count > 200_000:
            raise ValueError("runtime snapshot exceeds 200,000 entries")
        status = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(status.st_mode):
            link = os.readlink(name, dir_fd=parent)
            if Path(link).is_absolute():
                executable = _trusted_python(root, relative, link)
                identity, _ = read_identified_path(executable)
                manifest.append((relative + ":base-python", identity.digest))
            else:
                destination = posixpath.normpath(
                    posixpath.join(posixpath.dirname(relative), link)
                )
                if destination != runtime_root and not destination.startswith(
                    runtime_root + "/"
                ):
                    if workspace_root is not None or not is_retained_runtime(root):
                        workspace_dependency_link(
                            workspace_root or root, relative, link
                        )
                    else:
                        # Frozen generations deliberately omit ordinary source.
                        # Evidence binds the exact link; prepare validates its
                        # target against the fully merged source snapshot.
                        from llm_cli.workspace.broker import validate_symlink_target

                        validate_symlink_target(root, relative, os.fsencode(link))
            manifest.append((relative, digest(link)))
            return
        if stat.S_ISDIR(status.st_mode):
            descriptor = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            try:
                if _stat_signature(os.fstat(descriptor)) != _stat_signature(status):
                    raise ValueError("runtime directory changed during observation")
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        observe(
                            descriptor,
                            entry.name,
                            relative + "/" + entry.name,
                            runtime_root,
                        )
                if _stat_signature(os.fstat(descriptor)) != _stat_signature(status):
                    raise ValueError("runtime directory changed during observation")
            finally:
                os.close(descriptor)
            return
        if not stat.S_ISREG(status.st_mode):
            raise ValueError("runtime snapshot contains an absent or special file")
        key = root_name + "/" + relative
        before = _stat_signature(status)
        cached = _RUNTIME_DIGESTS.get(key)
        if cached is not None and cached[0] == before:
            file_digest, size = cached[1], cached[2]
        else:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                if _stat_signature(os.fstat(descriptor)) != before:
                    raise ValueError("runtime file changed during observation")
                body = _read(descriptor, 256 * 1024 * 1024)
                if (
                    _stat_signature(os.fstat(descriptor)) != before
                    or _stat_signature(
                        os.stat(name, dir_fd=parent, follow_symlinks=False)
                    )
                    != before
                ):
                    raise ValueError("runtime file changed during observation")
            finally:
                os.close(descriptor)
            if relocated_venv_text(relative, body):
                body = body.replace(os.fsencode(root), b"<private-runtime-root>")
            file_digest, size = hashlib.sha256(body).hexdigest(), status.st_size
            if len(_RUNTIME_DIGESTS) >= _RUNTIME_DIGESTS_LIMIT:
                _RUNTIME_DIGESTS.clear()
            _RUNTIME_DIGESTS[key] = (before, file_digest, size)
        total += size
        if total > 5 * 1024**3:
            raise ValueError("runtime snapshot exceeds 5 GiB")
        manifest.append((relative, file_digest))

    selected: list[str] = []
    for path in sorted(set(paths)):
        if any(path.startswith(parent + "/") for parent in selected):
            continue
        candidate_target(root, path)
        selected.append(path)
        with _parent(root, path) as (parent, name):
            observe(parent, name, path, path)
    encoded = json.dumps(sorted(manifest))
    if not paths:
        return digest(encoded)
    return hmac.new(_RUNTIME_EVIDENCE_KEY, encoded.encode(), "sha256").hexdigest()


def verification_identity(
    root: Path, config: dict[str, Any], *, runtime_root: Path | None = None
) -> str:
    return digest(
        source_snapshot(root)[0]
        + runtime_identity(runtime_root or root, config.get("runtime_paths", []))
    )


def verification_current(
    store: ControlStore,
    workflow: dict[str, Any],
    root: Path,
    files: tuple[BatchFile, ...],
    *,
    runtime_root: Path | None = None,
) -> bool:
    config = json.loads(workflow["config_json"])
    required = [
        name for name, check in config.get("checks", {}).items() if check["required"]
    ]
    if not required:
        return True
    source = verification_identity(root, config, runtime_root=runtime_root)
    candidate = digest(encode_files(files))
    config_digest = digest(workflow["config_json"])
    with store.connection() as con:
        for name in required:
            row = con.execute(
                (
                    "SELECT state FROM check_runs WHERE task_id=? AND attemp"
                    "t=? AND name=? AND candidate_digest=? AND source_digest"
                    "=? AND config_digest=? ORDER BY created_at DESC,run_id "
                    "DESC LIMIT 1"
                ),
                (
                    workflow["task_id"],
                    workflow["attempt"],
                    name,
                    candidate,
                    source,
                    config_digest,
                ),
            ).fetchone()
            if row is None or row[0] != "passed":
                return False
    return True


def published_verification(workflow: dict[str, Any]) -> str:
    """The verification status implied by an attempted publication.

    Publication requires current passing evidence for every required check
    (:func:`verification_current`), so a journaled batch was verified exactly
    when its workflow requires checks. Recovery uses this after the checkout
    has changed, when recomputing the evidence would wrongly call it stale.
    """

    config = json.loads(workflow["config_json"])
    required = any(check["required"] for check in config.get("checks", {}).values())
    return "passed" if required else "not_applicable"


def verification_status(
    store: ControlStore,
    workflow: dict[str, Any],
    root: Path,
    files: tuple[BatchFile, ...],
    *,
    runtime_root: Path | None = None,
) -> str:
    """Describe the verification evidence for this exact source and proposal.

    This is presentation metadata, not an authority check. Publication must keep
    using :func:`verification_current`; this helper prevents configured checks,
    stale evidence, and read-only tasks from all collapsing into a misleading
    passed/not-verified boolean.
    """

    if not files:
        return "not_applicable"
    config = json.loads(workflow["config_json"])
    required = [
        name for name, check in config.get("checks", {}).items() if check["required"]
    ]
    if not required:
        return "not_applicable"
    source = verification_identity(root, config, runtime_root=runtime_root)
    candidate = digest(encode_files(files))
    config_digest = digest(workflow["config_json"])
    saw_stale = False
    saw_interrupted = False
    saw_not_run = False
    with store.connection() as con:
        for name in required:
            rows = con.execute(
                (
                    "SELECT state,candidate_digest,source_digest,config_digest "
                    "FROM check_runs WHERE task_id=? AND attempt=? AND name=? "
                    "ORDER BY created_at DESC,run_id DESC"
                ),
                (workflow["task_id"], workflow["attempt"], name),
            ).fetchall()
            if not rows:
                saw_not_run = True
                continue
            current = next(
                (
                    row
                    for row in rows
                    if row["candidate_digest"] == candidate
                    and row["source_digest"] == source
                    and row["config_digest"] == config_digest
                ),
                None,
            )
            if current is None:
                saw_stale = True
                continue
            state = str(current["state"])
            if state == "passed":
                continue
            if state in {"running", "cancelled", "timed_out", "uncertain"}:
                saw_interrupted = True
                continue
            return "failed"
    if saw_interrupted:
        return "interrupted"
    if saw_stale:
        return "stale"
    if saw_not_run:
        return "not_run"
    return "passed"


class CheckRunner:
    def __init__(
        self,
        workflow: TaskWorkflow,
        row: dict[str, Any],
        root: Path,
        managed_root: Path,
        candidates: Callable[[], tuple[BatchFile, ...]],
        cancelled: Callable[[], bool],
        emit: Callable[[str, dict[str, object]], None],
        lock: RLock,
        deadline_at: float | None = None,
        on_result: Callable[[str, str, str], None] | None = None,
        runtime_paths: tuple[str, ...] | None = None,
        suppress_output: bool = False,
        runtime_root: Path | None = None,
        sensitive_paths: tuple[str, ...] = (),
    ) -> None:
        self.workflow, self.row, self.root = workflow, row, root
        self.runtime_root = runtime_root or root
        # Told (name, state, output) after every run, for repair limits.
        self.on_result = on_result
        self.store = workflow.store
        self.managed_root, self.candidates, self.cancelled = (
            managed_root,
            candidates,
            cancelled,
        )
        self.emit, self.lock = emit, lock
        self.config = json.loads(row["config_json"])
        if runtime_paths is not None:
            self.config["runtime_paths"] = sorted(
                set(self.config.get("runtime_paths", [])) | set(runtime_paths)
            )
        self.suppress_output = suppress_output
        self.sensitive_paths = sensitive_paths
        self.sensitive_values: tuple[str, ...] = ()
        self.deadline_at = deadline_at or (time.time() + 7200)

    def gate(self) -> ToolOutcome | None:
        if not self.config.get("checks"):
            return None
        # Reuse only evidence for this exact source and candidate; run every
        # missing required check. Failed checks are returned to the agent.
        failed = []
        for name, spec in self.config["checks"].items():
            if not spec["required"]:
                continue
            if not self._passed(name):
                result = self.run(name)
                if result.is_error:
                    failed.append(result.content)
        return (
            ToolOutcome(
                "Required checks did not pass. Repair the proposal before finishing.\n"
                + "\n".join(failed),
                True,
            )
            if failed
            else None
        )

    def current(self) -> bool:
        """Whether every required check passed on this exact source and proposal."""

        return all(
            self._passed(name)
            for name, spec in self.config.get("checks", {}).items()
            if spec["required"]
        )

    def _passed(self, name: str) -> bool:
        files = self.candidates()
        source = verification_identity(
            self.root, self.config, runtime_root=self.runtime_root
        )
        with self.store.connection() as con:
            row = con.execute(
                (
                    "SELECT state FROM check_runs WHERE task_id=? AND attemp"
                    "t=? AND name=? AND candidate_digest=? AND source_digest"
                    "=? AND config_digest=? ORDER BY created_at DESC,run_id "
                    "DESC LIMIT 1"
                ),
                (
                    self.row["task_id"],
                    self.row["attempt"],
                    name,
                    digest(encode_files(files)),
                    source,
                    digest(self.row["config_json"]),
                ),
            ).fetchone()
        return bool(row and row[0] == "passed")

    def run(self, name: str) -> ToolOutcome:
        spec = self.config.get("checks", {}).get(name)
        if spec is None:
            return ToolOutcome(
                "Unknown check. Configured names: "
                + ", ".join(self.config.get("checks", {})),
                True,
            )
        if self.cancelled():
            raise TaskCancelled("task stopped")
        run_id = new_id("check")
        started = time.monotonic()
        deadline = started + min(
            spec["timeout"], max(0, self.deadline_at - time.time())
        )
        worktree = None
        output = ""
        truncated = False
        state, exit_code = "error", None
        source = ""
        files = self.candidates()
        with self.store.connection() as con:
            con.execute(
                (
                    "INSERT INTO check_runs(run_id,task_id,attempt,name,stat"
                    "e,candidate_digest,source_digest,config_digest,created_"
                    "at) VALUES (?,?,?,?,?,?,?,?,?)"
                ),
                (
                    run_id,
                    self.row["task_id"],
                    self.row["attempt"],
                    name,
                    "running",
                    digest(encode_files(files)),
                    "",
                    digest(self.row["config_json"]),
                    self.store._now(None),
                ),
            )
        self.emit("check.started", {"run_id": run_id, "name": name})
        try:
            for attempt in range(_PREPARE_ATTEMPTS):
                try:
                    worktree, source = self._prepare(run_id, files)
                    break
                except LlmCoordError:
                    # Git can fail transiently while another task's check
                    # creates or removes its snapshot in the same repository.
                    # A failed preparation leaves nothing behind.
                    if attempt + 1 == _PREPARE_ATTEMPTS:
                        raise
            assert worktree is not None
            snapshot = worktree.path
            private = snapshot.parent / (run_id + "-runtime")
            private.mkdir(mode=0o700)
            env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": str(private),
                "TMPDIR": str(private),
                "XDG_CACHE_HOME": str(private / "cache"),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "LC_ALL": "C.UTF-8",
            }
            env.update(spec["env"])
            from llm_cli.workspace.runtime import private_output_values

            self.sensitive_values = private_output_values(
                snapshot,
                tuple(sorted(set(self.config.get("runtime_paths", [])))),
                source_paths=tuple(
                    sorted(set(self.sensitive_paths) | self._snapshot_sensitive_paths)
                ),
            )
            before = source_snapshot(snapshot)[0]
            commands = [*self.config.get("setup", []), spec["argv"]]
            for index, argv in enumerate(commands):
                cwd = snapshot if index < len(commands) - 1 else snapshot / spec["cwd"]
                if not cwd.resolve().is_relative_to(snapshot) or not cwd.is_dir():
                    raise ValueError(
                        "Check working directory is unavailable in the snapshot."
                    )
                exit_code, text, clipped, stopped = self._command(
                    argv,
                    cwd,
                    env,
                    deadline,
                    run_id,
                    _OUTPUT_LIMIT - len(output.encode()),
                )
                output += text
                truncated |= clipped
                if stopped:
                    state = stopped
                    break
                if exit_code:
                    state = "failed"
                    break
            else:
                state = "passed"
            if source_snapshot(snapshot)[0] != before:
                state = "source_mutated"
                output += "\nCheck/setup modified source; those changes were discarded."
        except (OSError, ValueError, RuntimeError, LlmCoordError) as exc:
            state = "cancelled" if self.cancelled() else "error"
            output += "\n" + str(exc)
        finally:
            if worktree is not None:
                with suppress(Exception):
                    remove_managed_worktree(
                        self.root,
                        managed_root=self.managed_root,
                        registered_path=worktree.path,
                        force=True,
                    )
                runtime_path = worktree.path.parent / (run_id + "-runtime")
                if runtime_path.exists():
                    shutil.rmtree(runtime_path)
            duration = time.monotonic() - started
            if self.suppress_output:
                output = "[Check output withheld: private runtime inputs.]"
            with self.store.connection() as con:
                con.execute(
                    (
                        "UPDATE check_runs SET state=?,source_digest=?,exit_code"
                        "=?,duration=?,output=?,truncated=? WHERE run_id=?"
                    ),
                    (
                        state,
                        source,
                        exit_code,
                        duration,
                        output[:_OUTPUT_LIMIT],
                        int(truncated),
                        run_id,
                    ),
                )
            self.emit(
                "check.finished",
                {
                    "run_id": run_id,
                    "name": name,
                    "state": state,
                    "exit_code": exit_code,
                    "duration": duration,
                    "truncated": truncated,
                },
            )
        if self.cancelled():
            raise TaskCancelled("task stopped")
        if self.on_result is not None:
            self.on_result(name, state, output)
        return ToolOutcome(
            f"{name}: {state} (exit {exit_code}, {duration:.1f}s)\n"
            + output[-60 * 1024 :],
            state != "passed",
        )

    def _prepare(
        self, run_id: str, files: tuple[BatchFile, ...]
    ) -> tuple[ManagedWorktree, str]:
        """Capture the checkout and build the check's snapshot worktree from it.

        Returns the worktree and the digest of the source it holds. On failure
        nothing is left behind: a worktree it created is removed again.
        """

        runtime = runtime_identity(
            self.runtime_root, self.config.get("runtime_paths", [])
        )
        for _ in range(3):
            with self.lock:
                modes: dict[str, str] = {}
                screened: set[str] = set()
                source, bodies = source_snapshot(
                    self.root, contents=True, _modes=modes, _screened=screened
                )
            if source_snapshot(self.root)[0] == source:
                break
        else:
            raise ValueError(
                "Checkout kept changing during snapshot capture; retry the check."
            )
        for f in files:
            current, _ = read_identified_path(
                candidate_target(self.root, f.relative_path)
            )
            if current != f.base:
                raise ValueError(
                    f"Candidate base changed at {f.relative_path}; retry the task."
                )
        head = run_git(self.root, ["rev-parse", "HEAD"])
        assert isinstance(head.stdout, str)
        worktree = create_managed_worktree(
            self.root,
            managed_root=self.managed_root,
            task_id=run_id,
            base_oid=head.stdout.strip(),
        )
        try:
            snapshot = worktree.path
            # Remove tracked files absent from the captured checkout, then
            # overlay exact dirty/untracked bytes rather than Git-filter output.
            listed = run_git(snapshot, ["ls-files", "-z"])
            assert isinstance(listed.stdout, str)
            for path in filter(None, listed.stdout.split("\0")):
                target = candidate_target(snapshot, path)
                if target.is_symlink() or (path not in bodies and target.is_file()):
                    target.unlink()
            for path, body in bodies.items():
                materialize_snapshot_file(snapshot, path, body, modes[path])
            for f in sorted(
                files,
                key=lambda f: (0 if f.mode == DIRECTORY_MODE else 1, f.relative_path),
            ):
                materialize_snapshot_file(snapshot, f.relative_path, f.content, f.mode)
            for path in self.config.get("runtime_paths", []):
                self._copy_runtime(path, snapshot)
            register_runtime_paths(
                snapshot, tuple(self.config.get("runtime_paths", []))
            )
            if (
                runtime_identity(
                    self.runtime_root,
                    self.config.get("runtime_paths", []),
                    workspace_root=snapshot,
                )
                != runtime
                or runtime_identity(snapshot, self.config.get("runtime_paths", []))
                != runtime
            ):
                raise ValueError(
                    "Runtime dependencies changed during snapshot capture."
                )
        except BaseException:
            with suppress(Exception):
                remove_managed_worktree(
                    self.root,
                    managed_root=self.managed_root,
                    registered_path=worktree.path,
                    force=True,
                )
            raise
        self._snapshot_sensitive_paths = screened
        return worktree, digest(source + runtime)

    def _copy_runtime(self, path: str, snapshot: Path) -> None:
        if runtime_path(self.runtime_root, path):
            from llm_cli.workspace.isolated import copy_pinned_runtime_file
            from llm_cli.workspace.runtime import dependency_path

            source = candidate_target(self.runtime_root, path)
            if (
                source.is_dir()
                and not source.is_symlink()
                and not dependency_path(path)
            ):
                # Configured directory roots overlap the exact session-pinned
                # files added to this config. Those exact entries copy below;
                # recursing this root would widen consent to new private files.
                if any(
                    other.startswith(path + "/")
                    for other in self.config.get("runtime_paths", [])
                ):
                    return
                with os.scandir(source) as runtime_entries:
                    if next(runtime_entries, None) is not None:
                        raise ValueError(
                            "runtime directory has no exact pinned file inputs: " + path
                        )
                candidate_target(snapshot, path).mkdir(
                    parents=True, exist_ok=True, mode=0o700
                )
                return
            copy_pinned_runtime_file(
                self.runtime_root,
                snapshot,
                path,
                copy_observer=lambda relative, before, after: _remember_runtime_copy(
                    self.runtime_root, snapshot, relative, before, after
                ),
            )
            return
        source = candidate_target(self.runtime_root, path)
        target = candidate_target(snapshot, path)
        ignored = run_git(self.runtime_root, ["check-ignore", "--", path], check=False)
        if ignored.returncode != 0:
            raise ValueError(f"runtime_paths must name ignored paths: {path}")
        if not source.exists() or target.exists():
            raise ValueError(f"runtime path missing or collides with source: {path}")
        entries = [source, *source.rglob("*")] if source.is_dir() else [source]
        for entry in entries:
            if entry.is_symlink() or (not entry.is_file() and not entry.is_dir()):
                raise ValueError(
                    f"Runtime path contains links or special files: {path}. "
                    "Recreate it using setup instead."
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)

    def _command(
        self,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        deadline: float,
        run_id: str,
        remaining: int,
    ) -> tuple[int, str, bool, str | None]:
        return supervised_run(
            argv,
            cwd=cwd,
            env=env,
            deadline=deadline,
            remaining=remaining,
            cancelled=self.cancelled,
            sensitive_values=self.sensitive_values,
            on_output=lambda text: (
                None
                if self.suppress_output
                else self.emit("check.output", {"run_id": run_id, "text": text})
            ),
        )


def check_retained_proposal(
    workflow: TaskWorkflow,
    row: dict[str, Any],
    root: Path,
    files: tuple[BatchFile, ...],
    *,
    runtime_root: Path | None = None,
    suppress_output: bool = False,
    sensitive_paths: tuple[str, ...] = (),
) -> str | None:
    """Refresh required evidence for a manual apply's captured candidate.

    The caller releases its publication lock before invoking this helper and
    revalidates both the candidate bases and evidence under that lock afterward.
    """

    def emit(event: str, payload: dict[str, object]) -> None:
        workflow.store.record_task_event(
            claim_id=None, task_id=row["task_id"], event_type=event, payload=payload
        )

    runner = CheckRunner(
        workflow,
        row,
        root,
        workflow.store.path.parent / "check-worktrees",
        lambda: files,
        lambda: False,
        emit,
        workflow.publisher.lock,
        runtime_root=runtime_root,
        suppress_output=suppress_output,
        sensitive_paths=sensitive_paths,
    )
    outcome = runner.gate()
    return outcome.content if outcome else None


def supervised_run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    deadline: float,
    remaining: int,
    cancelled: Callable[[], bool],
    on_output: Callable[[str], None],
    reaper: bool = False,
    sensitive_values: tuple[str, ...] = (),
) -> tuple[int, str, bool, str | None]:
    """Run argv under the process-group supervisor, screening its output.

    Returns the exit code, the screened output, whether output was clipped or
    withheld, and "cancelled", "timed_out", or "error" when the run stopped
    early. Output after recognized secret material is withheld. ``reaper``
    passes the supervisor's descriptors to a sandbox that starts its own
    reaper (see ``sandbox.uses_reaper``).

    Output is read until the supervisor exits and briefly after, so a process
    that left the command's group cannot hold the run open by keeping its
    output pipe.
    """

    private_pattern = (
        re.compile(
            "|".join(
                re.escape(value)
                for value in sorted(set(sensitive_values), key=len, reverse=True)
                if value
            )
        )
        if any(sensitive_values)
        else None
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("check_process.py")),
            *(["--sandbox-reaper"] if reaper else []),
            *argv,
        ],
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdin and process.stdout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    chunks: list[str] = []
    rendered_remaining = remaining
    partial_line: list[str] = []
    withheld = False
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    clipped, stopped = False, None

    def emit_safe(text: str) -> None:
        nonlocal clipped, rendered_remaining
        encoded = text.encode("utf-8")
        allowed = encoded[: max(0, rendered_remaining)]
        clipped |= len(allowed) < len(encoded)
        text = allowed.decode("utf-8", errors="ignore")
        rendered_remaining -= len(text.encode("utf-8"))
        for start in range(0, len(text), 4096):
            chunk = text[start : start + 4096]
            chunks.append(chunk)
            on_output(chunk)

    def screen_line() -> None:
        nonlocal withheld
        line = "".join(partial_line)
        partial_line.clear()
        try:
            # Recognized signatures must be checked before substitutions: a
            # short private value must not hide a token prefix from screening.
            ensure_safe_content(line)
        except LlmCoordError:
            withheld = True
            emit_safe("[Remaining output withheld: recognized secret material.]\n")
        else:
            if private_pattern is not None:
                # Match only original output. Never revisit placeholders.
                line = private_pattern.sub("[private value]", line)
            emit_safe(line)

    def collect(text: str) -> None:
        # A key can span read() blocks. Emit only complete, screened lines;
        # after a private-key header its following body is private too.
        for part in text.splitlines(keepends=True):
            if withheld:
                return
            partial_line.append(part)
            if part.endswith("\n"):
                screen_line()

    drain_until: float | None = None
    abandon_at: float | None = None
    try:
        while selector.get_map():
            now = time.monotonic()
            if drain_until is None and process.poll() is not None:
                # The command has ended. A process that left its group may
                # still hold the output pipe, so only output already on its
                # way is read.
                drain_until = now + _DRAIN_SECONDS
            if drain_until is None:
                if stopped is None and (cancelled() or now >= deadline):
                    stopped = "cancelled" if cancelled() else "timed_out"
                    process.stdin.close()
                    abandon_at = now + _STOP_SECONDS
                elif abandon_at is not None and now >= abandon_at:
                    process.kill()
                    drain_until = now
            elif now >= drain_until:
                break
            for key, _ in selector.select(0.1):
                block = os.read(key.fd, 4096)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                allowed = block[: max(remaining, 0)]
                clipped |= len(allowed) < len(block)
                remaining -= len(allowed)
                text = decoder.decode(allowed)
                if text:
                    collect(text)
        collect(decoder.decode(b"", final=True))
        # A capped partial line may contain an incomplete secret signature.
        # Drop it instead of emitting a prefix that escaped classification.
        if partial_line and not clipped and not withheld:
            screen_line()
        return (
            process.wait(),
            "".join(chunks),
            clipped or withheld,
            stopped or ("error" if withheld else None),
        )
    finally:
        selector.close()
        process.stdin.close()
        process.wait(timeout=10)
        process.stdout.close()


def cleanup_interrupted_checks(store: ControlStore, managed_root: Path) -> None:
    """Retire only recorded old check snapshots, after supervisor shutdown grace."""
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT c.run_id,e.worktree_path FROM check_runs c "
            "JOIN task_workflows w ON w.task_id=c.task_id AND w.attempt=c.attempt "
            "JOIN task_executions e ON e.execution_id=w.execution_id "
            "WHERE c.state='uncertain'"
        ).fetchall()
    for row in rows:
        run_id = str(row["run_id"])
        if not run_id.startswith("check_") or not run_id.replace("_", "").isalnum():
            continue
        path = managed_root / run_id
        runtime = managed_root / (run_id + "-runtime")
        try:
            if path.exists():
                remove_managed_worktree(
                    Path(row["worktree_path"]),
                    managed_root=managed_root,
                    registered_path=path,
                    force=True,
                )
            if runtime.exists() and not runtime.is_symlink():
                shutil.rmtree(runtime)
        except (OSError, ValueError, LlmCoordError):
            # Preserve the registered artifact when identity or removal is uncertain.
            continue
