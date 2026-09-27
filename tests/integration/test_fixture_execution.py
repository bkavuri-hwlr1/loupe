from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

from llm_cli.agent.driver import DriverCapabilities, RunRequest, RunResult
from llm_cli.agent.tools import ToolBroker
from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import ClaimState
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.execution.runner import (
    FixtureWrite,
    FixtureWriteRunner,
    TaskExecutionRunner,
    parse_fixture_writes,
)
from llm_cli.git.inspect import inspect_repository
from llm_cli.storage.control import ControlStore


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, ControlStore, RepositoryCoordinator]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")

    store = ControlStore(tmp_path / "control.sqlite3")
    store.initialize()
    info = inspect_repository(repository, profile_id="default")
    store.register_repository(
        repository_id="repo_fixture",
        repo_key=info.repo_key,
        display_name=info.display_name,
        git_common_dir=str(info.common_git_dir),
        main_worktree_path=str(info.main_worktree),
        target_ref=info.target_ref,
        profile_id="default",
        remote_identity=info.normalized_remote,
        object_format=info.object_format,
        integration_adapter=info.integration_adapter,
    )
    return repository, store, RepositoryCoordinator(store)


def test_fixture_execution_publishes_a_validated_internal_branch(
    tmp_path: Path,
) -> None:
    repository, store, coordinator = _repository(tmp_path)
    registered = store.list_repositories()[0]
    task = store.create_task(
        repository_id=registered.repository_id,
        task_id="task-fixture-publish",
        title="write documentation",
    )
    claim = coordinator.request_claim(task.task_id, ["docs/"])
    assert claim.state is ClaimState.ACTIVE_WORK

    result = FixtureWriteRunner(
        store,
        coordinator,
        managed_worktree_root=tmp_path / "managed-worktrees",
        boot_id="boot-test",
    ).execute(
        repository=registered,
        task=task,
        claim=claim,
        writes=parse_fixture_writes(["docs/guide.md=fixture content\n"]),
    )

    assert result.execution.state == "published"
    assert result.worktree_cleaned
    assert not Path(result.execution.worktree_path).exists()
    assert result.claim.state is ClaimState.ACTIVE_INTEGRATION
    assert result.publication_intent.operation_state == "confirmed"
    assert (
        _git(repository, "rev-parse", result.publication.ref)
        == result.publication.commit_oid
    )
    assert (
        _git(repository, "show", f"{result.publication.ref}:docs/guide.md")
        == "fixture content"
    )
    assert not (repository / "docs" / "guide.md").exists()


def test_isolated_read_only_execution_completes_without_a_publication_ref(
    tmp_path: Path,
) -> None:
    repository, store, coordinator = _repository(tmp_path)
    registered = store.list_repositories()[0]
    task = store.create_task(
        repository_id=registered.repository_id,
        task_id="task-read-only",
        title="explain the repository",
    )
    claim = coordinator.request_claim(task.task_id, ["*"])

    class ReadOnlyDriver:
        name = "read_only_fixture"
        capabilities: ClassVar[DriverCapabilities] = DriverCapabilities(resumable=True)

        def run(self, request: RunRequest, tools: ToolBroker) -> RunResult:
            del tools
            if request.checkpoint is not None:
                request.checkpoint(
                    {
                        "version": 2,
                        "provider": "fixture",
                        "model": "fixture",
                        "phase": "finished",
                        "session": {},
                        "tool_usage": {"finished": True},
                    }
                )
            return RunResult(
                summary="Inspected the repository.",
                answer="This repository contains a read-only fixture.",
            )

    result = TaskExecutionRunner(
        store,
        coordinator,
        managed_worktree_root=tmp_path / "managed-worktrees",
        boot_id="boot-test",
    ).execute(
        repository=registered,
        task=task,
        claim=claim,
        driver=ReadOnlyDriver(),
    )

    assert result.run.answer == "This repository contains a read-only fixture."
    assert result.execution.state == "published"
    assert result.task.state == "completed"
    assert result.claim.state is ClaimState.RELEASED
    assert result.publication is None and result.publication_intent is None
    assert result.validation is not None and not result.validation.changed_paths
    assert result.worktree_cleaned
    assert store.latest_publication_intent(task.task_id, task.attempt) is None
    assert _git(repository, "for-each-ref", "refs/llm-coord/tasks") == ""


def test_fixture_scope_failure_releases_claim_and_removes_worktree(
    tmp_path: Path,
) -> None:
    _, store, coordinator = _repository(tmp_path)
    registered = store.list_repositories()[0]
    task = store.create_task(
        repository_id=registered.repository_id,
        task_id="task-fixture-scope",
        title="unsafe write",
    )
    claim = coordinator.request_claim(task.task_id, ["docs/"])
    runner = FixtureWriteRunner(
        store,
        coordinator,
        managed_worktree_root=tmp_path / "managed-worktrees",
        boot_id="boot-test",
    )

    with pytest.raises(LlmCoordError) as failure:
        runner.execute(
            repository=registered,
            task=task,
            claim=claim,
            writes=(FixtureWrite(path="src/outside.py", content="outside\n"),),
        )

    assert failure.value.code is ErrorCode.SCOPE_VIOLATION
    stored_claim = store.get_claim(claim.claim_id)
    assert stored_claim is not None
    assert stored_claim.state is ClaimState.RELEASED
    execution = store.get_execution(task.task_id, task.attempt)
    assert execution is not None
    assert execution.state == "failed"
    assert not Path(execution.worktree_path).exists()
