"""CAS-only publication of validated results to internal task references."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.git.validate import WorktreeValidation
from llm_cli.git.worktrees import canonical_linked_worktree_path, resolve_exact_commit

_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True, slots=True)
class TaskRefPublication:
    """Confirmed identity of an internal branch publication."""

    ref: str
    commit_oid: str
    expected_old_oid: str | None


def create_result_commit(
    worktree: Path,
    validation: WorktreeValidation,
    *,
    task_id: str,
) -> str:
    """Create a squashed result commit without moving HEAD or any reference."""

    _validate_task_id(task_id)
    canonical = canonical_linked_worktree_path(worktree)
    if canonical != validation.worktree_path:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "validation belongs to a different managed worktree",
        )
    base_oid = resolve_exact_commit(canonical, validation.base_oid)
    result = run_git(
        canonical,
        [
            "-c",
            "user.name=llm-coord",
            "-c",
            "user.email=llm-coord@localhost.invalid",
            "commit-tree",
            validation.result_tree,
            "-p",
            base_oid,
            "-m",
            f"llm-coord task {task_id}",
        ],
    )
    commit_oid = _one_line(result)
    return resolve_exact_commit(canonical, commit_oid)


def publish_task_ref(
    repository: Path,
    *,
    task_id: str,
    new_commit_oid: str,
    expected_old_oid: str | None = None,
) -> TaskRefPublication:
    """Compare-and-swap one ``refs/llm-coord/tasks/<task-id>`` reference.

    ``expected_old_oid=None`` means the reference must be absent.  Callers must
    persist their publication intent before invoking this side effect.
    ``run_git`` disables hooks, pagers, replacement objects, credential prompts,
    and global/system configuration for the update.
    """

    _validate_task_id(task_id)
    ref = f"refs/llm-coord/tasks/{task_id}"
    format_result = run_git(repository, ["check-ref-format", ref], check=False)
    if format_result.returncode != 0:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "task ID did not produce a valid internal Git reference",
        )

    new_oid = resolve_exact_commit(repository, new_commit_oid)
    expected = ""
    if expected_old_oid is not None:
        expected = resolve_exact_commit(repository, expected_old_oid)

    update = run_git(
        repository,
        [
            "update-ref",
            "--no-deref",
            "-m",
            f"llm-coord publish {task_id}",
            ref,
            new_oid,
            expected,
        ],
        check=False,
    )
    if update.returncode != 0:
        current = _read_ref(repository, ref)
        raise LlmCoordError(
            ErrorCode.TARGET_MOVED,
            "internal task reference no longer matches the publication intent",
            details={
                "ref": ref,
                "expected_old_oid": expected_old_oid,
                "actual_oid": current,
            },
        )

    confirmed = _read_ref(repository, ref)
    if confirmed != new_oid:
        raise LlmCoordError(
            ErrorCode.INTERNAL_RECOVERABLE,
            "Git accepted the task ref update but confirmation did not match",
        )
    return TaskRefPublication(
        ref=ref,
        commit_oid=confirmed,
        expected_old_oid=expected_old_oid,
    )


def read_task_ref(repository: Path, task_id: str) -> str | None:
    """Return the commit an internal task ref points at, or ``None`` if absent.

    Publication compare-and-swaps against this value, so a later attempt of the
    same task advances its ref instead of demanding the ref never existed.
    """

    _validate_task_id(task_id)
    return _read_ref(repository, f"refs/llm-coord/tasks/{task_id}")


def publish_validated_result(
    worktree: Path,
    validation: WorktreeValidation,
    *,
    task_id: str,
    expected_old_oid: str | None = None,
) -> TaskRefPublication:
    """Create a result commit and CAS-publish it to the internal task ref."""

    commit_oid = create_result_commit(worktree, validation, task_id=task_id)
    return publish_task_ref(
        worktree,
        task_id=task_id,
        new_commit_oid=commit_oid,
        expected_old_oid=expected_old_oid,
    )


def _read_ref(repository: Path, ref: str) -> str | None:
    result = run_git(
        repository,
        ["show-ref", "--verify", "--hash", ref],
        check=False,
    )
    if result.returncode != 0:
        # An absent ref is reported as exit 1 by older Git and as 128 ("not a
        # valid ref") by newer Git, so the exit code alone cannot be trusted to
        # separate "absent" from "broken".  Only a successful lookup prints a
        # hash, which makes empty output the unambiguous absence signal.
        stdout = result.stdout
        text = stdout if isinstance(stdout, str) else stdout.decode("utf-8", "replace")
        if not text.strip():
            return None
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "Git could not verify the internal task reference",
        )
    return resolve_exact_commit(repository, _one_line(result))


def _validate_task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "task ID is not safe as an internal ref component",
        )


def _one_line(result: object) -> str:
    stdout = getattr(result, "stdout", None)
    if not isinstance(stdout, str):
        raise LlmCoordError(ErrorCode.REPOSITORY_UNSAFE, "Git output was not text")
    value = stdout.strip()
    if not value or "\x00" in value or "\n" in value:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "Git returned ambiguous object identity output",
        )
    return value


__all__ = [
    "TaskRefPublication",
    "create_result_commit",
    "publish_task_ref",
    "publish_validated_result",
    "read_task_ref",
]
