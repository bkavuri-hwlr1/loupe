"""Canonical Git repository discovery without human-formatted output."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from llm_cli.coordination.identity import normalize_remote_identity, repository_key
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git


@dataclass(frozen=True, slots=True)
class RepositoryInfo:
    repo_key: str
    display_name: str
    worktree: Path
    main_worktree: Path
    common_git_dir: Path
    target_ref: str
    base_oid: str
    object_format: str
    normalized_remote: str | None
    integration_adapter: str
    path_case_insensitive: bool


def inspect_repository(
    path: Path,
    *,
    profile_id: str,
    target: str | None = None,
    coordinate_by_remote: bool = True,
) -> RepositoryInfo:
    requested = path.expanduser().absolute()
    if not requested.exists():
        raise LlmCoordError(
            ErrorCode.REPOSITORY_NOT_FOUND,
            f"repository path does not exist: {requested}",
        )
    top_level = _output(
        run_git(requested, ["rev-parse", "--path-format=absolute", "--show-toplevel"])
    )
    worktree = Path(top_level).resolve()
    common_git_dir = Path(
        _output(
            run_git(
                worktree,
                ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            )
        )
    ).resolve()
    object_format = _output(run_git(worktree, ["rev-parse", "--show-object-format"]))
    if object_format not in {"sha1", "sha256"}:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            f"unsupported Git object format: {object_format}",
        )

    target_name = target or _current_branch(worktree)
    symbolic = run_git(
        worktree,
        ["rev-parse", "--symbolic-full-name", "--verify", target_name],
    )
    target_ref = _output(symbolic)
    if not target_ref.startswith("refs/"):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "target must resolve to a named Git reference",
        )
    base_oid = _output(
        run_git(worktree, ["rev-parse", "--verify", f"{target_ref}^{{commit}}"])
    )

    path_case_insensitive = _path_case_insensitive(worktree)

    remote_result = run_git(
        worktree, ["config", "--get", "remote.origin.url"], check=False
    )
    raw_remote = _output(remote_result) if remote_result.returncode == 0 else None
    normalized_remote = normalize_remote_identity(raw_remote)
    main_worktree = _main_worktree(worktree)
    integration_adapter = "local_git"
    repo_key = repository_key(
        profile_id=profile_id,
        integration_adapter=integration_adapter,
        target_ref=target_ref,
        common_git_dir=common_git_dir,
        normalized_remote=normalized_remote,
        coordinate_by_remote=coordinate_by_remote,
    )
    return RepositoryInfo(
        repo_key=repo_key,
        display_name=worktree.name,
        worktree=worktree,
        main_worktree=main_worktree,
        common_git_dir=common_git_dir,
        target_ref=target_ref,
        base_oid=base_oid,
        object_format=object_format,
        normalized_remote=normalized_remote,
        integration_adapter=integration_adapter,
        path_case_insensitive=path_case_insensitive,
    )


def _path_case_insensitive(worktree: Path) -> bool:
    """Return Git's own determination of working-tree case sensitivity.

    ``core.ignorecase`` is probed and written by ``git init``/``git clone``, so
    it is the authority on whether two path spellings that differ only by case
    name the same file.  Scope contention must follow Git's answer rather than
    a Python string comparison.  An unset or unreadable value fails closed to
    case-insensitive, because treating aliases as distinct is the unsafe
    direction: it would grant two claims authority over one path.
    """

    result = run_git(
        worktree, ["config", "--type=bool", "--get", "core.ignorecase"], check=False
    )
    if result.returncode != 0:
        return True
    return _output(result) != "false"


def _current_branch(worktree: Path) -> str:
    result = run_git(
        worktree, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False
    )
    if result.returncode != 0:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "a target ref is required when the repository HEAD is detached",
        )
    return _output(result)


def _main_worktree(worktree: Path) -> Path:
    result = run_git(worktree, ["worktree", "list", "--porcelain", "-z"], text=False)
    assert isinstance(result.stdout, bytes)
    for field in result.stdout.split(b"\0"):
        if field.startswith(b"worktree "):
            raw = field[len(b"worktree ") :]
            return Path(raw.decode("utf-8", errors="strict")).resolve()
    raise LlmCoordError(
        ErrorCode.REPOSITORY_UNSAFE, "Git did not report a primary worktree"
    )


def _output(result: object) -> str:
    stdout = getattr(result, "stdout", None)
    if not isinstance(stdout, str):
        raise LlmCoordError(ErrorCode.REPOSITORY_UNSAFE, "Git output was not text")
    return stdout.strip()


__all__ = ["RepositoryInfo", "inspect_repository"]
