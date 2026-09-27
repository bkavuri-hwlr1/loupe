"""Creation and removal of isolated, application-managed Git worktrees."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git

_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


@dataclass(frozen=True, slots=True)
class ManagedWorktree:
    """Canonical identity recorded after a linked worktree is created."""

    repository: Path
    managed_root: Path
    path: Path
    git_dir: Path
    task_id: str
    base_oid: str


@dataclass(frozen=True, slots=True)
class GitWorktreeRecord:
    """Machine-readable subset of ``git worktree list --porcelain -z``."""

    path: Path
    head_oid: str | None
    locked: bool


def create_managed_worktree(
    repository: Path,
    *,
    managed_root: Path,
    task_id: str,
    base_oid: str,
    worktree_path: Path | None = None,
) -> ManagedWorktree:
    """Create and lock a detached linked worktree at one exact commit OID.

    ``managed_root`` is always explicit.  ``worktree_path`` may be supplied by
    persisted application state; otherwise the validated task ID is used as a
    single path component below the root.  The selected path may not be inside
    any existing worktree, which prevents accidental writes to the user's
    primary checkout.
    """

    _validate_task_id(task_id)
    canonical_repository = _repository_root(repository)
    resolved_base = resolve_exact_commit(canonical_repository, base_oid)

    existing_worktrees = list_git_worktrees(canonical_repository)
    root = managed_root.expanduser().absolute().resolve(strict=False)
    for record in existing_worktrees:
        if root == record.path or root.is_relative_to(record.path):
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                "managed root must not be inside an existing worktree",
            )
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve(strict=True)

    requested_path = worktree_path if worktree_path is not None else Path(task_id)
    candidate = (
        requested_path if requested_path.is_absolute() else root / requested_path
    )
    candidate = candidate.expanduser().absolute().resolve(strict=False)
    if candidate == root or not candidate.is_relative_to(root):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "managed worktree path must be a child of the managed root",
        )
    if candidate.exists() or candidate.is_symlink():
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "managed worktree path already exists",
        )

    for record in existing_worktrees:
        if candidate == record.path or candidate.is_relative_to(record.path):
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                "managed worktree path must not be inside an existing worktree",
            )

    candidate.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        run_git(
            canonical_repository,
            ["worktree", "add", "--detach", str(candidate), resolved_base],
        )
        created = True
        canonical_path = candidate.resolve(strict=True)
        if canonical_path != candidate or not canonical_path.is_relative_to(root):
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                "Git created the worktree at an unexpected canonical path",
            )

        run_git(
            canonical_repository,
            [
                "worktree",
                "lock",
                "--reason",
                f"llm-coord:{task_id}",
                str(canonical_path),
            ],
        )
        actual_head = _text(
            run_git(canonical_path, ["rev-parse", "--verify", "HEAD^{commit}"])
        )
        if actual_head != resolved_base:
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                "managed worktree HEAD does not match its recorded base",
            )
        git_dir = Path(
            _text(
                run_git(
                    canonical_path,
                    ["rev-parse", "--path-format=absolute", "--git-dir"],
                )
            )
        ).resolve(strict=True)
        _assert_linked_worktree(canonical_path)
        return ManagedWorktree(
            repository=canonical_repository,
            managed_root=root,
            path=canonical_path,
            git_dir=git_dir,
            task_id=task_id,
            base_oid=resolved_base,
        )
    except Exception:
        if created:
            run_git(
                canonical_repository,
                ["worktree", "unlock", str(candidate)],
                check=False,
            )
            run_git(
                canonical_repository,
                ["worktree", "remove", "--force", str(candidate)],
                check=False,
            )
        raise


def resume_managed_worktree(
    repository: Path,
    *,
    managed_root: Path,
    registered_path: Path,
    task_id: str,
    base_oid: str,
) -> ManagedWorktree:
    """Re-open one exact locked worktree after a resumable daemon restart.

    A durable execution checkpoint never authorizes an arbitrary directory.
    The recorded path must still be an exact child of our managed root, still
    be a locked linked worktree owned by this repository, and still point at
    the original base commit. Anything else is a recovery failure, not a
    reason to recreate or clean a directory that may be evidence.
    """

    _validate_task_id(task_id)
    canonical_repository = _repository_root(repository)
    resolved_base = resolve_exact_commit(canonical_repository, base_oid)
    root = managed_root.expanduser().absolute().resolve(strict=True)
    if not registered_path.is_absolute():
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "resumed managed worktree path must be absolute",
        )
    supplied = registered_path.absolute()
    canonical_path = supplied.resolve(strict=True)
    if (
        supplied != canonical_path
        or canonical_path == root
        or not canonical_path.is_relative_to(root)
    ):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "resumed managed worktree path is outside its managed root",
        )
    records = {
        record.path: record for record in list_git_worktrees(canonical_repository)
    }
    record = records.get(canonical_path)
    if record is None or not record.locked:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "resumed managed worktree is not this daemon's locked worktree",
        )
    if record.head_oid != resolved_base:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "resumed managed worktree no longer points at its recorded base",
        )
    _assert_linked_worktree(canonical_path)
    git_dir = Path(
        _text(
            run_git(
                canonical_path,
                ["rev-parse", "--path-format=absolute", "--git-dir"],
            )
        )
    ).resolve(strict=True)
    return ManagedWorktree(
        repository=canonical_repository,
        managed_root=root,
        path=canonical_path,
        git_dir=git_dir,
        task_id=task_id,
        base_oid=resolved_base,
    )


def remove_managed_worktree(
    repository: Path,
    *,
    managed_root: Path,
    registered_path: Path,
    force: bool = False,
) -> None:
    """Remove exactly one registered linked worktree without recursive deletion.

    The caller must provide the canonical path stored in authoritative state.
    Relative paths, aliases containing ``..``, symlink aliases, unregistered
    directories, and paths outside the explicit managed root are refused.
    """

    canonical_repository = _repository_root(repository)
    root = managed_root.expanduser().absolute().resolve(strict=True)
    if not registered_path.is_absolute():
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "registered managed worktree path must be absolute",
        )
    supplied = registered_path.absolute()
    canonical_path = supplied.resolve(strict=True)
    if supplied != canonical_path:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "managed worktree removal requires its exact canonical path",
        )
    if canonical_path == root or not canonical_path.is_relative_to(root):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "registered managed worktree is outside the managed root",
        )

    records = {
        record.path: record for record in list_git_worktrees(canonical_repository)
    }
    record = records.get(canonical_path)
    if record is None:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "managed worktree path is not registered with this repository",
        )
    _assert_linked_worktree(canonical_path)

    status = run_git(
        canonical_path,
        ["status", "--porcelain=v2", "-z", "--untracked-files=all"],
        text=False,
    )
    if not isinstance(status.stdout, bytes):
        raise LlmCoordError(ErrorCode.REPOSITORY_UNSAFE, "Git status was not binary")
    if status.stdout and not force:
        raise LlmCoordError(
            ErrorCode.WORKTREE_DIRTY,
            "managed worktree has uncommitted or untracked changes",
        )

    if record.locked:
        run_git(canonical_repository, ["worktree", "unlock", str(canonical_path)])
    arguments = ["worktree", "remove"]
    if force:
        arguments.append("--force")
    arguments.append(str(canonical_path))
    try:
        run_git(canonical_repository, arguments)
    except Exception:
        if record.locked:
            run_git(
                canonical_repository,
                [
                    "worktree",
                    "lock",
                    "--reason",
                    "llm-coord:remove-failed",
                    str(canonical_path),
                ],
                check=False,
            )
        raise

    if canonical_path.exists() or any(
        item.path == canonical_path for item in list_git_worktrees(canonical_repository)
    ):
        raise LlmCoordError(
            ErrorCode.INTERNAL_RECOVERABLE,
            "Git reported removal but managed worktree state remains",
        )


def prune_managed_worktrees(
    repository: Path, *, managed_root: Path
) -> tuple[Path, ...]:
    """Drop administrative records for managed worktrees whose directories vanished.

    Recovery needs this because ``git worktree add`` refuses a path Git still
    believes it owns, so a directory removed while the daemon was dead would
    block every later attempt of that task.  ``git worktree prune`` is global,
    which is why a missing entry outside the explicit managed root aborts the
    whole operation instead: the operator's own stale worktree is theirs to
    retire, and silently discarding its record would be a surprise.
    """

    canonical_repository = _repository_root(repository)
    root = managed_root.expanduser().absolute().resolve(strict=False)
    records = list_git_worktrees(canonical_repository)
    gone = tuple(record for record in records if not record.path.exists())
    if not gone:
        return ()
    missing = tuple(record.path for record in gone)
    foreign = tuple(
        path for path in missing if path == root or not path.is_relative_to(root)
    )
    if foreign:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "this repository has stale worktree records outside the managed "
            "root; run 'git worktree prune' yourself to confirm removing them",
            details={"unmanaged_missing_worktrees": sorted(str(p) for p in foreign)},
        )
    for record in gone:
        # A managed worktree is created locked, and Git refuses to prune a
        # locked record.  Unlocking is safe only because every record still
        # standing here has been shown to be ours and to have no directory.
        if record.locked:
            run_git(
                canonical_repository,
                ["worktree", "unlock", str(record.path)],
                check=False,
            )
    run_git(canonical_repository, ["worktree", "prune", "--expire=now"])
    remaining = {record.path for record in list_git_worktrees(canonical_repository)}
    return tuple(path for path in missing if path not in remaining)


def list_git_worktrees(repository: Path) -> tuple[GitWorktreeRecord, ...]:
    """Return canonical worktree records from Git's NUL-framed format."""

    result = run_git(repository, ["worktree", "list", "--porcelain", "-z"], text=False)
    if not isinstance(result.stdout, bytes):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "Git worktree listing was not binary"
        )
    data = result.stdout
    if not data.endswith(b"\0\0"):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "Git returned an ambiguously framed worktree listing",
        )

    records: list[GitWorktreeRecord] = []
    fields: list[bytes] = []
    for field in data.split(b"\0"):
        if field:
            fields.append(field)
            continue
        if not fields:
            continue
        first = fields[0]
        if not first.startswith(b"worktree "):
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                "Git worktree record did not begin with a path",
            )
        raw_path = first[len(b"worktree ") :]
        try:
            decoded_path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                "managed worktree path is not valid UTF-8",
            ) from exc
        path = Path(decoded_path).resolve(strict=False)
        head: str | None = None
        locked = False
        for record_field in fields[1:]:
            if record_field.startswith(b"HEAD "):
                try:
                    head = record_field[len(b"HEAD ") :].decode("ascii", "strict")
                except UnicodeDecodeError as exc:
                    raise LlmCoordError(
                        ErrorCode.REPOSITORY_UNSAFE,
                        "Git returned a non-ASCII worktree object ID",
                    ) from exc
            elif record_field == b"locked" or record_field.startswith(b"locked "):
                locked = True
        records.append(GitWorktreeRecord(path=path, head_oid=head, locked=locked))
        fields = []
    if fields or not records:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "Git returned an incomplete worktree listing"
        )
    if len({record.path for record in records}) != len(records):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "Git returned duplicate worktree paths"
        )
    return tuple(records)


