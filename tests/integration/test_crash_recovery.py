"""Recovery of executions abandoned when a daemon stops mid-flight.

Each scenario kills the worker with ``KeyboardInterrupt`` at a precise point.
That is deliberate: the runner catches ``Exception``, so a ``BaseException``
leaves exactly the durable state a killed process would leave -- no failure
record, no cleanup, no release -- which is what recovery has to reason about.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import ClaimRecord, ClaimState, TaskRecord
from llm_cli.execution import runner as runner_module
from llm_cli.execution.recovery import (
    DAEMON_INTERRUPTED,
    PUBLICATION_AMBIGUOUS,
    ExecutionReconciler,
)
from llm_cli.execution.runner import FixtureWriteRunner, parse_fixture_writes
from llm_cli.git.inspect import inspect_repository
from llm_cli.git.integrate import publish_task_ref
from llm_cli.git.worktrees import create_managed_worktree, list_git_worktrees
from llm_cli.storage.control import ControlStore

DEAD_BOOT = "boot-dead"
LIVE_BOOT = "boot-live"


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


@dataclass
class Harness:
    """One registered repository plus the stores an abandoned boot left behind."""

    repository: Path
    store: ControlStore
    coordinator: RepositoryCoordinator
    worktree_root: Path

    def runner(self, boot_id: str = DEAD_BOOT) -> FixtureWriteRunner:
        return FixtureWriteRunner(
            self.store,
            self.coordinator,
            managed_worktree_root=self.worktree_root,
            boot_id=boot_id,
        )

    def reconciler(self, boot_id: str = LIVE_BOOT) -> ExecutionReconciler:
        return ExecutionReconciler(
            self.store,
            self.coordinator,
            managed_worktree_root=self.worktree_root,
            boot_id=boot_id,
        )

    def task(self, task_id: str, scopes: list[str]) -> tuple[TaskRecord, ClaimRecord]:
        registered = self.store.list_repositories()[0]
        created = self.store.create_task(
            repository_id=registered.repository_id,
            task_id=task_id,
            title=task_id,
        )
        claim = self.coordinator.request_claim(created.task_id, scopes)
        task = self.store.get_task(created.task_id)
        assert task is not None
        return task, claim

    def run_until_interrupt(
        self, task: TaskRecord, claim: ClaimRecord, write: str
    ) -> None:
        with pytest.raises(KeyboardInterrupt):
            self.runner().execute(
                repository=self.store.list_repositories()[0],
                task=task,
                claim=claim,
                writes=parse_fixture_writes([write]),
            )

    def claim_state(self, claim: ClaimRecord) -> ClaimState:
        fresh = self.store.get_claim(claim.claim_id)
        assert fresh is not None
        return fresh.state

    def task_ref(self, task_id: str) -> str | None:
        result = subprocess.run(
            [
                "git",
                "show-ref",
                "--verify",
                "--hash",
                f"refs/llm-coord/tasks/{task_id}",
            ],
            cwd=self.repository,
            capture_output=True,
            text=True,
            env={"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1"},
        )
        return result.stdout.strip() or None


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
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
        repository_id="repo_recovery",
        repo_key=info.repo_key,
        display_name=info.display_name,
        git_common_dir=str(info.common_git_dir),
        main_worktree_path=str(info.main_worktree),
        target_ref=info.target_ref,
        profile_id="default",
        remote_identity=info.normalized_remote,
        object_format=info.object_format,
        integration_adapter=info.integration_adapter,
        path_case_insensitive=info.path_case_insensitive,
    )
    return Harness(
        repository=repository,
        store=store,
        coordinator=RepositoryCoordinator(store),
        worktree_root=tmp_path / "managed-worktrees",
    )


def test_interrupt_before_publication_releases_the_claim_and_wakes_a_waiter(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def die(*args: object, **kwargs: object) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_module, "create_result_commit", die)
    task, claim = harness.task("task-interrupted", ["docs/"])
    _, waiting = harness.task("task-waiting", ["docs/guide.md"])
    assert waiting.state is ClaimState.QUEUED

    harness.run_until_interrupt(task, claim, "docs/guide.md=work in progress\n")
    interrupted = harness.store.get_execution("task-interrupted", 1)
    assert interrupted is not None
    assert interrupted.state == "running"
    assert Path(interrupted.worktree_path).exists()

    report = harness.reconciler().reconcile()

    assert report.summary == {"released": 1}
    assert report.blocking == ()
    settled = harness.store.get_execution("task-interrupted", 1)
    assert settled is not None
    assert settled.state == "failed"
    assert settled.failure_code == DAEMON_INTERRUPTED
    assert not Path(settled.worktree_path).exists()

    assert harness.claim_state(claim) is ClaimState.RELEASED
    assert harness.claim_state(waiting) is ClaimState.ACTIVE_WORK


def test_interrupt_after_a_successful_swap_confirms_the_publication(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def publish_then_die(*args: object, **kwargs: object) -> object:
        publish_task_ref(*args, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_module, "publish_task_ref", publish_then_die)
    task, claim = harness.task("task-swapped", ["docs/"])
    harness.run_until_interrupt(task, claim, "docs/guide.md=published\n")

    published_oid = harness.task_ref("task-swapped")
    assert published_oid is not None
    intent = harness.store.latest_publication_intent("task-swapped", 1)
    assert intent is not None and intent.operation_state == "prepared"

    report = harness.reconciler().reconcile()

    assert report.summary == {"confirmed": 1}
    confirmed = harness.store.get_publication_intent(intent.intent_id)
    assert confirmed is not None
    assert confirmed.operation_state == "confirmed"
    assert confirmed.result_commit_id == published_oid

    assert harness.claim_state(claim) is ClaimState.ACTIVE_INTEGRATION
    settled = harness.store.get_execution("task-swapped", 1)
    assert settled is not None
    assert settled.state == "published"
    assert not Path(settled.worktree_path).exists()
    assert harness.store.get_task("task-swapped").state == "ready_for_integration"  # type: ignore[union-attr]


def test_interrupt_before_the_swap_fails_safe_and_wakes_a_waiter(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def die(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_module, "publish_task_ref", die)
    task, claim = harness.task("task-unswapped", ["docs/"])
    _, waiting = harness.task("task-second", ["docs/"])

    harness.run_until_interrupt(task, claim, "docs/guide.md=never published\n")
    assert harness.task_ref("task-unswapped") is None
    intent = harness.store.latest_publication_intent("task-unswapped", 1)
    assert intent is not None and intent.operation_state == "prepared"

    report = harness.reconciler().reconcile()

    assert report.summary == {"failed_safe": 1}
    resolved = harness.store.get_publication_intent(intent.intent_id)
    assert resolved is not None
    assert resolved.operation_state == "failed_safe"

    assert harness.claim_state(claim) is ClaimState.RELEASED
    assert harness.claim_state(waiting) is ClaimState.ACTIVE_WORK
    settled = harness.store.get_execution("task-unswapped", 1)
    assert settled is not None
    assert settled.state == "failed"
    assert not Path(settled.worktree_path).exists()


def test_a_reference_that_moved_elsewhere_stays_blocking(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def die(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_module, "publish_task_ref", die)
    task, claim = harness.task("task-ambiguous", ["docs/"])
    _, waiting = harness.task("task-blocked", ["docs/"])
    harness.run_until_interrupt(task, claim, "docs/guide.md=unknown outcome\n")

    # Something else moved the reference while the daemon was gone, so neither
    # "it published" nor "it did not" can be read off the repository.
    third_party = _git(harness.repository, "rev-parse", "HEAD")
    _git(
        harness.repository,
        "update-ref",
        "refs/llm-coord/tasks/task-ambiguous",
        third_party,
    )

    report = harness.reconciler().reconcile()

    assert report.summary == {"operator_attention": 1}
    assert [outcome.task_id for outcome in report.blocking] == ["task-ambiguous"]

    intent = harness.store.latest_publication_intent("task-ambiguous", 1)
    assert intent is not None
    assert intent.operation_state == "operator_attention"

    assert harness.claim_state(claim) is ClaimState.PUBLISHING
    assert harness.claim_state(waiting) is ClaimState.QUEUED

    settled = harness.store.get_execution("task-ambiguous", 1)
    assert settled is not None
    assert settled.state == "operator_attention"
    assert settled.failure_code == PUBLICATION_AMBIGUOUS
    # The worktree is the only remaining evidence of what the attempt produced.
    assert Path(settled.worktree_path).exists()


def test_an_unresolvable_reservation_survives_repeated_expiry_reconciliation(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def die(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_module, "publish_task_ref", die)
    task, claim = harness.task("task-held", ["docs/"])
    harness.run_until_interrupt(task, claim, "docs/guide.md=held\n")
    _git(
        harness.repository,
        "update-ref",
        "refs/llm-coord/tasks/task-held",
        _git(harness.repository, "rev-parse", "HEAD"),
    )
    harness.reconciler().reconcile()

    # A publishing reservation holds no lease, so no amount of elapsed time can
    # retire it. That is what keeps an unknown outcome from silently expiring.
    harness.coordinator.reconcile_expired(now=10**13)

    assert harness.claim_state(claim) is ClaimState.PUBLISHING


def test_recovery_is_idempotent(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def die(*args: object, **kwargs: object) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_module, "create_result_commit", die)
    task, claim = harness.task("task-twice", ["docs/"])
    harness.run_until_interrupt(task, claim, "docs/guide.md=once\n")

    first = harness.reconciler().reconcile()
    second = harness.reconciler().reconcile()

    assert first.summary == {"released": 1}
    assert second.outcomes == ()
    assert harness.claim_state(claim) is ClaimState.RELEASED


def test_recovery_leaves_this_boot_s_own_execution_alone(harness: Harness) -> None:
    task, claim = harness.task("task-live", ["docs/"])
    harness.store.create_execution(
        task_id=task.task_id,
        attempt=1,
        claim_id=claim.claim_id,
        driver="fixture_write",
        worktree_path=str(harness.worktree_root / task.task_id),
        base_oid=_git(harness.repository, "rev-parse", "HEAD"),
        boot_id=LIVE_BOOT,
    )

    report = harness.reconciler(LIVE_BOOT).reconcile()

    assert report.outcomes == ()
    execution = harness.store.get_execution(task.task_id, 1)
    assert execution is not None
    assert execution.state == "preparing"
    assert harness.claim_state(claim) is ClaimState.ACTIVE_WORK


def test_a_worktree_directory_removed_by_hand_is_pruned_from_git(
    harness: Harness,
) -> None:
    task, claim = harness.task("task-vanished", ["docs/"])
    base_oid = _git(harness.repository, "rev-parse", "HEAD")
    managed = create_managed_worktree(
        harness.repository,
        managed_root=harness.worktree_root,
        task_id=task.task_id,
        base_oid=base_oid,
    )
    harness.store.create_execution(
        task_id=task.task_id,
        attempt=1,
        claim_id=claim.claim_id,
        driver="fixture_write",
        worktree_path=str(managed.path),
        base_oid=base_oid,
        boot_id=DEAD_BOOT,
    )
    # An operator deleting the directory leaves Git still owning the record,
    # which would make every later attempt fail to create its worktree.
    subprocess.run(["rm", "-rf", str(managed.path)], check=True)
    assert any(
        record.path == managed.path for record in list_git_worktrees(harness.repository)
    )

    report = harness.reconciler().reconcile()

    assert report.summary == {"released": 1}
    assert not any(
        record.path == managed.path for record in list_git_worktrees(harness.repository)
    )


def test_an_execution_written_before_boot_ownership_existed_is_recovered(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def die(*args: object, **kwargs: object) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_module, "create_result_commit", die)
    task, claim = harness.task("task-legacy", ["docs/"])
    harness.run_until_interrupt(task, claim, "docs/guide.md=legacy\n")
    # Rows migrated from a schema without the column carry no boot at all, and
    # they too predate the running process, so they must not be mistaken for
    # work that something is still advancing.
    with harness.store.connection() as connection:
        connection.execute("UPDATE task_executions SET boot_id = NULL")
        connection.commit()

    report = harness.reconciler(DEAD_BOOT).reconcile()

    assert report.summary == {"released": 1}
    assert harness.claim_state(claim) is ClaimState.RELEASED
