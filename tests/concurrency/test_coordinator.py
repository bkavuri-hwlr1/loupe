from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import (
    ClaimAuthorityError,
    ClaimRecord,
    ClaimState,
    ReleaseResult,
)
from llm_cli.storage.control import ControlStore

NOW = 1_750_000_000_000
REPO_KEY = "a" * 64


@dataclass(frozen=True)
class Harness:
    store: ControlStore
    coordinator: RepositoryCoordinator
    task_ids: tuple[str, ...]


def _harness(
    tmp_path: Path,
    *task_ids: str,
    launch_lease_ms: int = 1_000,
    work_lease_ms: int = 300,
) -> Harness:
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
            repository_id="repo_test",
            task_id=task_id,
            title=task_id,
            now=NOW,
        )
    return Harness(
        store=store,
        coordinator=RepositoryCoordinator(
            store,
            launch_lease_ms=launch_lease_ms,
            work_lease_ms=work_lease_ms,
        ),
        task_ids=tuple(task_ids),
    )


def _claim(
    harness: Harness,
    task_id: str,
    scope: str,
    *,
    now: int = NOW + 1,
) -> ClaimRecord:
    return harness.coordinator.request_claim(task_id, (scope,), now=now)


def _release(
    harness: Harness,
    claim: ClaimRecord,
    *,
    now: int,
) -> ReleaseResult:
    return harness.coordinator.release_claim(
        claim.claim_id,
        reason="completed",
        expected_fencing_token=claim.fencing_token,
        now=now,
    )


def test_duplicate_request_is_idempotent(tmp_path: Path) -> None:
    harness = _harness(tmp_path, "task_a")

    first = _claim(harness, "task_a", "src/")
    duplicate = _claim(harness, "task_a", "src/", now=NOW + 2)

    assert duplicate == first
    assert harness.store.list_claims(REPO_KEY) == (first,)


def test_simultaneous_overlapping_requests_have_one_owner(tmp_path: Path) -> None:
    harness = _harness(tmp_path, "task_a", "task_b")
    barrier = threading.Barrier(3)

    def request(task_id: str) -> ClaimRecord:
        barrier.wait(timeout=5)
        return _claim(harness, task_id, "src/parser.py")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(request, task_id) for task_id in harness.task_ids]
        barrier.wait(timeout=5)
        claims = [future.result(timeout=10) for future in futures]

    owners = [claim for claim in claims if claim.state is ClaimState.ACTIVE_WORK]
    waiters = [claim for claim in claims if claim.state is ClaimState.QUEUED]
    assert len(owners) == 1
    assert len(waiters) == 1
    assert waiters[0].blocking_claim_ids == (owners[0].claim_id,)
    assert owners[0].fencing_token is not None
    assert waiters[0].fencing_token is None


def test_overlapping_waiters_activate_in_fifo_order(tmp_path: Path) -> None:
    harness = _harness(tmp_path, "holder", "first", "second")
    holder = _claim(harness, "holder", "src/")
    first = _claim(harness, "first", "src/parser.py", now=NOW + 2)
    second = _claim(harness, "second", "src/parser.py", now=NOW + 3)
    assert first.state is ClaimState.QUEUED
    assert second.state is ClaimState.QUEUED

    first_release = _release(harness, holder, now=NOW + 4)
    assert tuple(item.task_id for item in first_release.activated) == ("first",)
    first_active = harness.store.get_claim(first.claim_id)
    second_waiting = harness.store.get_claim(second.claim_id)
    assert first_active is not None and first_active.granted
    assert second_waiting is not None
    assert second_waiting.state is ClaimState.QUEUED
    assert second_waiting.blocking_claim_ids == (first.claim_id,)

    second_release = _release(harness, first_active, now=NOW + 5)
    assert tuple(item.task_id for item in second_release.activated) == ("second",)


def test_disjoint_claims_run_concurrently(tmp_path: Path) -> None:
    harness = _harness(tmp_path, "source", "docs")

    source = _claim(harness, "source", "src/")
    docs = _claim(harness, "docs", "docs/", now=NOW + 2)

    assert source.granted
    assert docs.granted
    assert source.fencing_token != docs.fencing_token


def test_broad_waiter_cannot_be_starved_by_a_new_disjoint_request(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, "holder", "broad", "newcomer")
    holder = _claim(harness, "holder", "src/")
    broad = _claim(harness, "broad", "*", now=NOW + 2)
    newcomer = _claim(harness, "newcomer", "docs/", now=NOW + 3)

    assert broad.state is ClaimState.QUEUED
    assert newcomer.state is ClaimState.QUEUED
    assert broad.claim_id in newcomer.blocking_claim_ids

    result = _release(harness, holder, now=NOW + 4)
    assert tuple(item.task_id for item in result.activated) == ("broad",)
    still_waiting = harness.store.get_claim(newcomer.claim_id)
    assert still_waiting is not None
    assert still_waiting.state is ClaimState.QUEUED
    assert still_waiting.blocking_claim_ids == (broad.claim_id,)


