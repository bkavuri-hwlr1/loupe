"""User hooks around the agent's tool calls.

Hooks are commands from the user's configuration. They run in a sandboxed
snapshot of the checkout with the task's pending edits, like the agent's
commands, so they cannot change the checkout directly. A pre_tool hook can
block a call by exiting with status 2; its output becomes the reason the model
sees. A post_edit hook can rewrite the files an edit touched, and those changes
are staged like the agent's own, for review before they apply. Other hook
failures are reported and do not block.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Protocol

from llm_cli.config.models import HookConfig

PATHS = "{paths}"
# The tool argument naming the file each editing tool leaves changed.
EDITED_PATH = {
    "write_file": "path",
    "apply_patch": "path",
    "rename_file": "destination",
}
_BLOCK_EXIT = 2
_SHOWN_OUTPUT_CHARACTERS = 2_000


class _Run(Protocol):
    @property
    def state(self) -> str: ...
    @property
    def exit_code(self) -> int | None: ...
    @property
    def output(self) -> str: ...
    @property
    def files(self) -> dict[str, bytes | None]: ...


RunHook = Callable[..., _Run]


def hook_label(hook: HookConfig) -> str:
    """A short name for a hook: its configured name, or its program and first
    argument."""

    if hook.name:
        return hook.name
    words = [Path(hook.command[0]).name, *hook.command[1:2]]
    label = " ".join(word for word in words if word != PATHS)
    return label if len(label) <= 40 else label[:39] + "…"


class HookRunner:
    """Run one task's configured hooks through ``run_hook``."""

    def __init__(
        self,
        hooks: Sequence[HookConfig],
        run_hook: RunHook,
        emit: Callable[[str, dict[str, object]], None],
    ) -> None:
        self._pre = tuple(hook for hook in hooks if hook.kind == "pre_tool")
        self._post = tuple(hook for hook in hooks if hook.kind == "post_edit")
        self._run_hook = run_hook
        self._emit = emit

    def before_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> tuple[str, str] | None:
        """Return (hook, reason) when a pre_tool hook blocks this call."""

        for hook in self._pre:
            if not any(fnmatchcase(name, pattern) for pattern in hook.match):
                continue
            run = self._run_hook(
                list(hook.command),
                hook.timeout_seconds,
                hook_input={"tool": name, "arguments": dict(arguments)},
            )
            if run.state == "completed" and run.exit_code == 0:
                continue
            label = hook_label(hook)
            if run.state == "completed" and run.exit_code == _BLOCK_EXIT:
                self._emit("hook.blocked", {"hook": label, "tool": name})
                return label, _tail(run.output) or "no reason given"
            self._emit(
                "hook.failed", {"hook": label, "phase": "pre_tool", "reason": _why(run)}
            )
        return None

    def after_edit(
        self,
        paths: Sequence[str],
        *,
        current: Callable[[str], bytes | None],
        stage: Callable[[str, bytes], str | None],
    ) -> list[str]:
        """Run post_edit hooks for edited paths and stage what they change.

        ``current`` gives a path's pending content and ``stage`` stages new
        content, returning an error message if it was refused. Returns notes
        for the model about what each hook did.
        """

        notes: list[str] = []
        for hook in self._post:
            matched = [
                path
                for path in paths
                if any(fnmatchcase(path, pattern) for pattern in hook.match)
            ]
            if not matched:
                continue
            label = hook_label(hook)
            argv = [
                part
                for item in hook.command
                for part in (matched if item == PATHS else [item])
            ]
            run = self._run_hook(
                argv,
                hook.timeout_seconds,
                hook_input={"paths": matched},
                read_back=matched,
            )
            if run.state != "completed" or run.exit_code != 0:
                reason = _why(run)
                self._emit(
                    "hook.failed",
                    {"hook": label, "phase": "post_edit", "reason": reason},
                )
                output = _tail(run.output)
                notes.append(
                    f"[post_edit hook {label!r} failed ({reason}); the edit is "
                    "unchanged" + (f". Its output:\n{output}]" if output else "]")
                )
                continue
            changed: list[str] = []
            for path in matched:
                content = run.files.get(path)
                if content is None:
                    notes.append(f"[post_edit hook {label!r} removed {path}; ignored]")
                elif content != current(path):
                    refusal = stage(path, content)
                    if refusal is None:
                        changed.append(path)
                    else:
                        notes.append(
                            f"[post_edit hook {label!r} changed {path}, but the "
                            f"change was not staged: {refusal}]"
                        )
            if changed:
                self._emit("hook.changed", {"hook": label, "paths": changed})
                notes.append(
                    f"[post_edit hook {label!r} changed {', '.join(changed)}; read "
                    "it again before patching it]"
                )
        return notes


def _why(run: _Run) -> str:
    if run.state == "completed":
        return f"exit {run.exit_code}"
    return {"timed_out": "timed out", "cancelled": "cancelled"}.get(
        run.state, "could not run"
    )


def _tail(output: str) -> str:
    text = output.strip()
    if len(text) <= _SHOWN_OUTPUT_CHARACTERS:
        return text
    return "…" + text[-(_SHOWN_OUTPUT_CHARACTERS - 1) :]


__all__ = ["EDITED_PATH", "PATHS", "HookRunner", "hook_label"]
