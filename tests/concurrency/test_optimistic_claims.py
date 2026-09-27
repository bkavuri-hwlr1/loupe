"""Private preparation can overlap; exclusive reservations retain FIFO authority."""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import (
    ClaimAuthorityError,
    ClaimConflict,
    ClaimRecord,
    ClaimState,
    ExecutionRecord,
    PublicationError,
    ScopeAuthorityError,
)
from llm_cli.storage.control import ControlStore

NOW = 2_000_000_000_000
REPO_KEY = "a" * 64


@dataclass(frozen=True)
class Harness:
    store: ControlStore
    coordinator: RepositoryCoordinator
    workspace_id: str
    checkout_path: str

    def claim(
        self,
        task_id: str,
        scope: str = "src/shared.py",
        *,
        mode: str = "optimistic",
    ) -> ClaimRecord:
        return self.coordinator.request_claim(
            task_id,
            (scope,),
            scheduling_mode=mode,
            optimistic_driver="coding_agent" if mode == "optimistic" else None,
            now=NOW,
        )

    def execution(self, claim: ClaimRecord) -> ExecutionRecord:
        return self.store.create_execution(
            task_id=claim.task_id,
            attempt=claim.task_attempt,
            claim_id=claim.claim_id,
            driver="coding_agent",
            worktree_path=self.checkout_path,
            base_oid="workspace:1:0",
            boot_id="boot-test",
            workspace_id=self.workspace_id,
            now=NOW,
        )


def _harness(tmp_path: Path, *task_ids: str) -> Harness:
    store = ControlStore(tmp_path / "control.sqlite3", timeout_seconds=5)
    store.initialize()
    repository = store.register_repository(
        repository_id="repo_test",
        repo_key=REPO_KEY,
        display_name="fixture",
        git_common_dir=str(tmp_path / "repo" / ".git"),
        main_worktree_path=str(tmp_path / "repo"),
        target_ref="refs/heads/main",
        path_case_insensitive=False,
        now=NOW,
    )
    checkout = store.ensure_checkout(
        repository=repository,
        canonical_path=repository.main_worktree_path,
        git_common_dir=repository.git_common_dir,
        now=NOW,
    )
    workspace = store.ensure_shared_workspace(checkout, now=NOW)
    for task_id in task_ids:
        session_id = f"session-{task_id}"
        store.open_session(
            session_id=session_id,
            checkout_id=checkout.checkout_id,
            resume_token_hash=hashlib.sha256(session_id.encode()).hexdigest(),
            provider="scripted",
            model="test",
            workspace_id=workspace.workspace_id,
            now=NOW,
        )
        with store.connection() as connection:
            connection.execute(
                "UPDATE sessions SET state = 'active' WHERE session_id = ?",
                (session_id,),
            )
        store.create_task(
            repository_id=repository.repository_id,
            session_id=session_id,
            task_id=task_id,
            title=task_id,
            now=NOW,
        )
        _save_launch(store, task_id, attempt=1)
    return Harness(
        store,
        RepositoryCoordinator(store, launch_lease_ms=1_000, work_lease_ms=300),
        workspace.workspace_id,
        checkout.canonical_path,
    )


def _save_launch(store: ControlStore, task_id: str, *, attempt: int) -> None:
    store.save_execution_launch(
        task_id=task_id,
        attempt=attempt,
        driver="coding_agent",
        instructions="Prepare a scoped private edit",
        interactive=True,
        parameters={},
        now=NOW,
    )


def test_same_file_optimistic_requests_grant_concurrently_with_distinct_fences(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, "first", "second")
    barrier = threading.Barrier(3)

    def request(task_id: str) -> ClaimRecord:
        barrier.wait(timeout=5)
        return harness.claim(task_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(request, name) for name in ("first", "second")]
        barrier.wait(timeout=5)
        claims = [future.result(timeout=10) for future in futures]

    assert all(claim.granted and not claim.blocking_claim_ids for claim in claims)
    assert len({claim.fencing_token for claim in claims}) == 2
    assert all(claim.workspace_id == harness.workspace_id for claim in claims)
    assert all(claim.scheduling_mode == "optimistic" for claim in claims)
    first = claims[0]
    assert first.fencing_token is not None
    renewed = harness.coordinator.renew_claim(
        task_id=first.task_id,
        claim_id=first.claim_id,
        fencing_token=first.fencing_token,
        attempt=first.task_attempt,
        now=NOW + 1,
    )
    assert renewed.lease_expires_at == NOW + 301
    reopened = ControlStore(harness.store.path)
    assert reopened.get_claim(first.claim_id) == renewed