def test_release_activates_multiple_mutually_disjoint_waiters(tmp_path: Path) -> None:
    harness = _harness(tmp_path, "holder", "source", "docs", "tests")
    holder = _claim(harness, "holder", "*")
    waiters = (
        _claim(harness, "source", "src/", now=NOW + 2),
        _claim(harness, "docs", "docs/", now=NOW + 3),
        _claim(harness, "tests", "tests/", now=NOW + 4),
    )
    assert all(claim.state is ClaimState.QUEUED for claim in waiters)

    result = _release(harness, holder, now=NOW + 5)

    assert tuple(item.task_id for item in result.activated) == (
        "source",
        "docs",
        "tests",
    )
    fences = [holder.fencing_token, *(item.fencing_token for item in result.activated)]
    assert all(fence is not None for fence in fences)
    concrete_fences = [fence for fence in fences if fence is not None]
    assert len(concrete_fences) == len(fences)
    assert concrete_fences == sorted(concrete_fences)
    assert len(set(concrete_fences)) == len(concrete_fences)


def test_renewal_requires_the_exact_fence_and_an_unexpired_lease(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path,
        "task_a",
        launch_lease_ms=100,
        work_lease_ms=30,
    )
    claim = _claim(harness, "task_a", "src/")
    assert claim.fencing_token is not None

    with pytest.raises(ClaimAuthorityError, match="stale"):
        harness.coordinator.renew_claim(
            task_id="task_a",
            claim_id=claim.claim_id,
            fencing_token=claim.fencing_token + 1,
            attempt=1,
            now=NOW + 2,
        )

    renewed = harness.coordinator.renew_claim(
        task_id="task_a",
        claim_id=claim.claim_id,
        fencing_token=claim.fencing_token,
        attempt=1,
        now=NOW + 3,
    )
    assert renewed.lease_expires_at == NOW + 33

    with pytest.raises(ClaimAuthorityError, match="stale"):
        harness.coordinator.renew_claim(
            task_id="task_a",
            claim_id=claim.claim_id,
            fencing_token=claim.fencing_token,
            attempt=1,
            now=NOW + 33,
        )


class _Clocks:
    """A wall clock that keeps running while asleep, and a monotonic one."""

    def __init__(self) -> None:
        self.wall = 1_000.0
        self.monotonic = 50.0

    def sleep(self, seconds: float) -> None:
        self.wall += seconds

    def run(self, seconds: float) -> None:
        self.wall += seconds
        self.monotonic += seconds


def test_time_asleep_does_not_count_against_leases(tmp_path: Path) -> None:
    harness = _harness(tmp_path, "owner", "waiter")
    clocks = _Clocks()
    coordinator = RepositoryCoordinator(
        harness.store,
        launch_lease_ms=100,
        work_lease_ms=30,
        wall_clock=lambda: clocks.wall,
        monotonic_clock=lambda: clocks.monotonic,
    )
    owner = coordinator.request_claim("owner", ("src/",), now=NOW + 1)
    waiter = coordinator.request_claim("waiter", ("src/parser.py",), now=NOW + 2)
    assert owner.fencing_token is not None
    assert owner.lease_expires_at == NOW + 101

    # Ten minutes asleep. The first call afterwards fails and rolls back, which
    # must not undo the lease time given back for the sleep.
    clocks.sleep(600)
    with pytest.raises(ClaimAuthorityError, match="stale"):
        coordinator.renew_claim(
            task_id="owner",
            claim_id=owner.claim_id,
            fencing_token=owner.fencing_token + 1,
            attempt=1,
            now=NOW + 600_050,
        )
    result = coordinator.reconcile_expired(REPO_KEY, now=NOW + 600_050)
    assert result.expired_claim_ids == ()
    stored = harness.store.get_claim(owner.claim_id)
    assert stored is not None and stored.state is ClaimState.ACTIVE_WORK
    assert stored.lease_expires_at == NOW + 600_101
    renewed = coordinator.renew_claim(
        task_id="owner",
        claim_id=owner.claim_id,
        fencing_token=owner.fencing_token,
        attempt=1,
        now=NOW + 600_060,
    )
    assert renewed.lease_expires_at == NOW + 600_090

    # Awake, a worker that stops renewing still loses its lease.
    clocks.run(600)
    result = coordinator.reconcile_expired(REPO_KEY, now=NOW + 1_200_090)
    assert result.expired_claim_ids == (owner.claim_id,)
    assert [claim.claim_id for claim in result.activated] == [waiter.claim_id]


def test_reconcile_expires_owner_and_activates_waiter_with_a_new_fence(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path,
        "owner",
        "waiter",
        launch_lease_ms=100,
        work_lease_ms=30,
    )
    owner = _claim(harness, "owner", "src/")
    waiter = _claim(harness, "waiter", "src/parser.py", now=NOW + 2)
    assert owner.fencing_token is not None
    assert waiter.state is ClaimState.QUEUED

    result = harness.coordinator.reconcile_expired(
        REPO_KEY,
        now=NOW + 101,
    )

    assert result.expired_claim_ids == (owner.claim_id,)
    assert result.expired_task_ids == ("owner",)
    assert tuple(item.claim_id for item in result.activated) == (waiter.claim_id,)
    activated = result.activated[0]
    assert activated.fencing_token is not None
    assert activated.fencing_token > owner.fencing_token
    assert activated.lease_expires_at == NOW + 201
    stored_owner = harness.store.get_claim(owner.claim_id)
    assert stored_owner is not None
    assert stored_owner.state is ClaimState.EXPIRED
