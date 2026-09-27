"""Regression checks for shared journals awaiting execution settlement."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.coordination.models import ClaimRecord, ExecutionRecord, RepositoryRecord
from llm_cli.daemon.service import DaemonService
from llm_cli.workspace.batches import BatchFile


@dataclass(frozen=True)
class PendingExecution:
    execution: ExecutionRecord
    claim: ClaimRecord
    session_id: str
    files: tuple[BatchFile, ...]
    checkpoint: dict[str, object]


@dataclass
class RecoveryEnvironment:
    service: DaemonService
    repository: RepositoryRecord
    checkout: Path

    def prepare(self, task_id: str, path: str) -> PendingExecution:
        store = self.service.store
        checkout = store.list_checkouts()[0]
        workspace = store.ensure_shared_workspace(checkout)
        session_id = f"session-{task_id}"
        store.open_session(
            session_id=session_id,
            checkout_id=checkout.checkout_id,
            resume_token_hash=hashlib.sha256(session_id.encode()).hexdigest(),
            provider="scripted",
            model=task_id,
            workspace_id=workspace.workspace_id,
        )
        with store.connection() as connection:
            connection.execute(
                "UPDATE sessions SET state = 'active' WHERE session_id = ?",
                (session_id,),
            )
        task = store.create_task(
            repository_id=self.repository.repository_id,
            session_id=session_id,
            task_id=task_id,
            title=f"update {path}",
        )
        claim = self.service.coordinator.request_claim(task_id, [path])
        execution = store.create_execution(
            task_id=task_id,
            attempt=task.attempt,
            claim_id=claim.claim_id,
            driver="coding_agent",
            worktree_path=str(self.checkout),
            base_oid="workspace:1:0",
            boot_id=self.service.boot_id,
            workspace_id=workspace.workspace_id,
        )
        broker = SharedToolBroker(
            worktree=self.checkout,
            scopes=(path,),
            case_insensitive_filesystem=False,
            publication_lock=self.service.workspace_batches.lock,
        )
        assert not broker.invoke("read_file", {"path": path}).is_error
        assert not broker.invoke(
            "write_file", {"path": path, "content": f"result for {task_id}\n"}
        ).is_error
        assert not broker.invoke(
            "finish_task", {"answer": "finished", "summary": "finished"}
        ).is_error
        checkpoint: dict[str, object] = {
            "version": 1,
            "provider": "scripted",
            "model": task_id,
            "session": {
                "messages": [{"role": "assistant", "content": f"finished {task_id}"}]
            },
            "deadline_at": 2_000_000_000.0,
            "phase": "finished",
            "turn": None,
            "tool_results": [],
            "in_flight_call": None,
            "tool_usage": broker.usage_snapshot(),
            "usage_total": {},
            "idle_turns": 0,
            "final_summary": "finished",
        }
        store.save_execution_checkpoint(
            execution_id=execution.execution_id,
            driver="coding_agent",
            checkpoint=checkpoint,
        )
        store.update_execution(execution.execution_id, state="validated")
        return PendingExecution(
            execution, claim, session_id, broker.candidates(), checkpoint
        )

    def publish(self, pending: PendingExecution) -> None:
        assert pending.claim.fencing_token is not None
        self.service.coordinator.reserve_shared_publication(
            pending.execution.execution_id,
            fencing_token=pending.claim.fencing_token,
            changed_paths=(item.relative_path for item in pending.files),
        )
        self.service.workspace_batches.publish(
            pending.execution.execution_id,
            pending.session_id,
            pending.files,
            pending.claim.scopes,
        )


@pytest.fixture
def recovery_env(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> RecoveryEnvironment:
    checkout = repository_factory(tmp_path, {"a.txt": "base a\n", "b.txt": "base b\n"})
    service = service_factory(tmp_path)
    service.initialize()
    repository = service.store.register_repository(
        repo_key="a" * 64,
        display_name="shared recovery",
        git_common_dir=str(checkout / ".git"),
        main_worktree_path=str(checkout),
        target_ref="refs/heads/main",
        path_case_insensitive=False,
    )
    service.store.ensure_checkout(
        repository=repository,
        canonical_path=str(checkout),
        git_common_dir=str(checkout / ".git"),
    )
    return RecoveryEnvironment(service, repository, checkout)


@pytest.mark.parametrize("diverged", [False, True])
def test_same_boot_terminal_batch_settles_and_recovery_is_idempotent(
    recovery_env: RecoveryEnvironment, diverged: bool
) -> None:
    env = recovery_env
    pending = env.prepare("interrupted-settlement", "a.txt")
    if diverged:
        (env.checkout / "a.txt").write_text("external edit\n")
    env.publish(pending)
    execution = env.service.store.get_execution(pending.execution.task_id, 1)
    assert execution is not None
    assert execution.boot_id == env.service.boot_id and execution.state == "publishing"

    # The daemon remains alive, but the worker stopped after the batch commit
    # and before execution/claim settlement (for example, a database error).
    (outcome,) = env.service._recover_shared_executions()
    assert outcome.resolution == ("failed_safe" if diverged else "confirmed")
    execution = env.service.store.get_execution(pending.execution.task_id, 1)
    assert execution is not None
    assert execution.state == ("failed" if diverged else "published")
    claim = env.service.store.get_claim(pending.claim.claim_id)
    assert claim is not None and claim.state == "released"
    session = env.service.store.get_session(pending.session_id)
    assert session is not None
    assert session.conversation_revision == (0 if diverged else 1)
    if not diverged:
        saved = env.service.store.session_conversation(pending.session_id)
        assert (
            saved is not None and saved[2]["session"] == pending.checkpoint["session"]
        )
    assert env.service._recover_shared_executions() == ()
    refreshed = env.service.store.get_session(pending.session_id)
    assert refreshed is not None
    assert refreshed.conversation_revision == session.conversation_revision


def test_terminal_claim_still_terminalizes_its_abandoned_execution(
    recovery_env: RecoveryEnvironment,
) -> None:
    env = recovery_env
    pending = env.prepare("cancelled-worker", "a.txt")
    env.service.coordinator.release_claim(
        pending.claim.claim_id, reason="operator_cancel"
    )
    with env.service.store.connection() as connection:
        connection.execute(
            "UPDATE task_executions SET boot_id = 'dead-boot' WHERE execution_id = ?",
            (pending.execution.execution_id,),
        )
    (outcome,) = env.service._recover_shared_executions()
    assert outcome.resolution == "failed_safe"
    execution = env.service.store.get_execution(pending.execution.task_id, 1)
    task = env.service.store.get_task(pending.execution.task_id)
    assert execution is not None and execution.state == "failed"
    assert task is not None and task.state == "cancelled"
    assert env.service._recover_shared_executions() == ()
    assert (env.checkout / "a.txt").read_text() == "base a\n"


def test_malformed_completion_does_not_prevent_other_execution_recovery(
    recovery_env: RecoveryEnvironment,
) -> None:
    env = recovery_env
    broken = env.prepare("broken-conversation", "a.txt")
    env.publish(broken)
    healthy = env.prepare("healthy-conversation", "b.txt")
    env.publish(healthy)
    env.service.store.save_execution_checkpoint(
        execution_id=broken.execution.execution_id,
        driver="coding_agent",
        checkpoint={**broken.checkpoint, "provider": "wrong-provider"},
    )
    outcomes = {
        outcome.task_id: outcome.resolution
        for outcome in env.service._recover_shared_executions()
    }
    assert outcomes == {
        "broken-conversation": "operator_attention",
        "healthy-conversation": "confirmed",
    }
    execution = env.service.store.get_execution(healthy.execution.task_id, 1)
    assert execution is not None and execution.state == "published"
    claim = env.service.store.get_claim(broken.claim.claim_id)
    assert claim is not None and claim.state == "publishing"

    env.service.store.save_execution_checkpoint(
        execution_id=broken.execution.execution_id,
        driver="coding_agent",
        checkpoint=broken.checkpoint,
    )
    (repaired,) = env.service._recover_shared_executions()
    assert repaired.task_id == broken.execution.task_id
    assert repaired.resolution == "confirmed"
    assert env.service._recover_shared_executions() == ()