def test_exclusive_release_activates_overlapping_optimistic_waiters_together(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, "holder", "first", "second")
    holder = harness.claim("holder", mode="exclusive")
    first = harness.claim("first")
    second = harness.claim("second")
    assert first.state is second.state is ClaimState.QUEUED
    assert first.blocking_claim_ids == second.blocking_claim_ids == (holder.claim_id,)

    released = harness.coordinator.release_claim(holder.claim_id, reason="finished")

    assert [claim.task_id for claim in released.activated] == ["first", "second"]
    assert all(claim.granted for claim in released.activated)
    assert len({claim.fencing_token for claim in released.activated}) == 2


def test_optimistic_waiter_does_not_reserve_against_another_optimistic_request(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, "holder", "broad", "newcomer")
    holder = harness.claim("holder", "src/", mode="exclusive")
    broad = harness.claim("broad", "*")
    newcomer = harness.claim("newcomer", "docs/")
    assert broad.state is ClaimState.QUEUED
    assert newcomer.granted

    released = harness.coordinator.release_claim(holder.claim_id, reason="finished")
    assert [claim.task_id for claim in released.activated] == ["broad"]
    still_active = harness.store.get_claim(newcomer.claim_id)
    assert still_active is not None and still_active.granted


def test_older_exclusive_waiter_cannot_be_starved_by_optimistic_newcomers(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, "first", "exclusive", "later", "disjoint")
    first = harness.claim("first")
    exclusive = harness.claim("exclusive", mode="exclusive")
    later = harness.claim("later")
    disjoint = harness.claim("disjoint", "docs/")
    assert exclusive.blocking_claim_ids == (first.claim_id,)
    assert later.blocking_claim_ids == (exclusive.claim_id,)
    assert disjoint.granted

    released = harness.coordinator.release_claim(first.claim_id, reason="finished")
    assert [claim.task_id for claim in released.activated] == ["exclusive"]
    later = harness.store.get_claim(later.claim_id)
    assert later is not None and later.state is ClaimState.QUEUED
    released = harness.coordinator.release_claim(exclusive.claim_id, reason="finished")
    assert [claim.task_id for claim in released.activated] == ["later"]


def test_expiry_keeps_exclusive_waiter_ahead_of_newer_optimistic_work(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, "first", "exclusive", "later")
    first = harness.claim("first")
    exclusive = harness.claim("exclusive", mode="exclusive")
    later = harness.claim("later")

    result = harness.coordinator.reconcile_expired(REPO_KEY, now=NOW + 1_000)

    assert result.expired_claim_ids == (first.claim_id,)
    assert [claim.claim_id for claim in result.activated] == [exclusive.claim_id]
    later = harness.store.get_claim(later.claim_id)
    assert later is not None and later.blocking_claim_ids == (exclusive.claim_id,)
    assert later.state is ClaimState.QUEUED
    assert harness.store.get_claim(first.claim_id).scheduling_mode == "optimistic"