def canonical_linked_worktree_path(path: Path) -> Path:
    """Return ``path`` canonically after proving it is not the primary worktree."""

    canonical = path.expanduser().absolute().resolve(strict=True)
    _assert_linked_worktree(canonical)
    return canonical


def resolve_exact_commit(repository: Path, oid: str) -> str:
    """Verify that ``oid`` is a full SHA-1/SHA-256 commit object ID."""

    if not isinstance(oid, str) or not _OBJECT_ID.fullmatch(oid):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "base/result identity must be a full SHA-1 or SHA-256 object ID",
        )
    resolved = _text(
        run_git(
            repository,
            ["rev-parse", "--verify", "--end-of-options", f"{oid}^{{commit}}"],
        )
    )
    if resolved.casefold() != oid.casefold():
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "object ID did not resolve exactly"
        )
    return resolved


def _repository_root(repository: Path) -> Path:
    requested = repository.expanduser().absolute().resolve(strict=True)
    root = Path(
        _text(
            run_git(
                requested,
                ["rev-parse", "--path-format=absolute", "--show-toplevel"],
            )
        )
    ).resolve(strict=True)
    return root


def _assert_linked_worktree(path: Path) -> None:
    git_dir = Path(
        _text(run_git(path, ["rev-parse", "--path-format=absolute", "--git-dir"]))
    ).resolve(strict=True)
    common_dir = Path(
        _text(
            run_git(
                path,
                ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            )
        )
    ).resolve(strict=True)
    if git_dir == common_dir:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "trusted task operation refused the primary worktree",
        )
    administrative_root = (common_dir / "worktrees").resolve(strict=False)
    if not git_dir.is_relative_to(administrative_root):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "linked worktree administrative path is outside the common Git directory",
        )


def _validate_task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "task ID is not safe as an application-owned path/ref component",
        )


def _text(result: object) -> str:
    stdout = getattr(result, "stdout", None)
    if not isinstance(stdout, str):
        raise LlmCoordError(ErrorCode.REPOSITORY_UNSAFE, "Git output was not text")
    value = stdout.strip()
    if not value or "\x00" in value or "\n" in value:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "Git returned ambiguous text output"
        )
    return value


__all__ = [
    "GitWorktreeRecord",
    "ManagedWorktree",
    "canonical_linked_worktree_path",
    "create_managed_worktree",
    "list_git_worktrees",
    "prune_managed_worktrees",
    "remove_managed_worktree",
    "resolve_exact_commit",
    "resume_managed_worktree",
]
