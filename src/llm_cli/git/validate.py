"""Trusted final-tree, changed-path, and canonical patch validation."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from llm_cli.coordination.scopes import ScopeValidationError, normalize_changed_path
from llm_cli.coordination.scopes import uncovered_paths as find_uncovered_paths
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.git.worktrees import (
    ManagedWorktree,
    canonical_linked_worktree_path,
    resolve_exact_commit,
)


@dataclass(frozen=True, slots=True)
class WorktreeValidation:
    """Canonical identity of a task's fully materialized result tree."""

    worktree_path: Path
    base_oid: str
    result_tree: str
    changed_paths: tuple[str, ...]
    patch_sha256: str
    patch: bytes
    uncovered_paths: tuple[str, ...]

    @property
    def patch_bytes(self) -> int:
        return len(self.patch)

    @property
    def scope_covered(self) -> bool:
        return not self.uncovered_paths


def collect_changed_paths(worktree: ManagedWorktree | Path) -> tuple[str, ...]:
    """Derive staged, unstaged, and untracked paths from porcelain-v2 status.

    Rename/copy records include both source and destination.  Unmerged records
    are refused because they cannot represent a publishable result tree.
    """

    path = _worktree_path(worktree)
    result = run_git(
        path,
        [
            "-c",
            "status.renames=copies",
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
        ],
        text=False,
    )
    if not isinstance(result.stdout, bytes):
        raise LlmCoordError(ErrorCode.REPOSITORY_UNSAFE, "Git status was not binary")
    return parse_porcelain_v2_z(result.stdout)


def validate_worktree(
    worktree: ManagedWorktree | Path,
    *,
    scopes: Iterable[str],
    base_oid: str | None = None,
    case_insensitive_filesystem: bool = False,
) -> WorktreeValidation:
    """Materialize and validate the exact final tree in an isolated worktree.

    All worktree content, including staged, unstaged, and non-ignored untracked
    files, is staged into the *linked task worktree's* private index.  The
    primary worktree and its index are never selected or modified.

    Scope violations raise ``SCOPE_VIOLATION`` and therefore cannot be passed
    accidentally to publication code.
    """

    path = _worktree_path(worktree)
    selected_base = base_oid
    if selected_base is None and isinstance(worktree, ManagedWorktree):
        selected_base = worktree.base_oid
    if selected_base is None:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "validation requires an exact recorded base object ID",
        )
    resolved_base = resolve_exact_commit(path, selected_base)

    # Parse status before mutating the isolated index.  Besides providing the
    # direct staged/unstaged/untracked contract, this fails closed on malformed
    # or unmerged porcelain records.
    collect_changed_paths(path)
    unmerged = run_git(path, ["ls-files", "--unmerged", "-z"], text=False)
    if not isinstance(unmerged.stdout, bytes):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "Git unmerged-path output was not binary"
        )
    if unmerged.stdout:
        raise LlmCoordError(
            ErrorCode.INTEGRATION_CONFLICT,
            "managed worktree contains unresolved index entries",
        )

    # A linked worktree owns a distinct index, so staging here cannot affect the
    # user's primary index.  This avoids reliance on process-global environment
    # mutation while still producing a complete tree for untracked content.
    run_git(path, ["add", "--all", "--", ":/"])
    result_tree = _one_line(run_git(path, ["write-tree"]))
    _verify_tree(path, result_tree)

    name_status = run_git(
        path,
        [
            "diff-tree",
            "--no-commit-id",
            "--name-status",
            "-z",
            "-r",
            "-M",
            "-C",
            "--find-copies-harder",
            f"{resolved_base}^{{tree}}",
            result_tree,
            "--",
        ],
        text=False,
    )
    if not isinstance(name_status.stdout, bytes):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "Git changed-path output was not binary"
        )
    changed_paths = parse_name_status_z(name_status.stdout)

    patch_result = run_git(
        path,
        [
            "-c",
            "core.quotePath=true",
            "-c",
            "diff.renames=false",
            "diff-tree",
            "--no-commit-id",
            "--binary",
            "--full-index",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "-p",
            "-r",
            f"{resolved_base}^{{tree}}",
            result_tree,
            "--",
        ],
        text=False,
    )
    if not isinstance(patch_result.stdout, bytes):
        raise LlmCoordError(ErrorCode.REPOSITORY_UNSAFE, "Git patch was not binary")
    patch = patch_result.stdout
    patch_sha256 = hashlib.sha256(patch).hexdigest()

    try:
        uncovered = find_uncovered_paths(
            scopes,
            changed_paths,
            case_insensitive_filesystem=case_insensitive_filesystem,
        )
    except ScopeValidationError as exc:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "stored claim scopes or Git paths failed canonical validation",
        ) from exc
    if uncovered:
        raise LlmCoordError(
            ErrorCode.SCOPE_VIOLATION,
            "validated result contains paths outside the active claim",
            details={"uncovered_paths": list(uncovered)},
        )

    return WorktreeValidation(
        worktree_path=path,
        base_oid=resolved_base,
        result_tree=result_tree,
        changed_paths=changed_paths,
        patch_sha256=patch_sha256,
        patch=patch,
        uncovered_paths=uncovered,
    )