@pytest.mark.parametrize("reservation_state", ["publishing", "active_integration"])
def test_nonexpiring_optimistic_reservations_still_block_exclusive_work(
    tmp_path: Path, reservation_state: str
) -> None:
    harness = _harness(tmp_path, "publisher", "peer", "exclusive")
    publisher = harness.claim("publisher")
    execution = harness.execution(publisher)
    assert publisher.fencing_token is not None
    harness.coordinator.reserve_shared_publication(
        execution.execution_id,
        fencing_token=publisher.fencing_token,
        changed_paths=publisher.scopes,
    )
    # The shared path settles directly from publishing today. Keep the common
    # reservation predicate safe if an integration phase is added later.
    with harness.store.connection() as connection:
        connection.execute(
            "UPDATE claims SET state = ? WHERE claim_id = ?",
            (reservation_state, publisher.claim_id),
        )
    assert harness.claim("peer").granted
    exclusive = harness.claim("exclusive", mode="exclusive")
    assert publisher.claim_id in exclusive.blocking_claim_ids

    with pytest.raises(ClaimConflict, match="reservations require verified"):
        harness.coordinator.release_claim(publisher.claim_id, reason="too early")
    harness.coordinator.reconcile_expired(REPO_KEY, now=NOW + 10_000)
    stored = harness.store.get_claim(publisher.claim_id)
    assert stored is not None and stored.state == reservation_state
    exclusive = harness.store.get_claim(exclusive.claim_id)
    assert exclusive is not None and exclusive.blocking_claim_ids == (
        publisher.claim_id,
    )


def test_duplicate_cannot_change_mode_or_scopes_and_never_upgrades_legacy_claim(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, "optimistic", "legacy")
    optimistic = harness.claim("optimistic")
    legacy = harness.claim("legacy", mode="exclusive")
    assert harness.claim("optimistic") == optimistic
    assert harness.claim("legacy", mode="exclusive") == legacy
    for task_id, scope, mode in (
        ("optimistic", "src/shared.py", "exclusive"),
        ("legacy", "src/shared.py", "optimistic"),
        ("optimistic", "docs/", "optimistic"),
        ("legacy", "docs/", "exclusive"),
    ):
        with pytest.raises(ClaimConflict, match="different claim scopes or"):
            harness.claim(task_id, scope, mode=mode)
    assert harness.store.get_claim(optimistic.claim_id) == optimistic
    assert harness.store.get_claim(legacy.claim_id) == legacy


@pytest.mark.parametrize("next_mode", ["exclusive", "optimistic"])
def test_new_attempt_selects_mode_without_reusing_old_launch_or_authority(
    tmp_path: Path, next_mode: str
) -> None:
    harness = _harness(tmp_path, "task")
    first = harness.claim("task")
    harness.coordinator.release_claim(first.claim_id, reason="retry", now=NOW)
    with pytest.raises(ClaimConflict, match="task retry"):
        harness.claim("task", mode=next_mode)

    harness.store.begin_new_attempt("task", now=NOW)
    if next_mode == "optimistic":
        with pytest.raises(ClaimAuthorityError, match="approved saved launch"):
            harness.claim("task")
    _save_launch(harness.store, "task", attempt=2)
    retry = harness.claim("task", mode=next_mode)
    assert retry.task_attempt == 2
    assert retry.scheduling_mode == next_mode
    assert first.fencing_token is not None and retry.fencing_token is not None
    assert retry.fencing_token > first.fencing_token
    with pytest.raises(ClaimAuthorityError, match="stale"):
        harness.coordinator.renew_claim(
            task_id="task",
            claim_id=first.claim_id,
            fencing_token=first.fencing_token,
            attempt=1,
            now=NOW,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE tasks SET session_id = NULL",
        "DELETE FROM task_execution_launches",
        "UPDATE task_execution_launches SET driver = 'another_driver'",
        "UPDATE sessions SET state = 'closed'",
        "UPDATE sessions SET workspace_id = NULL",
        "UPDATE sessions SET workspace_mode = 'isolated'",
        "UPDATE workspaces SET mode = 'isolated'",
        "UPDATE workspaces SET kind = 'linked_worktree'",
        "UPDATE workspaces SET state = 'quarantined'",
        "UPDATE workspaces SET canonical_path = '/another-checkout'",
    ],
)
def test_optimistic_claim_requires_a_durable_active_shared_execution_binding(
    tmp_path: Path, mutation: str
) -> None:
    harness = _harness(tmp_path, "task")
    with harness.store.connection() as connection:
        connection.execute(mutation)

    with pytest.raises(ClaimAuthorityError, match="active shared session workspace"):
        harness.claim("task")
    assert harness.store.list_claims(REPO_KEY) == ()


