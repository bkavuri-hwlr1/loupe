"""Sanitized Git subprocess execution for trusted repository inspection."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
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
    "GIT_ATTR_NOSYSTEM": "1",
}
_INHERITED_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "XDG_CONFIG_HOME",
)


def sanitized_git_environment() -> dict[str, str]:
    env = {key: os.environ[key] for key in _INHERITED_ENV if key in os.environ}
    env.update(_CONTROLLED_ENV)
    return env


def run_git(
    cwd: Path,
    arguments: Sequence[str],
    *,
    check: bool = True,
    text: bool = True,
    input_data: str | bytes | None = None,
    remaining: Callable[[], float] | None = None,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    prefix = [
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
        "-c",
        "submodule.recurse=false",
        "-c",
        "protocol.allow=never",
        "-c",
        "commit.gpgSign=false",
        "-c",
        f"core.attributesFile={os.devnull}",
    ]
    env = sanitized_git_environment()
    operation_index = 0
    while operation_index < len(arguments) and arguments[operation_index] == "-c":
        operation_index += 2
    operation = arguments[operation_index] if operation_index < len(arguments) else ""
    safe_arguments = list(arguments)
    if operation in {"diff", "diff-tree", "diff-index", "diff-files", "show", "log"}:
        options = safe_arguments[operation_index + 1 :]
        if "--" in options:
            options = options[: options.index("--")]
        if any(option in {"--ext-diff", "--textconv"} for option in options):
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                "trusted Git operations cannot enable external diff helpers",
            )
        safe_arguments[operation_index + 1 : operation_index + 1] = [
            "--no-ext-diff",
            "--no-textconv",
        ]
    try:
        # Even status/diff can run clean filters. Refuse configured filters before
        # any command that could inspect/transform worktree contents. Do not
        # silently bypass required transformations such as Git LFS.
        if operation not in {
            "config",
            "init",
            "rev-parse",
            "show-ref",
            "check-ref-format",
            "check-ignore",
            "ls-files",
            "ls-tree",
            "write-tree",
            "update-ref",
            "commit-tree",
        }:
            filters = subprocess.run(
                [
                    *prefix,
                    *arguments[:operation_index],
                    "config",
                    "--null",
                    "--get-regexp",
                    r"^filter\..*\.(clean|smudge|process)$",
                ],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=min(30, remaining()) if remaining is not None else 30,
            )
            if filters.returncode not in {0, 1}:
                raise LlmCoordError(
                    ErrorCode.REPOSITORY_UNSAFE, "could not inspect repository filters"
                )
            if any(
                entry.partition(b"\n")[2].strip()
                for entry in filters.stdout.split(b"\x00")
            ):
                raise LlmCoordError(
                    ErrorCode.REPOSITORY_UNSAFE,
                    "repository-configured Git filters are not supported "
                    "by trusted operations",
                )
        result = subprocess.run(
            [*prefix, *safe_arguments],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL if input_data is None else None,
            input=input_data,
            capture_output=True,
            check=False,
            text=text,
            timeout=min(30, remaining()) if remaining is not None else 30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if remaining is not None:
            # A shared search owns a shorter deadline than general Git work.
            # Surface its timeout/cancellation after subprocess.run reaps Git.
            remaining()
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
