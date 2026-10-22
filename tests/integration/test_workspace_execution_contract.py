"""Claim-free execution uses real physical authority and durable workspace records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from llm_cli.agent.driver import DriverCapabilities, RunRequest, RunResult
from llm_cli.agent.tools import ToolBroker
from llm_cli.coordination.models import ClaimConflict, WorkspaceExecutionRecord
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.workspace.regime import CutoverManager


class WorkspaceDriver:
    name = "contract_driver"
    capabilities = DriverCapabilities(shared_workspace=True, resumable=True)

    def __init__(self, content: str | None = "workspace result\n") -> None:
        self.content = content
        self.requests: list[RunRequest] = []

    def run(self, request: RunRequest, tools: ToolBroker) -> RunResult:
        self.requests.append(request)
        assert not tools.invoke("read_file", {"path": "a.txt"}).is_error
        if self.content is not None:
            assert not tools.invoke(
                "write_file", {"path": "a.txt", "content": self.content}
            ).is_error
        assert not tools.invoke(
            "finish_task", {"answer": "complete answer", "summary": "done"}
        ).is_error
        assert request.checkpoint is not None
        request.checkpoint(
            {
                "version": 1,
                "provider": "scripted",
                "model": "workspace-contract",
                "session": {
                    "messages": [{"role": "assistant", "content": "complete answer"}]
                },
                "phase": "finished",
                "tool_usage": tools.usage_snapshot(),
                "usage_total": {"input_tokens": 3, "output_tokens": 2},
                "accepted_answer": {
                    "version": 1,
                    "answer_id": request.task_id + ":answer",
                    "text": "complete answer",
                    "summary": "done",
                    "outcome": "completed",
                },
            }
        )
        return RunResult(
            "done",
            tool_calls=tools.usage.calls,
            usage={"input_tokens": 3, "output_tokens": 2},
            answer="complete answer",
        )


@dataclass
class Environment:
    service: DaemonService
    root: Path
    repository: Any
    checkout: Any
    session: Any

    def prepare(
        self, task_id: str = "workspace-task"
    ) -> tuple[Any, WorkspaceExecutionRecord]:
        store = self.service.store
        task = store.create_task(
            repository_id=self.repository.repository_id,
            session_id=self.session.session_id,
            workspace_execution=True,
            title="edit a",
            task_id=task_id,
        )
        store.save_execution_launch(
            task_id=task_id,
            attempt=task.attempt,
            driver=WorkspaceDriver.name,
            instructions="edit a",
            interactive=False,
            parameters={},
        )
        regime = self.service.physical_authority.regime(self.root)
        workspace = store.get_workspace(self.session.workspace_id)
        assert workspace is not None
        execution = store.create_workspace_execution(
            task_id=task_id,
            attempt=task.attempt,
            session_id=self.session.session_id,
            workspace_id=workspace.workspace_id,
            scopes=("a.txt",),
            physical_key=regime["physical_key"],
            regime_generation=regime["generation"],
            driver=WorkspaceDriver.name,
            worktree_path=workspace.canonical_path,
            base_oid=f"workspace:{workspace.workspace_epoch}:{workspace.workspace_revision}",
            boot_id=self.service.boot_id,
        )
        return store.get_task(task_id), execution

    def run(
        self,
        task_id: str = "workspace-task",
        *,
        content: str | None = "workspace result\n",
    ) -> WorkspaceExecutionRecord:
        task, execution = self.prepare(task_id)
        return self.service.shared_runner.execute_workspace(
            repository=self.repository,
            task=task,
            execution=execution,
            driver=WorkspaceDriver(content),
            instructions="edit a",
        )

    def assert_no_claims(self) -> None:
        with self.service.store.connection() as con:
            assert con.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
            assert (
                con.execute("SELECT COUNT(*) FROM task_executions").fetchone()[0] == 0
            )
            assert (
                con.execute("SELECT COUNT(*) FROM execution_checkpoints").fetchone()[0]
                == 0
            )
        for task in self.service.store.list_tasks(self.repository.repo_key):
            assert task.current_claim_id is None
            assert task.current_fencing_token is None
            assert all(
                event.claim_id is None
                for event in self.service.store.list_task_events(task.task_id)
            )


@pytest.fixture
def workspace_env(
    tmp_path: Path, repository_factory: Any, service_factory: Any, request: Any
):
    root = repository_factory(tmp_path, {"a.txt": "base\n", ".gitignore": ".env\n"})
    service = service_factory(tmp_path)
    service.initialize()
    repository = service.store.register_repository(
        repo_key="a" * 64,
        display_name="workspace contract",
        git_common_dir=str(root / ".git"),
        main_worktree_path=str(root),
        target_ref="refs/heads/main",
        path_case_insensitive=False,
    )
    checkout = service.store.ensure_checkout(
        repository=repository,
        canonical_path=str(root),
        git_common_dir=str(root / ".git"),
    )
    manager = CutoverManager(service.store, service.physical_authority)
    mode = getattr(request, "param", "shared")
    if mode != "legacy":
        plan = manager.preview(root)
        manager.begin(root, checkpoint=plan["checkpoint"])
        assert service.physical_authority.regime(root)["regime"] == "workspace"
    else:
        mode = "shared"
    workspace = service.store.ensure_shared_workspace(checkout)
    if mode == "isolated":
        workspace, _ = service.isolated_workspaces.open(checkout, "workspace-session")
    session, _, _ = service.store.open_session(
        session_id="workspace-session",
        checkout_id=checkout.checkout_id,
        resume_token_hash=hashlib.sha256(b"workspace-session-secret").hexdigest(),
        provider="scripted",
        model="workspace-contract",
        workspace_id=workspace.workspace_id,
        workspace_mode=mode,
    )
    with service.store.connection() as con:
        con.execute(
            "UPDATE sessions SET state='active' WHERE session_id=?",
            (session.session_id,),
        )
    session = service.store.get_session(session.session_id)
    try:
        yield Environment(service, root, repository, checkout, session)
    finally:
        service.close()


@pytest.mark.parametrize("workspace_env", ["shared", "isolated"], indirect=True)
def test_workspace_publication_and_answers_never_allocate_claims(
    workspace_env: Environment,
) -> None:
    env = workspace_env
    execution = env.run()
    assert execution.state == "published" and execution.claim_id is None
    assert (env.root / "a.txt").read_text() == "workspace result\n"
    assert env.service.store.get_execution_by_id(execution.execution_id) == execution
    answer = env.service.store.get_execution_answer(execution.execution_id)
    assert answer is not None and answer.payload["answer"] == "complete answer"
    assert env.service.store.session_execution_usage(env.session.session_id) == (
        (execution.execution_id, "published", {"input_tokens": 3, "output_tokens": 2}),
    )
    env.assert_no_claims()


def test_workspace_read_only_answer_and_rewrite_share_durable_facade(
    workspace_env: Environment,
) -> None:
    env = workspace_env
    execution = env.run(content=None)
    assert execution.state == "published"
    assert env.service.workspace_batches.get(execution.execution_id) is None
    assert (
        env.service.store.latest_session_execution(env.session.session_id) == execution
    )
    _, promoted = env.service.store.save_answer_revision(
        execution.execution_id,
        text="rewritten complete answer",
        usage={"output_tokens": 7},
        session={"messages": []},
        replaces={"messages": [{"role": "assistant", "content": "complete answer"}]},
        context_tokens=10,
    )
    assert promoted and env.service.store.answer_revisions(execution.execution_id) == 1
    with pytest.raises(ValueError, match="already written"):
        env.service.store.save_answer_revision(
            execution.execution_id,
            text="again",
            usage={},
            session={},
            replaces={},
            context_tokens=None,
        )
    with env.service.store.connection() as con:
        assert (
            con.execute("SELECT COUNT(*) FROM workspace_execution_answers").fetchone()[
                0
            ]
            == 1
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM workspace_execution_answer_revisions"
            ).fetchone()[0]
            == 1
        )
    env.assert_no_claims()


@pytest.mark.parametrize("workspace_env", ["shared", "isolated"], indirect=True)
def test_workspace_held_proposal_apply_and_undo_are_claim_free(
    workspace_env: Environment,
) -> None:
    env = workspace_env
    env.service.store.set_session_mode(env.session.session_id, "normal")
    execution = env.run()
    assert execution.state == "failed" and execution.failure_code == "REVIEW_REQUIRED"
    assert (env.root / "a.txt").read_text() == "base\n"
    assert "workspace result" in env.service.workflow.inspect(execution.task_id)["diff"]
    private = None
    if env.session.workspace_mode == "isolated":
        workspace = env.service.store.get_workspace(env.session.workspace_id)
        private = Path(workspace.canonical_path)
        env.service.store.close_session(env.session.session_id, reason="test_close")
        env.service.isolated_workspaces.cleanup_closed()
        assert private.exists(), "held workspace proposal lost its private runtime"
    applied = env.service.workflow.apply(execution.task_id, allow_unverified=True)
    assert applied["state"] == "published"
    assert (env.root / "a.txt").read_text() == "workspace result\n"
    if private is not None:
        env.service.isolated_workspaces.cleanup_closed()
        assert not private.exists(), "settled closed workspace was not cleaned"
    undone = env.service.workflow.apply(execution.task_id, undo=True)
    assert undone["state"] == "published"
    assert (env.root / "a.txt").read_text() == "base\n"
    env.assert_no_claims()


def test_workspace_stale_generation_is_refused_before_driver_runs(
    workspace_env: Environment,
) -> None:
    env = workspace_env
    task, execution = env.prepare()
    driver = WorkspaceDriver()
    with pytest.raises(LlmCoordError) as error:
        env.service.shared_runner.execute_workspace(
            repository=env.repository,
            task=task,
            execution=replace(
                execution, regime_generation=execution.regime_generation + 1
            ),
            driver=driver,
            instructions="edit a",
        )
    assert error.value.code is ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED
    assert not driver.requests and (env.root / "a.txt").read_text() == "base\n"
    env.assert_no_claims()


def _prepare_finished(env: Environment, task_id: str):
    from llm_cli.agent.shared_tools import SharedToolBroker

    _task, execution = env.prepare(task_id)
    broker = SharedToolBroker(
        Path(execution.worktree_path),
        execution.scopes,
        publication_lock=env.service.workspace_batches.lock,
    )
    WorkspaceDriver().run(
        RunRequest(
            task_id=task_id,
            attempt=1,
            instructions="edit a",
            scopes=execution.scopes,
            worktree=Path(execution.worktree_path),
            base_oid=execution.base_oid,
            checkpoint=lambda state: (
                env.service.store.save_execution_checkpoint(
                    execution_id=execution.execution_id,
                    driver=execution.driver,
                    checkpoint=state,
                ).terminal_event_persisted
            ),
        ),
        broker,
    )
    env.service.store.update_execution(execution.execution_id, state="validated")
    return execution, broker


def test_workspace_recovery_adopts_private_checkpoint_without_a_claim(
    workspace_env: Environment,
) -> None:
    env = workspace_env
    execution, _ = _prepare_finished(env, "resume-workspace")
    with env.service.store.connection() as con:
        con.execute(
            "UPDATE workspace_executions SET boot_id='dead-boot' WHERE execution_id=?",
            (execution.execution_id,),
        )
    outcomes = env.service.shared_runner.recover(lambda _: WorkspaceDriver.capabilities)
    assert [(item.execution_id, item.resolution) for item in outcomes] == [
        (execution.execution_id, "resuming")
    ]
    adopted = env.service.store.get_execution_by_id(execution.execution_id)
    assert isinstance(adopted, WorkspaceExecutionRecord)
    assert adopted.boot_id == env.service.boot_id and adopted.state == "running"
    driver = WorkspaceDriver()
    result = env.service.shared_runner.execute_workspace(
        repository=env.repository,
        task=env.service.store.get_task(execution.task_id),
        execution=adopted,
        driver=driver,
        instructions="edit a",
    )
    assert result.state == "published" and driver.requests[0].resume_state is not None
    env.assert_no_claims()


def test_workspace_recovery_settles_exact_terminal_batch_once(
    workspace_env: Environment,
) -> None:
    env = workspace_env
    execution, broker = _prepare_finished(env, "recover-workspace")
    with env.service.workspace_batches.lock:
        env.service.coordinator.reserve_workspace_publication(
            execution.execution_id,
            regime_generation=execution.regime_generation,
            changed_paths=("a.txt",),
        )
        env.service.workspace_batches.publish(
            execution.execution_id,
            env.session.session_id,
            broker.candidates(),
            execution.scopes,
        )
    outcomes = env.service.shared_runner.recover(lambda _: WorkspaceDriver.capabilities)
    assert [(item.execution_id, item.resolution) for item in outcomes] == [
        (execution.execution_id, "confirmed")
    ]
    assert env.service.store.get_task(execution.task_id).state == "completed"
    assert (
        env.service.shared_runner.recover(lambda _: WorkspaceDriver.capabilities) == ()
    )
    env.assert_no_claims()


def test_workspace_cancelled_checkpoint_is_retained_before_release(
    workspace_env: Environment,
) -> None:
    env = workspace_env
    execution, _ = _prepare_finished(env, "cancel-workspace")
    env.service.workflow.ensure(execution.task_id)
    assert env.service.workflow.cancel(execution.task_id)["state"] == "stopping"
    with env.service.store.connection() as con:
        con.execute(
            "UPDATE workspace_executions SET boot_id='dead-boot' WHERE execution_id=?",
            (execution.execution_id,),
        )
    outcomes = env.service.shared_runner.recover(lambda _: WorkspaceDriver.capabilities)
    assert outcomes[0].resolution == "failed_safe"
    assert env.service.store.get_task(execution.task_id).state == "cancelled"
    assert "workspace result" in env.service.workflow.inspect(execution.task_id)["diff"]
    assert (env.root / "a.txt").read_text() == "base\n"
    env.assert_no_claims()


def test_workspace_preparing_launch_recovers_without_a_checkpoint(
    workspace_env: Environment,
) -> None:
    env = workspace_env
    task, execution = env.prepare("prepared-workspace")
    with env.service.store.connection() as con:
        con.execute(
            "UPDATE workspace_executions SET boot_id='dead-boot' WHERE execution_id=?",
            (execution.execution_id,),
        )
    outcomes = env.service.shared_runner.recover(lambda _: WorkspaceDriver.capabilities)
    assert outcomes[0].resolution == "resuming"
    adopted = env.service.store.get_execution_by_id(execution.execution_id)
    assert (
        isinstance(adopted, WorkspaceExecutionRecord) and adopted.state == "preparing"
    )
    driver = WorkspaceDriver()
    result = env.service.shared_runner.execute_workspace(
        repository=env.repository,
        task=env.service.store.get_task(task.task_id),
        execution=adopted,
        driver=driver,
        instructions="edit a",
    )
    assert result.state == "published" and driver.requests[0].resume_state is None
    env.assert_no_claims()


@pytest.mark.parametrize("workspace_env", ["legacy"], indirect=True)
def test_legacy_held_proposal_applies_after_cutover_without_new_claims(
    workspace_env: Environment, git_run: Any
) -> None:
    env = workspace_env
    store = env.service.store
    store.set_session_mode(env.session.session_id, "normal")
    task = store.create_task(
        repository_id=env.repository.repository_id,
        session_id=env.session.session_id,
        title="legacy edit",
        task_id="legacy-held",
    )
    store.save_execution_launch(
        task_id=task.task_id,
        attempt=1,
        driver=WorkspaceDriver.name,
        instructions="legacy edit",
        interactive=False,
        parameters={},
    )
    claim = env.service.coordinator.request_claim(task.task_id, ("a.txt",))
    result = env.service.shared_runner.execute(
        repository=env.repository,
        task=task,
        claim=claim,
        driver=WorkspaceDriver(),
        instructions="legacy edit",
    )
    assert result.failure_code == "REVIEW_REQUIRED"
    original_answer = store.get_execution_answer(result.execution_id)
    original_checkpoint = store.get_execution_checkpoint(result.execution_id)
    refs = git_run(env.root, "show-ref")
    manager = CutoverManager(store, env.service.physical_authority)
    manager.begin(env.root, checkpoint=manager.preview(env.root)["checkpoint"])
    assert (
        env.service.workflow.apply(task.task_id, allow_unverified=True)["state"]
        == "published"
    )
    assert env.service.workflow.apply(task.task_id, undo=True)["state"] == "published"
    assert (env.root / "a.txt").read_text() == "base\n"
    assert git_run(env.root, "show-ref") == refs
    assert store.get_execution_answer(result.execution_id) == original_answer
    assert store.get_execution_checkpoint(result.execution_id) == original_checkpoint
    with store.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM task_executions").fetchone()[0] == 1
        assert (
            con.execute("SELECT COUNT(*) FROM workspace_executions").fetchone()[0] == 2
        )
    assert store.get_execution_by_id(result.execution_id).claim_id == claim.claim_id


def test_workspace_unsettled_execution_blocks_mode_change_despite_task_attention(
    workspace_env: Environment,
) -> None:
    env = workspace_env
    task, execution = env.prepare("attention-workspace")
    with env.service.store.connection() as con:
        con.execute(
            "UPDATE tasks SET state='operator_attention' WHERE task_id=?",
            (task.task_id,),
        )
        con.execute(
            "UPDATE workspace_executions SET state='operator_attention' "
            "WHERE execution_id=?",
            (execution.execution_id,),
        )
    assert env.service.store.session_has_active_task(env.session.session_id)
    with pytest.raises(ClaimConflict, match="finish or stop"):
        env.service.store.set_session_mode(env.session.session_id, "normal")
    env.assert_no_claims()


@pytest.mark.parametrize("workspace_env", ["legacy"], indirect=True)
def test_retained_legacy_session_queue_does_not_block_new_workspace_task(
    workspace_env: Environment,
) -> None:
    from llm_cli.coordination.models import ClaimState

    env = workspace_env
    store = env.service.store
    with pytest.raises(LlmCoordError) as incompatible:
        store.create_task(
            repository_id=env.repository.repository_id,
            session_id=env.session.session_id,
            task_id="workspace-before-cutover",
            workspace_execution=True,
        )
    assert incompatible.value.code is ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED
    assert store.get_task("workspace-before-cutover") is None
    legacy_task = store.create_task(
        repository_id=env.repository.repository_id,
        session_id=env.session.session_id,
        task_id="retained-session-queue",
        title="legacy queued work",
    )
    claim = env.service.coordinator.request_claim(
        legacy_task.task_id, ("a.txt",), checkout_id=env.checkout.checkout_id
    )
    # Seed a valid parked queue with its original scopes and immutable checkout
    # binding; no running driver or execution owns this legacy task.
    with store.connection() as con:
        con.execute(
            "UPDATE claims SET state='queued',fencing_token=NULL,"
            "lease_expires_at=NULL,activated_at=NULL WHERE claim_id=?",
            (claim.claim_id,),
        )
        con.execute(
            "UPDATE tasks SET state='queued',coordination_state='queued',"
            "current_fencing_token=NULL WHERE task_id=?",
            (legacy_task.task_id,),
        )
        before = dict(
            con.execute(
                "SELECT * FROM claims WHERE claim_id=?", (claim.claim_id,)
            ).fetchone()
        )
    manager = CutoverManager(store, env.service.physical_authority)
    preview = manager.preview(env.root)
    assert preview["ready"]
    assert any(
        item["id"] == claim.claim_id and item["default"] == "retain_legacy"
        for item in preview["dispositions"]
    )
    manager.begin(env.root, preview["checkpoint"])
    execution = env.run("new-workspace-after-queue")
    assert execution.state == "published" and execution.claim_id is None
    assert store.get_task(execution.task_id).state == "completed"
    assert (env.root / "a.txt").read_text() == "workspace result\n"
    env.service.coordinator.reconcile_expired()
    retained = store.get_claim(claim.claim_id)
    assert retained is not None and retained.state is ClaimState.QUEUED
    assert retained.scopes == claim.scopes and retained.fencing_token is None
    assert store.get_task(legacy_task.task_id).current_claim_id == claim.claim_id
    with store.connection() as con:
        assert (
            dict(
                con.execute(
                    "SELECT * FROM claims WHERE claim_id=?", (claim.claim_id,)
                ).fetchone()
            )
            == before
        )
        assert con.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM task_executions").fetchone()[0] == 0
        assert (
            con.execute("SELECT COUNT(*) FROM workspace_executions").fetchone()[0] == 1
        )
    assert not store.session_has_active_task(env.session.session_id)
    mode = store.set_session_mode(env.session.session_id, "normal")
    assert mode.agent_mode == "normal"
    assert store.get_claim(claim.claim_id).state is ClaimState.QUEUED