def test_launch_requires_trusted_matching_driver_attestation(tmp_path: Path) -> None:
    harness = _harness(tmp_path, "task")
    for attested_driver in (None, "not_the_saved_driver"):
        with pytest.raises(ClaimAuthorityError, match="approved saved launch"):
            harness.coordinator.request_claim(
                "task",
                ("src/shared.py",),
                scheduling_mode="optimistic",
                optimistic_driver=attested_driver,
                now=NOW,
            )
    with pytest.raises(ValueError, match="exclusive or optimistic"):
        harness.claim("task", mode="unsafe")


@pytest.mark.parametrize("switch_checkout", [False, True])
def test_shared_session_cannot_bind_another_checkout_or_repository(
    tmp_path: Path, switch_checkout: bool
) -> None:
    harness = _harness(tmp_path, "task")
    other_repository = harness.store.register_repository(
        repo_key="b" * 64,
        display_name="other",
        git_common_dir=str(tmp_path / "other" / ".git"),
        main_worktree_path=str(tmp_path / "other"),
        target_ref="refs/heads/main",
        now=NOW,
    )
    other_checkout = harness.store.ensure_checkout(
        repository=other_repository,
        canonical_path=other_repository.main_worktree_path,
        git_common_dir=other_repository.git_common_dir,
        now=NOW,
    )
    workspace = harness.store.ensure_shared_workspace(other_checkout, now=NOW)
    with harness.store.connection() as connection:
        connection.execute(
            "UPDATE sessions SET workspace_id = ?", (workspace.workspace_id,)
        )
        if switch_checkout:
            connection.execute(
                "UPDATE sessions SET checkout_id = ?", (other_checkout.checkout_id,)
            )

    with pytest.raises(ClaimAuthorityError, match="active shared session workspace"):
        harness.claim("task")
    assert harness.store.list_claims(REPO_KEY) == ()


def test_optimistic_claim_cannot_publish_via_legacy_git_ref(tmp_path: Path) -> None:
    harness = _harness(tmp_path, "task")
    claim = harness.claim("task")
    assert claim.fencing_token is not None
    with pytest.raises(PublicationError, match="shared workspace batch publication"):
        harness.coordinator.begin_publication(
            task_id=claim.task_id,
            claim_id=claim.claim_id,
            fencing_token=claim.fencing_token,
            attempt=claim.task_attempt,
            changed_paths=claim.scopes,
            patch_hash="a" * 64,
            result_tree_id="b" * 40,
            now=NOW,
        )
    assert harness.store.get_claim(claim.claim_id) == claim


def test_optimistic_shared_publication_preserves_scope_and_fence_authority(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, "task")
    claim = harness.claim("task")
    execution = harness.execution(claim)
    assert claim.fencing_token is not None
    with pytest.raises(ScopeAuthorityError, match="out-of-scope"):
        harness.coordinator.reserve_shared_publication(
            execution.execution_id,
            fencing_token=claim.fencing_token,
            changed_paths=("docs/other.md",),
        )
    with pytest.raises(ClaimAuthorityError, match="stale"):
        harness.coordinator.reserve_shared_publication(
            execution.execution_id,
            fencing_token=claim.fencing_token + 1,
            changed_paths=claim.scopes,
        )
    assert harness.store.get_claim(claim.claim_id) == claim


def test_optimistic_publication_cannot_switch_execution_workspace(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, "task")
    claim = harness.claim("task")
    execution = harness.execution(claim)
    repository = harness.store.get_repository("repo_test")
    assert repository is not None
    other_checkout = harness.store.ensure_checkout(
        repository=repository,
        canonical_path=str(tmp_path / "other"),
        git_common_dir=str(tmp_path / "other" / ".git"),
        now=NOW,
    )
    other_workspace = harness.store.ensure_shared_workspace(other_checkout, now=NOW)
    with harness.store.connection() as connection:
        connection.execute(
            "UPDATE task_executions SET workspace_id = ? WHERE execution_id = ?",
            (other_workspace.workspace_id, execution.execution_id),
        )
    assert claim.fencing_token is not None
    with pytest.raises(ClaimAuthorityError, match="workspace does not match"):
        harness.coordinator.reserve_shared_publication(
            execution.execution_id,
            fencing_token=claim.fencing_token,
            changed_paths=claim.scopes,
        )
    assert harness.store.get_claim(claim.claim_id) == claim
