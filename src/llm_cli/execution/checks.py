"""Configured verification commands against disposable source snapshots."""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import selectors
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from threading import RLock
from typing import Any

from llm_cli.agent.source_policy import ensure_safe_content
from llm_cli.agent.tools import TaskCancelled, ToolOutcome
from llm_cli.coordination.scopes import normalize_changed_path
from llm_cli.errors import LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.git.worktrees import create_managed_worktree, remove_managed_worktree
from llm_cli.ids import new_id
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.batches import BatchFile, candidate_target
from llm_cli.workspace.identity import DIRECTORY_MODE, ObjectKind, read_identified_path
from llm_cli.workspace.workflow import TaskWorkflow, encode_files

_OUTPUT_LIMIT = 10 * 1024 * 1024


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


def source_snapshot(
    root: Path, *, contents: bool = False
) -> tuple[str, dict[str, bytes | None]]:
    result = run_git(
        root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    )
    assert isinstance(result.stdout, str)
    paths = sorted(set(filter(None, result.stdout.split("\0"))))
    if len(paths) > 100_000:
        raise ValueError("source snapshot exceeds 100,000 paths")
    manifest: list[tuple[str, str, str, str]] = []
    bodies: dict[str, bytes | None] = {}
    total = 0
    for path in paths:
        target = candidate_target(root, path)
        identity, body = read_identified_path(target)
        if identity.kind not in {ObjectKind.REGULAR, ObjectKind.ABSENT}:
            raise ValueError(
                f"Snapshot cannot include symlinks or nested repositories: {path}"
            )
        total += identity.size
        if total > 1024**3:
            raise ValueError("source snapshot exceeds 1 GiB")
        manifest.append((path, str(identity.kind), identity.mode, identity.digest))
        if contents:
            bodies[path] = body
    return digest(json.dumps(manifest)), bodies


def runtime_identity(root: Path, paths: list[str]) -> str:
    """Bind copied inputs to evidence without exposing their contents."""
    manifest: list[tuple[str, str]] = []
    total = 0
    for path in paths:
        target = candidate_target(root, path)
        entries = [target, *target.rglob("*")] if target.is_dir() else [target]
        if len(entries) + len(manifest) > 200_000:
            raise ValueError("runtime snapshot exceeds 200,000 entries")
        for entry in sorted(entries):
            if entry.is_symlink():
                raise ValueError(
                    "runtime snapshot contains symlinks; use setup instead"
                )
            if entry.is_dir():
                continue
            identity, _ = read_identified_path(entry)
            if identity.kind is not ObjectKind.REGULAR:
                raise ValueError("runtime snapshot contains an absent or special file")
            total += identity.size
            if total > 5 * 1024**3:
                raise ValueError("runtime snapshot exceeds 5 GiB")
            manifest.append((entry.relative_to(root).as_posix(), identity.digest))
    return digest(json.dumps(manifest))


def verification_identity(root: Path, config: dict[str, Any]) -> str:
    return digest(
        source_snapshot(root)[0]
        + runtime_identity(root, config.get("runtime_paths", []))
    )


def verification_current(
    store: ControlStore,
    workflow: dict[str, Any],
    root: Path,
    files: tuple[BatchFile, ...],
) -> bool:
    config = json.loads(workflow["config_json"])
    required = [
        name for name, check in config.get("checks", {}).items() if check["required"]
    ]
    if not required:
        return True
    source = verification_identity(root, config)
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


def verification_status(
    store: ControlStore,
    workflow: dict[str, Any],
    root: Path,
    files: tuple[BatchFile, ...],
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
    source = verification_identity(root, config)
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
    ) -> None:
        self.workflow, self.row, self.root = workflow, row, root
        self.store = workflow.store
        self.managed_root, self.candidates, self.cancelled = (
            managed_root,
            candidates,
            cancelled,
        )
        self.emit, self.lock = emit, lock
        self.config = json.loads(row["config_json"])
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

    def _passed(self, name: str) -> bool:
        files = self.candidates()
        source = verification_identity(self.root, self.config)
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
            runtime = runtime_identity(self.root, self.config.get("runtime_paths", []))
            for _ in range(3):
                with self.lock:
                    source, bodies = source_snapshot(self.root, contents=True)
                    modes = {
                        p: read_identified_path(candidate_target(self.root, p))[0].mode
                        for p in bodies
                    }
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
                target = candidate_target(snapshot, path)
                if body is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(body)
                    target.chmod(0o755 if modes[path] == "100755" else 0o644)
            for f in sorted(
                files,
                key=lambda f: (0 if f.mode == DIRECTORY_MODE else 1, f.relative_path),
            ):
                target = candidate_target(snapshot, f.relative_path)
                if f.mode == DIRECTORY_MODE:
                    target.mkdir(parents=True, exist_ok=True)
                elif f.content is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(f.content)
                    target.chmod(0o755 if f.mode == "100755" else 0o644)
            for path in self.config.get("runtime_paths", []):
                self._copy_runtime(path, snapshot)
            if (
                runtime_identity(self.root, self.config.get("runtime_paths", []))
                != runtime
                or runtime_identity(snapshot, self.config.get("runtime_paths", []))
                != runtime
            ):
                raise ValueError(
                    "Runtime dependencies changed during snapshot capture."
                )
            source = digest(source + runtime)
            private = snapshot.parent / (run_id + "-runtime")
            private.mkdir(mode=0o700)
            env = {
                "PATH": os.defpath,
                "HOME": str(private),
                "TMPDIR": str(private),
                "XDG_CACHE_HOME": str(private / "cache"),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "LC_ALL": "C.UTF-8",
            }
            env.update(spec["env"])
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
        except (OSError, ValueError, RuntimeError) as exc:
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
        return ToolOutcome(
            f"{name}: {state} (exit {exit_code}, {duration:.1f}s)\n"
            + output[-60 * 1024 :],
            state != "passed",
        )

    def _copy_runtime(self, path: str, snapshot: Path) -> None:
        source = candidate_target(self.root, path)
        target = candidate_target(snapshot, path)
        ignored = run_git(self.root, ["check-ignore", "--", path], check=False)
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
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("check_process.py")), *argv],
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
        partial_line: list[str] = []
        withheld = False
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        clipped, stopped = False, None

        def emit_safe(text: str) -> None:
            for start in range(0, len(text), 4096):
                chunk = text[start : start + 4096]
                chunks.append(chunk)
                self.emit("check.output", {"run_id": run_id, "text": chunk})

        def screen_line() -> None:
            nonlocal withheld
            line = "".join(partial_line)
            partial_line.clear()
            try:
                ensure_safe_content(line)
            except LlmCoordError:
                withheld = True
                emit_safe(
                    "[Remaining check output withheld: recognized secret material.]\n"
                )
            else:
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

        try:
            while selector.get_map():
                if stopped is None and (
                    self.cancelled() or time.monotonic() >= deadline
                ):
                    stopped = "cancelled" if self.cancelled() else "timed_out"
                    process.stdin.close()
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
