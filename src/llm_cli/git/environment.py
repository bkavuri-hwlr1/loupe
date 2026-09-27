"""Sanitized Git subprocess execution for trusted repository inspection."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from llm_cli.errors import ErrorCode, LlmCoordError

_CONTROLLED_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "LC_ALL": "C",
}


def sanitized_git_environment() -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(_CONTROLLED_ENV)
    return env


def run_git(
    cwd: Path,
    arguments: Sequence[str],
    *,
    check: bool = True,
    text: bool = True,
    input_data: str | bytes | None = None,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    command = [
        "git",
        "--no-pager",
        "--no-replace-objects",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "credential.interactive=never",
        "-c",
        "color.ui=false",
        "-c",
        "core.fsmonitor=false",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=sanitized_git_environment(),
            stdin=subprocess.DEVNULL if input_data is None else None,
            input=input_data,
            capture_output=True,
            check=False,
            text=text,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "trusted Git inspection could not run"
        ) from exc
    if check and result.returncode != 0:
        stderr = (
            result.stderr
            if isinstance(result.stderr, str)
            else result.stderr.decode("utf-8", errors="replace")
        )
        sanitized = stderr.strip().splitlines()[-1:] or ["Git command failed"]
        raise LlmCoordError(ErrorCode.REPOSITORY_UNSAFE, sanitized[0][:500])
    return result


__all__ = ["run_git", "sanitized_git_environment"]
