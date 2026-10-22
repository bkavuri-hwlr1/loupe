"""Claim-free publication authority rejects stale or invented settlement proof."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from llm_cli.coordination.models import (
    ClaimAuthorityError,
    ClaimConflict,
    ScopeAuthorityError,
    WorkspaceExecutionRecord,
)
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import LlmCoordError


@pytest.fixture
def workspace_authority(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    request_factory: Callable[[str, dict[str, Any]], Any],
    git_run: Callable[..., str],
) -> Iterator[tuple[DaemonService, Path, WorkspaceExecutionRecord]]:
    root = repository_factory(tmp_path, {"src/value.py": "VALUE = 1\n"})
    service = service_factory(tmp_path)
    service.initialize()
    registered = asyncio.run(
        service.handle(request_factory("repo.add", {"path": str(root)}))
    )
    preview = service.cutovers.preview(root)
    with service.workspace_batches.lock:
        service.cutovers.begin(root, checkpoint=preview["checkpoint"])
    checkout = service.store.get_checkout(registered["checkout"]["checkout_id"])
    assert checkout is not None
    workspace = service.store.ensure_shared_workspace(checkout)
    session, _, sequence = service.store.open_session(
        session_id="publication-session",
        checkout_id=checkout.checkout_id,
        resume_token_hash="b" * 64,
        provider="scripted",
        model="fixture",
        workspace_id=workspace.workspace_id,
    )
    service.store.acknowledge_session(session.session_id, sequence=sequence)
    task = service.store.create_task(
        repository_id=checkout.repository_id,
        task_id="publication-task",
        session_id=session.session_id,
    )
    regime = service.physical_authority.regime(root)
    execution = service.store.create_workspace_execution(
        task_id=task.task_id,
        attempt=task.attempt,
        session_id=session.session_id,
        workspace_id=workspace.workspace_id,
        scopes=("src/",),
        physical_key=regime["physical_key"],
        regime_generation=regime["generation"],
        driver="scripted",
        worktree_path=workspace.canonical_path,
        base_oid=git_run(root, "rev-parse", "HEAD"),
        boot_id=service.boot_id,
    )
    service.workflow.ensure(task.task_id)
    service.store.update_execution(execution.execution_id, state="running")
    try:
        with service.physical_authority.pin(root):
            yield service, root, execution
    finally:
        service.close()


def test_workspace_reservation_rejects_scope_generation_and_stop(
    workspace_authority: tuple[DaemonService, Path, WorkspaceExecutionRecord],
) -> None:
    service, _, execution = workspace_authority
    reserve = service.coordinator.reserve_workspace_publication
    with pytest.raises(ScopeAuthorityError):
        reserve(
            execution.execution_id,
            regime_generation=execution.regime_generation,
            changed_paths=("outside.py",),
        )
    with pytest.raises(ClaimAuthorityError):
        reserve(
            execution.execution_id,
            regime_generation=execution.regime_generation + 1,
            changed_paths=("src/value.py",),
        )
    with service.store.connection() as connection:
        connection.execute(
            "UPDATE task_workflows SET stop_requested=1 WHERE task_id=?",
            (execution.task_id,),
        )
    with pytest.raises(ClaimAuthorityError):
        reserve(
            execution.execution_id,
            regime_generation=execution.regime_generation,
            changed_paths=("src/value.py",),
        )
    assert service.store.get_execution_by_id(execution.execution_id).state == "running"
    assert not any(
        e.event_type == "execution.publishing"
        for e in service.store.list_task_events(execution.task_id)
    )


def test_workspace_settlement_requires_real_batch_or_finished_checkpoint(
    workspace_authority: tuple[DaemonService, Path, WorkspaceExecutionRecord],
) -> None:
    service, _, execution = workspace_authority
    settle = service.coordinator.settle_workspace_execution
    for outcome in ("published", "diverged", "no_changes"):
        with pytest.raises(ClaimConflict):
            settle(execution.execution_id, outcome=outcome)
    service.store.save_execution_checkpoint(
        execution_id=execution.execution_id,
        driver=execution.driver,
        checkpoint={
            "phase": "finished",
            "provider": "scripted",
            "model": "fixture",
            "session": {},
            "tool_usage": {
                "shared_workspace_state": {
                    "files": [{"pending": True, "content": "edit"}]
                }
            },
        },
    )
    with pytest.raises(ClaimConflict):
        settle(execution.execution_id, outcome="no_changes")
    service.store.save_execution_checkpoint(
        execution_id=execution.execution_id,
        driver=execution.driver,
        checkpoint={
            "phase": "finished",
            "provider": "scripted",
            "model": "fixture",
            "session": {},
            "tool_usage": {"shared_workspace_state": {"files": []}},
        },
    )
    settle(execution.execution_id, outcome="no_changes")
    count = len(service.store.list_task_events(execution.task_id))
    settle(execution.execution_id, outcome="no_changes")
    assert len(service.store.list_task_events(execution.task_id)) == count
    task = service.store.get_task(execution.task_id)
    assert (
        task is not None and task.state == "completed" and task.current_claim_id is None
    )
    with service.store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM task_executions").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM workspace_executions").fetchone()[
                0
            ]
            == 1
        )
    assert all(
        e.claim_id is None for e in service.store.list_task_events(execution.task_id)
    )


def test_workspace_authority_cannot_settle_newer_attempt_or_regime(
    workspace_authority: tuple[DaemonService, Path, WorkspaceExecutionRecord],
) -> None:
    service, _, execution = workspace_authority
    with service.store.connection() as connection:
        connection.execute(
            "UPDATE workspace_executions SET regime_generation=regime_generation+1 "
            "WHERE execution_id=?",
            (execution.execution_id,),
        )
    with pytest.raises(LlmCoordError):
        service.coordinator.settle_workspace_execution(
            execution.execution_id, outcome="failed_safe"
        )
    with service.store.connection() as connection:
        connection.execute(
            "UPDATE workspace_executions SET regime_generation=? WHERE execution_id=?",
            (execution.regime_generation, execution.execution_id),
        )
        connection.execute(
            "UPDATE tasks SET attempt=attempt+1 WHERE task_id=?", (execution.task_id,)
        )
    with pytest.raises(ClaimAuthorityError):
        service.coordinator.settle_workspace_execution(
            execution.execution_id, outcome="failed_safe"
        )
    assert service.store.get_execution_by_id(execution.execution_id).state == "running"


def test_foreign_profile_without_local_cutover_rows_cannot_activate_old_waiter(
    workspace_authority: tuple[DaemonService, Path, WorkspaceExecutionRecord],
    tmp_path: Path,
) -> None:
    from llm_cli.coordination.coordinator import RepositoryCoordinator
    from llm_cli.coordination.models import ClaimState
    from llm_cli.errors import ErrorCode
    from llm_cli.storage.control import ControlStore
    from llm_cli.workspace.authority import PhysicalAuthority

    service, root, execution = workspace_authority
    source = service.store.get_task(execution.task_id)
    assert source is not None
    original = service.store.get_repository_by_key(source.repo_key)
    assert original is not None
    peer = ControlStore(tmp_path / "stale-peer.sqlite3")
    peer.initialize()
    repository = peer.register_repository(
        repo_key=source.repo_key,
        display_name="peer",
        git_common_dir=original.git_common_dir,
        main_worktree_path=str(root),
        target_ref=original.target_ref,
    )
    peer.ensure_checkout(
        repository=repository,
        canonical_path=str(root),
        git_common_dir=original.git_common_dir,
    )
    for task_id in ("old-holder", "old-waiter", "new-request"):
        peer.create_task(repository_id=repository.repository_id, task_id=task_id)
    coordinator = RepositoryCoordinator(peer)
    # Model the durable queue left by a pre-cutover peer database, before it
    # joins the upgraded physical regime protocol. No worker or files are used.
    holder = coordinator.request_claim("old-holder", ("src/",))
    waiter = coordinator.request_claim("old-waiter", ("src/",))
    assert waiter.state is ClaimState.QUEUED
    with peer.connection() as connection:
        connection.execute(
            "UPDATE claims SET state='cancelled',lease_expires_at=NULL,"
            "terminal_expires_at=9999999999999 "
            "WHERE claim_id=?",
            (holder.claim_id,),
        )
        assert (
            connection.execute("SELECT count(*) FROM checkout_regimes").fetchone()[0]
            == 0
        )
    authority = PhysicalAuthority(
        peer,
        "peer",
        "peer-boot",
        tmp_path / "peer.sock",
        root=service.physical_authority.root,
    )
    peer.authority = authority
    try:
        before = service.physical_authority.regime(root)
        reconciled = coordinator.reconcile_expired()
        assert reconciled.activated == ()
        assert peer.get_claim(waiter.claim_id).state is ClaimState.QUEUED
        with pytest.raises(LlmCoordError) as refused:
            coordinator.request_claim("new-request", ("src/",))
        assert refused.value.code is ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED
        assert len(peer.list_claims(source.repo_key)) == 2
        assert service.physical_authority.regime(root) == before
        with peer.connection() as connection:
            assert (
                connection.execute("SELECT count(*) FROM checkout_regimes").fetchone()[
                    0
                ]
                == 0
            )
    finally:
        authority.close()


def test_workspace_settlement_rejects_another_sessions_batch(
    workspace_authority: tuple[DaemonService, Path, WorkspaceExecutionRecord],
) -> None:
    service, _, execution = workspace_authority
    session = service.store.get_session(execution.session_id)
    assert session is not None
    service.store.open_session(
        session_id="different-session",
        checkout_id=session.checkout_id,
        resume_token_hash="c" * 64,
        provider="scripted",
        model="fixture",
        workspace_id=execution.workspace_id,
    )
    with service.store.connection() as connection:
        checkout = service.store.get_checkout(session.checkout_id)
        assert checkout is not None
        connection.execute(
            "INSERT INTO workspace_batches("
            "batch_id,session_id,workspace_id,checkout_id,"
            "canonical_path,git_common_dir,request_json,state,workspace_revision,"
            "checkout_event_sequence,created_at,updated_at) VALUES (?,?,?,?,?,?,'{}',"
            "'published',1,1,1,1)",
            (
                execution.execution_id,
                "different-session",
                execution.workspace_id,
                session.checkout_id,
                checkout.canonical_path,
                checkout.git_common_dir,
            ),
        )
    with pytest.raises(ClaimConflict, match="another execution binding"):
        service.coordinator.settle_workspace_execution(
            execution.execution_id, outcome="published"
        )
    assert service.store.get_execution_by_id(execution.execution_id).state == "running"
