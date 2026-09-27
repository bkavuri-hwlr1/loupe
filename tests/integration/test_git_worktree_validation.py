from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.integrate import (
    create_result_commit,
    publish_task_ref,
    publish_validated_result,
)
from llm_cli.git.validate import (
    collect_changed_paths,
    parse_name_status_z,
    parse_porcelain_v2_z,
    validate_worktree,
)
from llm_cli.git.worktrees import (
    create_managed_worktree,
    list_git_worktrees,
    remove_managed_worktree,
)


def _git(path: Path, *arguments: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=not binary,
        env={
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    if binary:
        assert isinstance(result.stdout, bytes)
        return result.stdout
    assert isinstance(result.stdout, str)
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "tracked.txt").write_text("tracked base\n", encoding="utf-8")
    (repository / "rename-me.txt").write_text("rename base\n", encoding="utf-8")
    (repository / "copy-source.txt").write_text(
        "unique copy source\n", encoding="utf-8"
    )
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "fixture base")
    return repository, str(_git(repository, "rev-parse", "HEAD"))


def test_create_lock_and_remove_exact_managed_worktree(tmp_path: Path) -> None:
    repository, base_oid = _repository(tmp_path)
    primary_status = _git(repository, "status", "--porcelain=v2", "-z", binary=True)
    primary_head = _git(repository, "rev-parse", "HEAD")
    primary_content = (repository / "tracked.txt").read_bytes()
    managed_root = tmp_path / "managed"

    with pytest.raises(LlmCoordError) as nested_root_error:
        create_managed_worktree(
            repository,
            managed_root=repository / "unsafe-managed-root",
            task_id="task-unsafe-root",
            base_oid=base_oid,
        )
    assert nested_root_error.value.code is ErrorCode.REPOSITORY_UNSAFE
    assert not (repository / "unsafe-managed-root").exists()
    with pytest.raises(LlmCoordError) as primary_error:
        collect_changed_paths(repository)
    assert primary_error.value.code is ErrorCode.REPOSITORY_UNSAFE

    managed = create_managed_worktree(
        repository,
        managed_root=managed_root,
        task_id="task-001",
        base_oid=base_oid,
    )

    assert managed.path == (managed_root / "task-001").resolve()
    assert managed.path.is_dir()
    assert _git(managed.path, "rev-parse", "HEAD") == base_oid
    assert _git(managed.path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    record = next(
        item for item in list_git_worktrees(repository) if item.path == managed.path
    )
    assert record.locked
    assert _git(repository, "rev-parse", "HEAD") == primary_head
    assert (
        _git(repository, "status", "--porcelain=v2", "-z", binary=True)
        == primary_status
    )
    assert (repository / "tracked.txt").read_bytes() == primary_content

    alias = managed.path / ".." / managed.path.name
    with pytest.raises(LlmCoordError) as alias_error:
        remove_managed_worktree(
            repository,
            managed_root=managed_root,
            registered_path=alias,
        )
    assert alias_error.value.code is ErrorCode.REPOSITORY_UNSAFE

    remove_managed_worktree(
        repository,
        managed_root=managed_root,
        registered_path=managed.path,
    )
    assert not managed.path.exists()
    assert all(item.path != managed.path for item in list_git_worktrees(repository))
    assert (
        _git(repository, "status", "--porcelain=v2", "-z", binary=True)
        == primary_status
    )


def test_validation_covers_status_tree_binary_patch_and_scope(tmp_path: Path) -> None:
    repository, base_oid = _repository(tmp_path)
    managed_root = tmp_path / "managed"
    managed = create_managed_worktree(
        repository,
        managed_root=managed_root,
        task_id="task-validate",
        base_oid=base_oid,
    )

    (managed.path / "tracked.txt").write_text(
        "tracked base\nunstaged edit\n", encoding="utf-8"
    )
    _git(managed.path, "mv", "rename-me.txt", "renamed.txt")
    (managed.path / "copy.txt").write_text(
        (managed.path / "copy-source.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _git(managed.path, "add", "copy.txt")
    (managed.path / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    (managed.path / "binary.dat").write_bytes(b"\x00\x01\xffbinary\x00")

    status_paths = set(collect_changed_paths(managed))
    assert {
        "binary.dat",
        "copy.txt",
        "rename-me.txt",
        "renamed.txt",
        "tracked.txt",
        "untracked.txt",
    } <= status_paths

    with pytest.raises(LlmCoordError) as scope_error:
        validate_worktree(managed, scopes=["tracked.txt"])
    assert scope_error.value.code is ErrorCode.SCOPE_VIOLATION
    assert "binary.dat" in (scope_error.value.details or {}).get("uncovered_paths", [])

    validation = validate_worktree(managed, scopes=["*"])
    assert set(validation.changed_paths) == {
        "binary.dat",
        "copy-source.txt",
        "copy.txt",
        "rename-me.txt",
        "renamed.txt",
        "tracked.txt",
        "untracked.txt",
    }
    assert validation.scope_covered
    assert validation.patch_sha256 == hashlib.sha256(validation.patch).hexdigest()
    assert validation.patch_bytes == len(validation.patch)
    assert b"GIT binary patch" in validation.patch
    assert _git(managed.path, "show", f"{validation.result_tree}:untracked.txt") == (
        "untracked"
    )

    assert _git(repository, "status", "--porcelain=v2", "-z", binary=True) == b""
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "tracked base\n"

    with pytest.raises(LlmCoordError) as dirty_error:
        remove_managed_worktree(
            repository,
            managed_root=managed_root,
            registered_path=managed.path,
        )
    assert dirty_error.value.code is ErrorCode.WORKTREE_DIRTY
    remove_managed_worktree(
        repository,
        managed_root=managed_root,
        registered_path=managed.path,
        force=True,
    )


def test_result_commit_and_internal_ref_use_compare_and_swap(tmp_path: Path) -> None:
    repository, base_oid = _repository(tmp_path)
    managed = create_managed_worktree(
        repository,
        managed_root=tmp_path / "managed",
        task_id="task-publish",
        base_oid=base_oid,
    )
    (managed.path / "tracked.txt").write_text("result one\n", encoding="utf-8")
    first_validation = validate_worktree(managed, scopes=["tracked.txt"])
    first_commit = create_result_commit(
        managed.path, first_validation, task_id="task-publish"
    )
    publication = publish_task_ref(
        repository,
        task_id="task-publish",
        new_commit_oid=first_commit,
        expected_old_oid=None,
    )
    assert publication.ref == "refs/llm-coord/tasks/task-publish"
    assert _git(repository, "rev-parse", publication.ref) == first_commit
    assert _git(managed.path, "rev-parse", "HEAD") == base_oid

    (managed.path / "tracked.txt").write_text("result two\n", encoding="utf-8")
    second_validation = validate_worktree(managed, scopes=["tracked.txt"])
    second_commit = create_result_commit(
        managed.path, second_validation, task_id="task-publish"
    )
    with pytest.raises(LlmCoordError) as cas_error:
        publish_task_ref(
            repository,
            task_id="task-publish",
            new_commit_oid=second_commit,
            expected_old_oid=base_oid,
        )
    assert cas_error.value.code is ErrorCode.TARGET_MOVED
    assert _git(repository, "rev-parse", publication.ref) == first_commit

    updated = publish_task_ref(
        repository,
        task_id="task-publish",
        new_commit_oid=second_commit,
        expected_old_oid=first_commit,
    )
    assert updated.commit_oid == second_commit
    assert _git(repository, "rev-parse", publication.ref) == second_commit


def test_publish_validated_result_requires_absent_ref(tmp_path: Path) -> None:
    repository, base_oid = _repository(tmp_path)
    managed = create_managed_worktree(
        repository,
        managed_root=tmp_path / "managed",
        task_id="task-combined",
        base_oid=base_oid,
    )
    (managed.path / "new.txt").write_text("new\n", encoding="utf-8")
    validation = validate_worktree(managed, scopes=["new.txt"])

    publication = publish_validated_result(
        managed.path,
        validation,
        task_id="task-combined",
    )
    assert _git(repository, "rev-parse", publication.ref) == publication.commit_oid
    assert _git(repository, "rev-parse", "HEAD") == base_oid

    with pytest.raises(LlmCoordError) as duplicate_error:
        publish_validated_result(
            managed.path,
            validation,
            task_id="task-combined",
        )
    assert duplicate_error.value.code is ErrorCode.TARGET_MOVED


@pytest.mark.parametrize(
    "malformed",
    [
        b"M\0path-without-final-nul",
        b"R100\0only-one-path\0",
        b"unknown\0path\0",
        b"M\0\0",
    ],
)
def test_name_status_parser_fails_closed_on_ambiguous_data(malformed: bytes) -> None:
    with pytest.raises(LlmCoordError) as error:
        parse_name_status_z(malformed)
    assert error.value.code is ErrorCode.REPOSITORY_UNSAFE


@pytest.mark.parametrize(
    "malformed",
    [
        b"1 broken\0",
        b"2 M. N... 100644 100644 100644 a b R100 destination\0",
        b"? missing-final-nul",
        b"! ignored\0",
    ],
)
def test_porcelain_parser_fails_closed_on_ambiguous_data(malformed: bytes) -> None:
    with pytest.raises(LlmCoordError) as error:
        parse_porcelain_v2_z(malformed)
    assert error.value.code is ErrorCode.REPOSITORY_UNSAFE
