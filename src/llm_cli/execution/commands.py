"""Model-chosen commands and user hooks in sandboxed copies of the checkout.

A command runs against a private copy of the checkout with the task's pending
edits applied, inside an operating-system sandbox (see ``sandbox``). Changes
the command makes are discarded with its copy; edits still go through the
file tools. Commands diagnose; configured checks remain the verification
required for publication. Hooks run the same way, but can hand back the
contents of named files, which the task may then stage as edits.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

from llm_cli.agent.source_policy import ensure_safe_content
from llm_cli.agent.tools import TaskCancelled, ToolOutcome
from llm_cli.coordination.scopes import normalize_changed_path
from llm_cli.errors import LlmCoordError
from llm_cli.execution.checks import source_snapshot, supervised_run
from llm_cli.execution.sandbox import SandboxPolicy, uses_reaper, wrap
from llm_cli.git.environment import run_git
from llm_cli.ids import new_id
from llm_cli.workspace.batches import BatchFile, candidate_target
from llm_cli.workspace.identity import DIRECTORY_MODE, read_identified_path

# Ignored dependency folders a command may read in place. Copying them would
# be slow, and virtual environments embed absolute paths.
DEPENDENCY_PATHS = (".venv", "venv", "node_modules")
_CAPTURE_BYTES = 1024 * 1024
_RESULT_HEAD_BYTES = 4 * 1024
_RESULT_TAIL_BYTES = 28 * 1024
# Files a hook hands back are source files; larger ones are not read back.
_MAX_READ_BACK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class HookRun:
    """How a hook ended, and the files it was asked to hand back."""

    state: str
    exit_code: int | None
    output: str
    # Path to content after the run; None when the file no longer exists.
    files: dict[str, bytes | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CommandSettings:
    """Daemon-wide command policy; tasks add their checkout and edits."""

    snapshot_root: Path
    protected: tuple[Path, ...] = ()
    # "ask", "allow", or "off"; see Settings.agent_commands.
    approval: str = "ask"


class CommandRunner:
    """Run one task's commands against snapshots of its checkout and edits."""

    def __init__(
        self,
        *,
        root: Path,
        snapshot_root: Path,
        candidates: Callable[[], tuple[BatchFile, ...]],
        cancelled: Callable[[], bool],
        emit: Callable[[str, dict[str, object]], None],
        lock: RLock,
        sandbox: str,
        protected: Sequence[Path] = (),
        dependency_paths: Sequence[str] = DEPENDENCY_PATHS,
        deadline_at: float | None = None,
        search_path: str | None = None,
    ) -> None:
        self.root = root.resolve()
        self.snapshot_root = snapshot_root
        self.candidates = candidates
        self.cancelled = cancelled
        self.emit = emit
        self.lock = lock
        self.sandbox = sandbox
        self.protected = tuple(path.resolve() for path in protected)
        self.dependency_paths = tuple(dependency_paths)
        self.deadline_at = deadline_at or (time.time() + 7200)
        self.search_path = (
            search_path if search_path is not None else os.environ.get("PATH", "")
        )

    def run(self, argv: list[str], cwd: str, timeout: int) -> ToolOutcome:
        if self.cancelled():
            raise TaskCancelled("task stopped")
        try:
            ensure_safe_content("\0".join(argv))
        except LlmCoordError:
            return ToolOutcome(
                "command arguments contain recognized secret material", True
            )
        relative_cwd = "." if cwd in {"", "."} else normalize_changed_path(cwd)
        state, exit_code, output, clipped, duration, _ = self._execute(
            argv, relative_cwd, timeout, quiet=False
        )
        return ToolOutcome(
            _result(state, exit_code, duration, output, clipped),
            state not in {"completed"},
        )

    def run_hook(
        self,
        argv: list[str],
        timeout: int,
        *,
        hook_input: object = None,
        read_back: Sequence[str] = (),
    ) -> HookRun:
        """Run a user hook in a snapshot without showing its output.

        ``hook_input`` is written as JSON to a file named by the
        ``LOUPE_HOOK_INPUT`` variable. The contents of ``read_back`` paths are
        returned as they were when the hook finished.
        """

        if self.cancelled():
            raise TaskCancelled("task stopped")
        state, exit_code, output, clipped, _, files = self._execute(
            argv, ".", timeout, quiet=True, hook_input=hook_input, read_back=read_back
        )
        if clipped:
            output += "\n[output was capped or withheld]"
        return HookRun(state, exit_code, output, files)

    def _execute(
        self,
        argv: list[str],
        relative_cwd: str,
        timeout: int,
        *,
        quiet: bool,
        hook_input: object = None,
        read_back: Sequence[str] = (),
    ) -> tuple[str, int | None, str, bool, float, dict[str, bytes | None]]:
        emit = (lambda kind, payload: None) if quiet else self.emit
        run_id = new_id("command")
        run_directory = self.snapshot_root / run_id
        started = time.monotonic()
        deadline = started + min(timeout, max(0, self.deadline_at - time.time()))
        output = ""
        clipped = False
        state, exit_code = "error", None
        files: dict[str, bytes | None] = {}
        emit(
            "command.started",
            {
                "run_id": run_id,
                "argv": list(argv),
                "cwd": relative_cwd,
                "timeout": timeout,
                "sandbox": self.sandbox,
            },
        )
        try:
            source = run_directory / "source"
            home = run_directory / "home"
            source.mkdir(parents=True)
            home.mkdir(mode=0o700)
            (home / "tmp").mkdir()
            self._capture(source)
            readable = self._link_dependencies(source)
            source = source.resolve()
            home = home.resolve()
            directory = (source / relative_cwd).resolve()
            if not directory.is_relative_to(source) or not directory.is_dir():
                raise ValueError(
                    f"{relative_cwd} is not a directory in the checkout snapshot"
                )
            env = self._environment(home)
            if hook_input is not None:
                input_file = home / "hook-input.json"
                input_file.write_text(json.dumps(hook_input), encoding="utf-8")
                env["LOUPE_HOOK_INPUT"] = str(input_file)
            policy = SandboxPolicy(
                writable=(source, home),
                protected=(self.root, *self.protected),
                readable=readable,
            )
            exit_code, output, clipped, stopped = supervised_run(
                wrap(self.sandbox, policy, argv, cwd=directory),
                cwd=directory,
                env=env,
                deadline=deadline,
                remaining=_CAPTURE_BYTES,
                cancelled=self.cancelled,
                on_output=lambda text: emit(
                    "command.output", {"run_id": run_id, "text": text}
                ),
                reaper=uses_reaper(self.sandbox),
            )
            # "error" from the supervisor means output was withheld for secret
            # material; the command itself still ran to completion.
            state = "completed" if stopped is None or stopped == "error" else stopped
            if state == "completed":
                files = {path: _read_back(source, path) for path in read_back}
        except (OSError, ValueError, RuntimeError) as exc:
            state = "cancelled" if self.cancelled() else "error"
            output += f"\n{exc}"
        finally:
            with suppress(OSError):
                shutil.rmtree(run_directory)
            duration = time.monotonic() - started
            emit(
                "command.finished",
                {
                    "run_id": run_id,
                    "state": state,
                    "exit_code": exit_code,
                    "duration": duration,
                    "truncated": clipped,
                },
            )
        if self.cancelled():
            raise TaskCancelled("task stopped")
        return state, exit_code, output, clipped, duration, files

    def _capture(self, target: Path) -> None:
        """Copy the checkout's source and apply pending edits on top."""

        with self.lock:
            files = self.candidates()
            _, bodies = source_snapshot(self.root, contents=True)
            modes = {
                path: read_identified_path(candidate_target(self.root, path))[0].mode
                for path, body in bodies.items()
                if body is not None
            }
        for path, body in bodies.items():
            if body is None:
                continue
            destination = candidate_target(target, path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            destination.chmod(0o755 if modes.get(path) == "100755" else 0o644)
        for file in sorted(
            files,
            key=lambda item: (
                0 if item.mode == DIRECTORY_MODE else 1,
                item.relative_path,
            ),
        ):
            destination = candidate_target(target, file.relative_path)
            if file.mode == DIRECTORY_MODE:
                destination.mkdir(parents=True, exist_ok=True)
            elif file.content is None:
                destination.unlink(missing_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(file.content)
                destination.chmod(0o755 if file.mode == "100755" else 0o644)

    def _link_dependencies(self, source: Path) -> tuple[Path, ...]:
        """Expose ignored dependency folders read-only at their usual paths."""

        readable: list[Path] = []
        for relative in self.dependency_paths:
            original = self.root / relative
            link = source / relative
            if (
                original.is_symlink()
                or not original.is_dir()
                or link.exists()
                or link.is_symlink()
            ):
                continue
            ignored = run_git(
                self.root, ["check-ignore", "-q", "--", relative], check=False
            )
            if ignored.returncode != 0:
                continue
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(original, target_is_directory=True)
            readable.append(original.resolve())
        return tuple(readable)

    def _environment(self, home: Path) -> dict[str, str]:
        # Only the search path is inherited. Credentials in the daemon's
        # environment never reach a model-chosen command.
        return {
            "PATH": self.search_path or os.defpath,
            "HOME": str(home),
            "TMPDIR": str(home / "tmp"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "PAGER": "cat",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }


def cleanup_command_snapshots(snapshot_root: Path) -> None:
    """Remove snapshots left by an earlier daemon; none can still be running."""

    if not snapshot_root.is_dir() or snapshot_root.is_symlink():
        return
    for entry in snapshot_root.iterdir():
        if entry.name.startswith("command_") and not entry.is_symlink():
            with suppress(OSError):
                shutil.rmtree(entry)


def _read_back(source: Path, relative: str) -> bytes | None:
    """A snapshot file's contents, or None if the hook removed it.

    Symlinks, directories, and oversized files raise ValueError, so the hook's
    result for that path is not used.
    """

    target = candidate_target(source, relative)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError(f"{relative} is no longer a regular file")
    if not target.exists():
        return None
    if target.stat().st_size > _MAX_READ_BACK_BYTES:
        raise ValueError(f"{relative} grew too large to read back")
    return target.read_bytes()


def _result(
    state: str, exit_code: int | None, duration: float, output: str, clipped: bool
) -> str:
    status = {
        "completed": f"exit {exit_code}",
        "timed_out": "timed out",
        "cancelled": "cancelled",
    }.get(state, "could not run")
    header = (
        f"{status} after {duration:.1f}s "
        "(sandboxed copy with your pending edits; its changes were discarded)"
    )
    encoded = output.encode("utf-8")
    if len(encoded) > _RESULT_HEAD_BYTES + _RESULT_TAIL_BYTES:
        omitted = len(encoded) - _RESULT_HEAD_BYTES - _RESULT_TAIL_BYTES
        output = (
            encoded[:_RESULT_HEAD_BYTES].decode("utf-8", errors="ignore")
            + f"\n[... {omitted} bytes of output omitted ...]\n"
            + encoded[-_RESULT_TAIL_BYTES:].decode("utf-8", errors="ignore")
        )
    if clipped:
        output += "\n[output was capped or withheld]"
    return header + ("\n" + output if output.strip() else "\n(no output)")


__all__ = [
    "DEPENDENCY_PATHS",
    "CommandRunner",
    "CommandSettings",
    "HookRun",
    "cleanup_command_snapshots",
]
