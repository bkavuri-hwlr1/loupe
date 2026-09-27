from __future__ import annotations

from pathlib import Path

import pytest

from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import ClaimConflict, ClaimState, ScopeAuthorityError
from llm_cli.storage.control import ControlStore

NOW = 1_750_000_000_000
REPO_KEY = "b" * 64


def _coordinator(tmp_path: Path) -> tuple[ControlStore, RepositoryCoordinator]:
    store = ControlStore(tmp_path / "control.sqlite3")
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
    store.create_task(
        repository_id="repo_test",
        task_id="publisher",
        now=NOW,
    )
    return store, RepositoryCoordinator(store, launch_lease_ms=100)


def test_publication_rejects_changed_paths_outside_the_live_claim(
    tmp_path: Path,
) -> None:
    store, coordinator = _coordinator(tmp_path)
    claim = coordinator.request_claim("publisher", ("src/",), now=NOW + 1)
    assert claim.fencing_token is not None

    with pytest.raises(ScopeAuthorityError, match="outside the claim"):
        coordinator.begin_publication(
            task_id="publisher",
            claim_id=claim.claim_id,
            fencing_token=claim.fencing_token,
            attempt=1,
            changed_paths=("docs/design.md",),
            patch_hash="c" * 64,
            result_tree_id="a" * 40,
            now=NOW + 2,
        )

    unchanged = store.get_claim(claim.claim_id)
    assert unchanged is not None
    assert unchanged.state is ClaimState.ACTIVE_WORK
    assert unchanged.lease_expires_at is not None


def test_publication_intent_retry_is_identical_and_reservation_does_not_expire(
    tmp_path: Path,
) -> None:
    store, coordinator = _coordinator(tmp_path)
    claim = coordinator.request_claim("publisher", ("src/",), now=NOW + 1)
    assert claim.fencing_token is not None
    first = coordinator.begin_publication(
        task_id="publisher",
        claim_id=claim.claim_id,
        fencing_token=claim.fencing_token,
        attempt=1,
        changed_paths=("src/z.py", "src/a.py"),
        patch_hash="c" * 64,
        result_tree_id="a" * 40,
        result_commit_id="d" * 40,
        now=NOW + 2,
    )
    retry = coordinator.begin_publication(
        task_id="publisher",
        claim_id=claim.claim_id,
        fencing_token=claim.fencing_token,
        attempt=1,
        changed_paths=("src/a.py", "src/z.py", "src/a.py"),
        patch_hash="c" * 64,
        result_tree_id="a" * 40,
        result_commit_id="d" * 40,
        now=NOW + 500_000,
    )

    assert retry == first
    assert first.changed_paths == ("src/a.py", "src/z.py")
    publishing = store.get_claim(claim.claim_id)
    assert publishing is not None
    assert publishing.state is ClaimState.PUBLISHING
    assert publishing.lease_expires_at is None

    store.create_task(
        repository_id="repo_test",
        task_id="waiter",
        now=NOW + 500_001,
    )
    waiter = coordinator.request_claim(
        "waiter",
        ("src/a.py",),
        now=NOW + 500_001,
    )
    assert waiter.state is ClaimState.QUEUED
    assert waiter.blocking_claim_ids == (claim.claim_id,)

    reconciled = coordinator.reconcile_expired(
        REPO_KEY,
        now=NOW + 1_000_000,
    )
    assert reconciled.expired_claim_ids == ()
    assert reconciled.activated == ()
    still_publishing = store.get_claim(claim.claim_id)
    still_waiting = store.get_claim(waiter.claim_id)
    assert still_publishing is not None
    assert still_publishing.state is ClaimState.PUBLISHING
    assert still_waiting is not None
    assert still_waiting.state is ClaimState.QUEUED

    with pytest.raises(ClaimConflict, match="reservations"):
        coordinator.release_claim(
            claim.claim_id,
            reason="ordinary task failure",
            now=NOW + 1_000_001,
        )