def parse_porcelain_v2_z(data: bytes) -> tuple[str, ...]:
    """Strictly parse NUL-framed ``git status --porcelain=v2`` output."""

    if not data:
        return ()
    fields = _nul_fields(data, "Git status")
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if record.startswith(b"1 "):
            parts = record.split(b" ", 8)
            if len(parts) != 9 or len(parts[1]) != 2:
                _ambiguous("ordinary porcelain-v2 record")
            paths.add(_git_path(parts[8]))
        elif record.startswith(b"2 "):
            parts = record.split(b" ", 9)
            if len(parts) != 10 or len(parts[1]) != 2:
                _ambiguous("rename/copy porcelain-v2 record")
            score = parts[8]
            if not _valid_rename_copy_status(score):
                _ambiguous("rename/copy porcelain-v2 score")
            if index >= len(fields):
                _ambiguous("rename/copy porcelain-v2 source path")
            destination = _git_path(parts[9])
            source = _git_path(fields[index])
            index += 1
            paths.update((source, destination))
        elif record.startswith(b"u "):
            raise LlmCoordError(
                ErrorCode.INTEGRATION_CONFLICT,
                "managed worktree contains an unmerged Git status record",
            )
        elif record.startswith(b"? "):
            paths.add(_git_path(record[2:]))
        else:
            _ambiguous("porcelain-v2 record type")
    return tuple(sorted(paths))


def parse_name_status_z(data: bytes) -> tuple[str, ...]:
    """Strictly parse NUL-framed tree-to-tree ``--name-status`` output."""

    if not data:
        return ()
    fields = _nul_fields(data, "Git name-status")
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        try:
            status = fields[index].decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                "Git returned a non-ASCII name-status code",
            ) from exc
        index += 1
        if status in {"A", "D", "M", "T"}:
            if index >= len(fields):
                _ambiguous("name-status path")
            paths.add(_git_path(fields[index]))
            index += 1
            continue
        if _valid_rename_copy_status(status.encode("ascii")):
            if index + 1 >= len(fields):
                _ambiguous("rename/copy name-status paths")
            source = _git_path(fields[index])
            destination = _git_path(fields[index + 1])
            index += 2
            paths.update((source, destination))
            continue
        _ambiguous("name-status code")
    return tuple(sorted(paths))


def _worktree_path(worktree: ManagedWorktree | Path) -> Path:
    selected = worktree.path if isinstance(worktree, ManagedWorktree) else worktree
    canonical = canonical_linked_worktree_path(selected)
    if isinstance(worktree, ManagedWorktree) and canonical != worktree.path:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "managed worktree record no longer matches its canonical path",
        )
    return canonical


def _nul_fields(data: bytes, label: str) -> list[bytes]:
    if not isinstance(data, bytes) or not data.endswith(b"\0"):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            f"{label} output was not terminated unambiguously",
        )
    fields = data.split(b"\0")
    if fields[-1] != b"" or any(field == b"" for field in fields[:-1]):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            f"{label} output contained an ambiguous empty field",
        )
    return fields[:-1]


def _git_path(raw: bytes) -> str:
    try:
        decoded = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "Git path is not valid UTF-8 and cannot be scoped safely",
        ) from exc
    try:
        normalized = normalize_changed_path(decoded)
    except ScopeValidationError as exc:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "Git path cannot be represented safely as a claim path",
        ) from exc
    if normalized != decoded:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "Git path spelling would change during scope normalization",
        )
    return normalized


def _valid_rename_copy_status(status: bytes) -> bool:
    if len(status) < 2 or status[:1] not in {b"R", b"C"}:
        return False
    score = status[1:]
    if not score.isdigit():
        return False
    return 0 <= int(score) <= 100


def _verify_tree(path: Path, oid: str) -> None:
    invalid_hex = any(character not in "0123456789abcdef" for character in oid)
    if len(oid) not in {40, 64} or invalid_hex:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "Git returned an invalid result tree ID"
        )
    actual = _one_line(
        run_git(path, ["rev-parse", "--verify", "--end-of-options", f"{oid}^{{tree}}"])
    )
    if actual != oid:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "result tree identity did not verify exactly"
        )


def _one_line(result: object) -> str:
    stdout = getattr(result, "stdout", None)
    if not isinstance(stdout, str):
        raise LlmCoordError(ErrorCode.REPOSITORY_UNSAFE, "Git output was not text")
    value = stdout.strip()
    if not value or "\x00" in value or "\n" in value:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "Git returned ambiguous object identity output"
        )
    return value


def _ambiguous(record: str) -> None:
    raise LlmCoordError(
        ErrorCode.REPOSITORY_UNSAFE, f"Git returned an ambiguous {record}"
    )


__all__ = [
    "WorktreeValidation",
    "collect_changed_paths",
    "parse_name_status_z",
    "parse_porcelain_v2_z",
    "validate_worktree",
]
