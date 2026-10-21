"""Physical claim bindings keep independent legacy clones usable after cutover."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from llm_cli.coordination.models import ClaimAuthorityError, ClaimState
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.workspace.regime import CutoverManager


@dataclass
class Clones:
    service: DaemonService
    root_a: Path
    root_b: Path
    repository_id: str
    checkout_a: Any
    checkout_b: Any

    def task(self, name: str, *, session_id: str | None = None):
        return self.service.store.create_task(
            repository_id=self.repository_id,
            task_id=name,
            title="bound legacy fixture",
            session_id=session_id,
        )

    def convert_a(self) -> dict[str, Any]:
        manager = CutoverManager(self.service.store, self.service.physical_authority)
        plan = manager.preview(self.root_a)
        assert plan["ready"], plan["blockers"]
        result = manager.begin(self.root_a, plan["checkpoint"])
        assert result["regime"] == "workspace"
        assert self.service.physical_authority.regime(self.root_b)["regime"] == "legacy"
        return result


@pytest.fixture
def clones(tmp_path: Path, repository_factory: Any, service_factory: Any, git_run: Any):
    root_a = repository_factory(tmp_path, {"a.txt": "base\n"})
    root_b = tmp_path / "clone-b"
    git_run(root_a, "clone", "--no-hardlinks", str(root_a), str(root_b))
    service = service_factory(tmp_path)
    service.initialize()
    first = service.store.register_repository(
        repo_key="b" * 64,
        display_name="clone a",
        git_common_dir=str(root_a / ".git"),
        main_worktree_path=str(root_a),
        target_ref="refs/heads/main",
        path_case_insensitive=False,
    )
    checkout_a = service.store.ensure_checkout(
        repository=first,
        canonical_path=str(root_a),
        git_common_dir=str(root_a / ".git"),
    )
    second = service.store.register_repository(
        repo_key=first.repo_key,
        display_name="clone b",
        git_common_dir=str(root_b / ".git"),
        main_worktree_path=str(root_b),
        target_ref=first.target_ref,
        path_case_insensitive=False,
    )
    assert first.repository_id == second.repository_id
    checkout_b = service.store.ensure_checkout(
        repository=second,
        canonical_path=str(root_b),
        git_common_dir=str(root_b / ".git"),
    )
    try:
        yield Clones(
            service, root_a, root_b, first.repository_id, checkout_a, checkout_b
        )
    finally:
        service.close()


def test_converted_a_refuses_claims_while_verified_legacy_b_accepts_them(
    clones: Clones,
) -> None:
    env = clones
    env.convert_a()
    coordinator = env.service.coordinator
    task_b = env.task("legacy-b")
    claim_b = coordinator.request_claim(
        task_b.task_id, ("a.txt",), checkout_id=env.checkout_b.checkout_id
    )
    assert claim_b.state is ClaimState.ACTIVE_WORK
    with env.service.store.connection() as con:
        binding = con.execute(
            "SELECT * FROM legacy_claim_bindings WHERE claim_id=?", (claim_b.claim_id,)
        ).fetchone()
        assert (
            binding is not None and binding["checkout_id"] == env.checkout_b.checkout_id
        )
        assert binding["canonical_path"] == str(env.root_b)
        assert (
            binding["physical_key"]
            == env.service.physical_authority.regime(env.root_b)["physical_key"]
        )
    task_a = env.task("refused-a")
    with pytest.raises(LlmCoordError) as error:
        coordinator.request_claim(
            task_a.task_id, ("a.txt",), checkout_id=env.checkout_a.checkout_id
        )
    assert error.value.code is ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED
    assert env.service.store.get_task(task_a.task_id).current_claim_id is None


def test_existing_queued_b_activates_after_a_cutover_when_b_blocker_releases(
    clones: Clones,
) -> None:
    env = clones
    coordinator = env.service.coordinator
    blocker = env.task("legacy-b-blocker")
    waiter = env.task("legacy-b-waiter")
    first = coordinator.request_claim(
        blocker.task_id, ("a.txt",), checkout_id=env.checkout_b.checkout_id
    )
    queued = coordinator.request_claim(
        waiter.task_id, ("a.txt",), checkout_id=env.checkout_b.checkout_id
    )
    assert first.state is ClaimState.ACTIVE_WORK and queued.state is ClaimState.QUEUED
    env.convert_a()
    released = coordinator.release_claim(
        first.claim_id,
        reason="legacy-b-complete",
        expected_fencing_token=first.fencing_token,
    )
    assert released.released
    activated = env.service.store.get_claim(queued.claim_id)
    assert activated is not None and activated.state is ClaimState.ACTIVE_WORK
    assert activated.fencing_token is not None
    with env.service.store.connection() as con:
        assert (
            con.execute(
                "SELECT checkout_id FROM legacy_claim_bindings WHERE claim_id=?",
                (activated.claim_id,),
            ).fetchone()[0]
            == env.checkout_b.checkout_id
        )
    assert env.service.physical_authority.regime(env.root_a)["regime"] == "workspace"
    assert env.service.physical_authority.regime(env.root_b)["regime"] == "legacy"


def test_unbound_compatibility_client_remains_conservative_after_partial_cutover(
    clones: Clones,
) -> None:
    env = clones
    env.convert_a()
    task = env.task("unbound-client")
    with pytest.raises(LlmCoordError) as error:
        env.service.coordinator.request_claim(task.task_id, ("a.txt",))
    assert error.value.code is ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED
    assert env.service.store.get_task(task.task_id).current_claim_id is None
    with env.service.store.connection() as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM claims WHERE task_id=?", (task.task_id,)
            ).fetchone()[0]
            == 0
        )


def test_existing_claim_cannot_change_its_physical_checkout_binding(
    clones: Clones,
) -> None:
    env = clones
    task = env.task("immutable-b-binding")
    coordinator = env.service.coordinator
    claim = coordinator.request_claim(
        task.task_id, ("a.txt",), checkout_id=env.checkout_b.checkout_id
    )
    with pytest.raises(ClaimAuthorityError, match="physical checkout evidence changed"):
        coordinator.request_claim(
            task.task_id, ("a.txt",), checkout_id=env.checkout_a.checkout_id
        )
    with env.service.store.connection() as con:
        assert (
            con.execute(
                "SELECT checkout_id FROM legacy_claim_bindings WHERE claim_id=?",
                (claim.claim_id,),
            ).fetchone()[0]
            == env.checkout_b.checkout_id
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM claims WHERE task_id=?", (task.task_id,)
            ).fetchone()[0]
            == 1
        )


def test_session_checkout_cannot_be_rebound_by_a_trusted_request_argument(
    clones: Clones,
) -> None:
    env = clones
    store = env.service.store
    workspace = store.ensure_shared_workspace(env.checkout_a)
    session, _, _ = store.open_session(
        session_id="bound-a-session",
        checkout_id=env.checkout_a.checkout_id,
        resume_token_hash=hashlib.sha256(b"bound-a-session-secret").hexdigest(),
        provider="fixture",
        model="local",
        workspace_id=workspace.workspace_id,
    )
    with store.connection() as con:
        con.execute(
            "UPDATE sessions SET state='active' WHERE session_id=?",
            (session.session_id,),
        )
    task = env.task("session-a-mismatch", session_id=session.session_id)
    with pytest.raises(ClaimAuthorityError, match="session checkout binding changed"):
        env.service.coordinator.request_claim(
            task.task_id, ("a.txt",), checkout_id=env.checkout_b.checkout_id
        )
    with store.connection() as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM claims WHERE task_id=?", (task.task_id,)
            ).fetchone()[0]
            == 0
        )
