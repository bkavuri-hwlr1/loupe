"""A finished claim must record why it finished, and not block a retry."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import ClaimConflict, ClaimState
from llm_cli.storage.control import ControlStore

NOW = 1_750_000_000_000
REPO_KEY = "f" * 64


def _store_and_coordinator(
    tmp_path: Path, *task_ids: str
) -> tuple[ControlStore, RepositoryCoordinator]:
    store = ControlStore(tmp_path / "control.sqlite3", timeout_seconds=5)
    store.initialize()
    store.register_repository(
        repository_id="repo_test",
        repo_key=REPO_KEY,
        display_name="fixture",
        git_common_dir=str(tmp_path / "repo" / ".git"),
        main_worktree_path=str(tmp_path / "repo"),
        target_ref="refs/heads/main",
        now=NOW,
    )
    for task_id in task_ids:
        store.create_task(
            repository_id="repo_test", task_id=task_id, title=task_id, now=NOW
        )
    return store, RepositoryCoordinator(store)


def test_worker_failure_is_recorded_as_failed_with_its_code(tmp_path: Path) -> None:
    store, coordinator = _store_and_coordinator(tmp_path, "task_a")
    claim = coordinator.request_claim("task_a", ["src/"], now=NOW)

    coordinator.release_claim(
        claim.claim_id,
        reason="fixture_execution_failed",
        expected_fencing_token=claim.fencing_token,
        task_state="failed",
        failure_code="SCOPE_VIOLATION",
        now=NOW,
    )

    task = store.get_task("task_a")
    assert task is not None
    assert task.state == "failed"
    with store.connection() as connection:
        row = connection.execute(
            "SELECT failure_code FROM tasks WHERE task_id = ?", ("task_a",)
        ).fetchone()
    assert row["failure_code"] == "SCOPE_VIOLATION"


def test_operator_cancel_remains_a_cancel(tmp_path: Path) -> None:
    store, coordinator = _store_and_coordinator(tmp_path, "task_a")
    claim = coordinator.request_claim("task_a", ["src/"], now=NOW)

    coordinator.release_claim(claim.claim_id, reason="operator asked", now=NOW)

    task = store.get_task("task_a")
    assert task is not None
    assert task.state == "cancelled"
    with store.connection() as connection:
        row = connection.execute(
            "SELECT failure_code FROM tasks WHERE task_id = ?", ("task_a",)
        ).fetchone()
    assert row["failure_code"] is None


def test_release_rejects_an_unknown_task_outcome(tmp_path: Path) -> None:
    _, coordinator = _store_and_coordinator(tmp_path, "task_a")
    claim = coordinator.request_claim("task_a", ["src/"], now=NOW)

    with pytest.raises(ValueError):
        coordinator.release_claim(
            claim.claim_id, reason="x", task_state="completed", now=NOW
        )


def test_finished_attempt_refuses_a_new_claim_until_retry(tmp_path: Path) -> None:
    """A dead claim must not be handed back as if it still granted authority."""

    store, coordinator = _store_and_coordinator(tmp_path, "task_a")
    claim = coordinator.request_claim("task_a", ["src/"], now=NOW)
    coordinator.release_claim(claim.claim_id, reason="done", now=NOW)

    with pytest.raises(ClaimConflict, match="task retry"):
        coordinator.request_claim("task_a", ["src/"], now=NOW)

    store.begin_new_attempt("task_a", now=NOW)
    retried = coordinator.request_claim("task_a", ["src/"], now=NOW)

    assert retried.state is ClaimState.ACTIVE_WORK
    assert retried.claim_id != claim.claim_id
    assert retried.task_attempt == 2
    assert retried.fencing_token is not None
    assert claim.fencing_token is not None
    assert retried.fencing_token > claim.fencing_token
