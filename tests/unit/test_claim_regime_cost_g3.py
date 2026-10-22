"""Legacy contender checks stay fresh and proportional to actual scheduling work."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_physical_authority import _owner

from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import ClaimState
from llm_cli.workspace.regime import CutoverManager


def _task(store: Any, checkout: Any, name: str) -> str:
    workspace = store.ensure_shared_workspace(checkout)
    session_id = "session-" + name
    _, _, sequence = store.open_session(
        session_id=session_id,
        checkout_id=checkout.checkout_id,
        resume_token_hash=hashlib.sha256(session_id.encode()).hexdigest(),
        provider="scripted",
        model="offline",
        workspace_id=workspace.workspace_id,
    )
    store.acknowledge_session(session_id, sequence=sequence)
    task = store.create_task(
        repository_id=checkout.repository_id,
        task_id=name,
        session_id=session_id,
        title=name,
    )
    store.save_execution_launch(
        task_id=task.task_id,
        attempt=1,
        driver="coding_agent",
        instructions="synthetic",
        interactive=False,
        parameters={},
    )
    return task.task_id


def test_six_optimistic_claims_observe_only_their_own_physical_guards(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = repository_factory(tmp_path, {"same.txt": "base\n"})
    authority = _owner(tmp_path, repository, "cost")
    store = authority.store
    coordinator = RepositoryCoordinator(store, launch_lease_ms=60_000)
    with store.connection() as connection:
        checkout_id = connection.execute(
            "SELECT checkout_id FROM checkouts"
        ).fetchone()[0]
    checkout = store.get_checkout(checkout_id)
    assert checkout is not None
    original = authority.regime
    inspections: list[str] = []

    def observed(path: Path) -> dict[str, Any]:
        inspections.append(str(path))
        return original(path)

    monkeypatch.setattr(authority, "regime", observed)
    counts = []
    claims = []
    try:
        for index in range(6):
            task_id = _task(store, checkout, f"optimistic-{index}")
            before = len(inspections)
            claims.append(
                coordinator.request_claim(
                    task_id,
                    ["same.txt"],
                    scheduling_mode="optimistic",
                    optimistic_driver="coding_agent",
                    checkout_id=checkout_id,
                )
            )
            counts.append(len(inspections) - before)
        assert counts == [2] * 6
        assert all(
            claim.state is ClaimState.ACTIVE_WORK and not claim.blocking_claim_ids
            for claim in claims
        )
        assert len({claim.fencing_token for claim in claims}) == 6
        inspections.clear()
        assert not coordinator.reconcile_expired().activated
        assert inspections == []
    finally:
        authority.close()


@pytest.mark.parametrize(
    "same_scope", [False, True], ids=["disjoint", "exclusive-conflict"]
)
def test_only_real_scope_contenders_need_additional_regime_checks(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    same_scope: bool,
) -> None:
    repository = repository_factory(tmp_path, {"same.txt": "base\n"})
    authority = _owner(tmp_path, repository, "cost")
    store = authority.store
    coordinator = RepositoryCoordinator(store, launch_lease_ms=60_000)
    with store.connection() as connection:
        checkout_id = connection.execute(
            "SELECT checkout_id FROM checkouts"
        ).fetchone()[0]
    checkout = store.get_checkout(checkout_id)
    assert checkout is not None
    try:
        first = coordinator.request_claim(
            _task(store, checkout, "first"), ["same.txt"], checkout_id=checkout_id
        )
        original = coordinator._legacy_claim_can_activate
        checked: list[str] = []

        def check(connection: Any, repo_key: str, claim: Any) -> bool:
            checked.append(str(claim["claim_id"]))
            return original(connection, repo_key, claim)

        monkeypatch.setattr(coordinator, "_legacy_claim_can_activate", check)
        second = coordinator.request_claim(
            _task(store, checkout, "second"),
            ["same.txt" if same_scope else "other.txt"],
            scheduling_mode="optimistic",
            optimistic_driver="coding_agent",
            checkout_id=checkout_id,
        )
        assert checked == ([first.claim_id] if same_scope else [])
        assert second.state is (
            ClaimState.QUEUED if same_scope else ClaimState.ACTIVE_WORK
        )
        assert second.blocking_claim_ids == ((first.claim_id,) if same_scope else ())
    finally:
        authority.close()


def test_real_conflicting_converted_claim_remains_fenced_and_cannot_block_legacy_clone(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
) -> None:
    root_a = repository_factory(tmp_path, {"same.txt": "base\n"})
    root_b = tmp_path / "clone-b"
    git_run(root_a, "clone", "--no-hardlinks", str(root_a), str(root_b))
    authority = _owner(tmp_path, root_a, "cost")
    store = authority.store
    coordinator = RepositoryCoordinator(store, launch_lease_ms=60_000)
    with store.connection() as connection:
        repository = connection.execute("SELECT * FROM repositories").fetchone()
        checkout_a_id = connection.execute(
            "SELECT checkout_id FROM checkouts"
        ).fetchone()[0]
    checkout_a = store.get_checkout(checkout_a_id)
    assert checkout_a is not None
    registered = store.register_repository(
        repo_key=repository["repo_key"],
        display_name="clone-b",
        git_common_dir=str(root_b / ".git"),
        main_worktree_path=str(root_b),
        target_ref=repository["target_ref"],
        profile_id="cost",
    )
    checkout_b = store.ensure_checkout(
        repository=registered,
        canonical_path=str(root_b),
        git_common_dir=str(root_b / ".git"),
    )
    try:
        blocker_b = coordinator.request_claim(
            _task(store, checkout_b, "blocker-b"),
            ["same.txt"],
            checkout_id=checkout_b.checkout_id,
        )
        retained_a = coordinator.request_claim(
            _task(store, checkout_a, "retained-a"),
            ["same.txt"],
            checkout_id=checkout_a_id,
        )
        assert retained_a.state is ClaimState.QUEUED
        manager = CutoverManager(store, authority)
        manager.begin(root_a, manager.preview(root_a)["checkpoint"])
        assert blocker_b.fencing_token is not None
        released = coordinator.release_claim(
            blocker_b.claim_id,
            reason="finished",
            expected_fencing_token=blocker_b.fencing_token,
        )
        assert released.released and not released.activated
        new_b = coordinator.request_claim(
            _task(store, checkout_b, "new-b"),
            ["same.txt"],
            checkout_id=checkout_b.checkout_id,
        )
        assert new_b.state is ClaimState.ACTIVE_WORK and not new_b.blocking_claim_ids
        retained = store.get_claim(retained_a.claim_id)
        assert retained is not None and retained.state is ClaimState.QUEUED
        assert authority.regime(root_a)["regime"] == "workspace"
        assert authority.regime(root_b)["regime"] == "legacy"
    finally:
        authority.close()
