"""Session execution against private candidates and a shared checkout journal."""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from llm_cli.agent.driver import (
    AgentDriver,
    CoordinationUpdate,
    DriverCapabilities,
    FinalizationRequest,
    RunRequest,
    RunResult,
    SettlementFinalizer,
)
from llm_cli.agent.finalization import (
    SettlementFacts,
    checkpoint_finalization,
    settlement_surprises,
)
from llm_cli.agent.hooks import HookRunner
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import TaskCancelled
from llm_cli.config.models import HookConfig, McpServerConfig
from llm_cli.coordination.models import (
    ClaimConflict,
    ClaimRecord,
    ClaimState,
    CoordinationError,
    ExecutionLaunchRecord,
    ExecutionRecord,
    RepositoryRecord,
    TaskRecord,
)
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.execution.checks import (
    CheckRunner,
    verification_current,
    verification_status,
)
from llm_cli.execution.commands import (
    DEPENDENCY_PATHS,
    CommandRunner,
    CommandSettings,
)
from llm_cli.execution.recovery import RecoveryOutcome
from llm_cli.execution.runner import TaskExecutionRunner
from llm_cli.execution.sandbox import available_sandbox
from llm_cli.mcp.tools import McpToolset
from llm_cli.storage.connection import immediate_transaction
from llm_cli.workspace.batches import SharedBatchPublisher
from llm_cli.workspace.context import SharedCoordinationContext
from llm_cli.workspace.workflow import TaskWorkflow, decode_files


class SharedTaskExecutionRunner:
    """Reuse the supervised driver loop without writing an isolated task ref.

    A task keeps fenced, scope-bounded authority while optimistic claims allow
    overlapping preparation. Every edit remains private in its checkpoint.
    Only the independently validated batch publisher touches the checkout.
    """

    def __init__(
        self, lifecycle: TaskExecutionRunner, publisher: SharedBatchPublisher
    ) -> None:
        self.lifecycle = lifecycle
        self.store = lifecycle.store
        self.coordinator = lifecycle.coordinator
        self.publisher = publisher
        self.workflow = TaskWorkflow(self.store, self.coordinator, publisher)
        self.is_shutting_down: Callable[[], bool] = lambda: False
        # Set by the daemon. Without it, or without a working OS sandbox,
        # tasks are never offered model-chosen commands.
        self.commands: CommandSettings | None = None
        # Configured MCP servers, started for each task outside plan mode.
        self.mcp_servers: tuple[McpServerConfig, ...] = ()
        # User hooks, run in a sandbox around each task's tool calls.
        self.hooks: tuple[HookConfig, ...] = ()

    def ensure_available(self, workspace_id: str) -> None:
        if self.publisher.blocks_workspace(workspace_id):
            raise LlmCoordError(
                ErrorCode.CHECKOUT_RECOVERY_REQUIRED,
                "a shared batch needs recovery before reading or publishing",
            )
        with self.store.connection() as connection:
            pending = connection.execute(
                """SELECT 1 FROM workspace_publications WHERE workspace_id = ?
                    AND operation_state IN ('prepared', 'operator_attention')
                    LIMIT 1""",
                (workspace_id,),
            ).fetchone()
        if pending is not None:
            raise LlmCoordError(
                ErrorCode.CHECKOUT_RECOVERY_REQUIRED,
                "a single-file publication needs recovery before continuing",
            )

    def execute(
        self,
        *,
        repository: RepositoryRecord,
        task: TaskRecord,
        claim: ClaimRecord,
        driver: AgentDriver,
        instructions: str,
        conversation_state: Mapping[str, object] | None = None,
        coordination_context: str | None = None,
        asker: Callable[[str], str] | None = None,
    ) -> ExecutionRecord:
        if not (
            driver.capabilities.shared_workspace
            and driver.capabilities.enforces_scope
            and driver.capabilities.resumable
        ):
            raise ClaimConflict("driver cannot prepare recoverable shared candidates")
        session = self.store.get_session(task.session_id or "")
        workspace = (
            self.store.get_workspace(session.workspace_id or "") if session else None
        )
        if (
            session is None
            or workspace is None
            or session.state not in {"active", "disconnected"}
            or session.workspace_mode != "shared"
            or workspace.kind != "shared_checkout"
            or workspace.mode != "shared"
            or workspace.checkout_id != session.checkout_id
            or workspace.state != "active"
            or workspace.canonical_path != repository.main_worktree_path
            or (
                claim.scheduling_mode == "optimistic"
                and claim.workspace_id != workspace.workspace_id
            )
        ):
            raise ClaimConflict("task no longer names its active shared checkout")
        if claim.state is not ClaimState.ACTIVE_WORK or claim.fencing_token is None:
            raise ClaimConflict("shared execution requires an active fenced claim")
        if claim.task_id != task.task_id or claim.task_attempt != task.attempt:
            raise ClaimConflict("claim does not belong to this task attempt")
        checkout = self.store.get_checkout(session.checkout_id)
        if (
            checkout is None
            or checkout.repository_id != repository.repository_id
            or checkout.canonical_path != workspace.canonical_path
        ):
            raise ClaimConflict("shared execution checkout binding changed")
        existing = self.store.get_execution(task.task_id, task.attempt)
        if existing is not None and (
            existing.workspace_id != workspace.workspace_id
            or existing.state != "running"
            or existing.driver != driver.name
        ):
            raise ClaimConflict("shared execution requires recovery before continuing")
        execution = self.store.create_execution(
            task_id=task.task_id,
            attempt=task.attempt,
            claim_id=claim.claim_id,
            driver=driver.name,
            worktree_path=workspace.canonical_path,
            base_oid=(
                existing.base_oid
                if existing
                else (
                    f"workspace:{workspace.workspace_epoch}:"
                    f"{workspace.workspace_revision}"
                )
            ),
            boot_id=self.lifecycle.boot_id,
            workspace_id=workspace.workspace_id,
        )
        row = self.workflow.ensure(task.task_id)
        self.workflow.update(
            task.task_id, task.attempt, execution_id=execution.execution_id
        )
        broker: SharedToolBroker | None = None
        try:
            self.coordinator.renew_claim(
                task_id=task.task_id,
                claim_id=claim.claim_id,
                fencing_token=claim.fencing_token,
                attempt=task.attempt,
            )
            if existing is None:
                execution = self.store.update_execution(
                    execution.execution_id, state="running"
                )
            saved = self.store.get_execution_checkpoint(execution.execution_id)
            if existing is not None and saved is None:
                raise ClaimConflict("shared execution has no durable checkpoint")
            broker = SharedToolBroker(
                worktree=Path(workspace.canonical_path),
                scopes=claim.scopes,
                case_insensitive_filesystem=repository.path_case_insensitive,
                limits=self.lifecycle.limits,
                asker=asker,
                on_event=self.lifecycle._event_recorder(task, claim),
                publication_lock=self.publisher.lock,
                agent_mode=row["agent_mode"],
                web=self.lifecycle.web_access(),
                guard=functools.partial(self.ensure_available, workspace.workspace_id),
                cancelled=lambda: (
                    self.workflow.stopped(task.task_id, task.attempt)
                    or self.is_shutting_down()
                ),
            )
            deadline_value = saved.checkpoint.get("deadline_at") if saved else None
            deadline_at = (
                float(deadline_value)
                if isinstance(deadline_value, (float, int))
                else time.time() + self.lifecycle.limits.wall_clock_seconds
            )
            checker = CheckRunner(
                self.workflow,
                row,
                broker.worktree,
                self.lifecycle.managed_worktree_root.parent / "check-worktrees",
                broker.candidates,
                lambda: (
                    self.workflow.stopped(task.task_id, task.attempt)
                    or self.is_shutting_down()
                ),
                broker.emit,
                self.publisher.lock,
                deadline_at=deadline_at,
                on_result=broker.record_check,
            )
            if checker.config.get("checks") and row["agent_mode"] != "plan":
                broker.check_runner = checker.run
                broker.finish_gate = lambda: (
                    checker.gate() if broker and broker.candidates() else None
                )
                instructions += "\nConfigured checks: " + ", ".join(
                    checker.config["checks"]
                )
            sandbox = (
                available_sandbox()
                if self.commands is not None
                and self.commands.approval != "off"
                and row["agent_mode"] != "plan"
                else None
            )
            if sandbox is not None:
                assert self.commands is not None
                broker.command_runner = CommandRunner(
                    root=broker.worktree,
                    snapshot_root=self.commands.snapshot_root,
                    candidates=broker.candidates,
                    cancelled=lambda: (
                        self.workflow.stopped(task.task_id, task.attempt)
                        or self.is_shutting_down()
                    ),
                    emit=broker.emit,
                    lock=self.publisher.lock,
                    sandbox=sandbox,
                    protected=self.commands.protected,
                    dependency_paths=(
                        *DEPENDENCY_PATHS,
                        *checker.config.get("runtime_paths", []),
                    ),
                    deadline_at=deadline_at,
                ).run
                broker.command_approval = self.commands.approval
            if self.hooks and self.commands is not None:
                hook_sandbox = available_sandbox()
                if hook_sandbox is None:
                    broker.emit(
                        "hooks.unavailable",
                        {"reason": "no working operating-system sandbox"},
                    )
                else:
                    broker.hooks = HookRunner(
                        self.hooks,
                        CommandRunner(
                            root=broker.worktree,
                            snapshot_root=self.commands.snapshot_root,
                            candidates=broker.candidates,
                            cancelled=lambda: (
                                self.workflow.stopped(task.task_id, task.attempt)
                                or self.is_shutting_down()
                            ),
                            emit=broker.emit,
                            lock=self.publisher.lock,
                            sandbox=hook_sandbox,
                            protected=self.commands.protected,
                            dependency_paths=(
                                *DEPENDENCY_PATHS,
                                *checker.config.get("runtime_paths", []),
                            ),
                            deadline_at=deadline_at,
                        ).run_hook,
                        broker.emit,
                    )
            mcp = (
                McpToolset(
                    self.mcp_servers, interactive=asker is not None, emit=broker.emit
                )
                if self.mcp_servers and row["agent_mode"] != "plan"
                else None
            )
            broker.mcp = mcp
            context = SharedCoordinationContext(
                self.store, session.session_id, claim.scopes
            )

            def refresh_coordination(after_sequence: int | None) -> CoordinationUpdate:
                with self.publisher.lock:
                    self.ensure_available(workspace.workspace_id)
                    return context(after_sequence)

            request = RunRequest(
                task_id=task.task_id,
                attempt=task.attempt,
                instructions=instructions,
                scopes=claim.scopes,
                worktree=broker.worktree,
                base_oid=execution.base_oid,
                conversation_state=conversation_state,
                coordination_context=coordination_context,
                resume_state=saved.checkpoint if saved else None,
                checkpoint=functools.partial(
                    self.lifecycle._checkpoint_execution,
                    execution_id=execution.execution_id,
                    driver=driver.name,
                ),
                workspace_mode="shared",
                agent_mode=row["agent_mode"],
                refresh_coordination=refresh_coordination,
                # An edit task's answer stays a draft until settlement, so
                # it can describe what was actually published or held.
                publication_aware_finalization=isinstance(
                    driver, SettlementFinalizer
                ),
            )
            # Starting servers can take longer than a lease, so it is renewed.
            with self.lifecycle._renewing_lease(task=task, claim=claim):
                try:
                    if mcp is not None:
                        mcp.start()
                    run = driver.run(request, broker)
                finally:
                    if mcp is not None:
                        mcp.close()
            self.store.update_execution(
                execution.execution_id,
                state="validated",
                summary=run.summary,
                tool_calls=run.tool_calls,
                usage=run.usage,
            )
            files = broker.candidates()
            if row["agent_mode"] == "plan" and files:
                raise ValueError("plan tasks cannot publish file changes")
            self.workflow.retain(
                task.task_id, task.attempt, execution.execution_id, files
            )
            with self.publisher.lock:
                self.ensure_available(workspace.workspace_id)
                stopped = self.workflow.stopped(task.task_id, task.attempt)
                incomplete = run.outcome != "completed"
                if incomplete and not files and not stopped:
                    self.coordinator.settle_shared_execution(
                        execution.execution_id,
                        outcome="failed_safe",
                        failure_code=(
                            "MODEL_BLOCKED"
                            if run.outcome == "blocked"
                            else "RESPONSE_INCOMPLETE"
                        ),
                    )
                    self.workflow.update(
                        task.task_id,
                        task.attempt,
                        status="failed",
                        expires_at=self.store._now(None)
                        + self.coordinator.terminal_retention_ms,
                    )
                    result = self.store.get_execution(task.task_id, task.attempt)
                    assert result is not None
                    return result
                hold = stopped or incomplete or bool(
                    files
                    and (
                        row["publish_mode"] == "review"
                        or not verification_current(
                            self.store, row, broker.worktree, files
                        )
                    )
                )
                verification = verification_status(
                    self.store, row, broker.worktree, files
                )
                outcome = "held_for_review"
                if not hold:
                    broker.emit("workflow.verification", {"status": verification})
                    self.coordinator.reserve_shared_publication(
                        execution.execution_id,
                        fencing_token=claim.fencing_token,
                        changed_paths=(file.relative_path for file in files),
                    )
                    if not files:
                        outcome = "no_changes"
                    else:
                        publication = self.publisher.publish(
                            execution.execution_id,
                            session.session_id,
                            files,
                            claim.scopes,
                            case_insensitive_filesystem=(
                                repository.path_case_insensitive
                            ),
                        )
                        outcome = publication.state
            # The files are settled. Finish the response before the task
            # becomes terminal, outside the lock: a model call must never
            # block other sessions' reads.
            self._finalize_response(
                driver=driver,
                task=task,
                claim=claim,
                execution=execution,
                broker=broker,
                instructions=instructions,
                run=run,
                publish_mode=row["publish_mode"],
                facts=SettlementFacts(
                    publication=outcome,
                    verification=verification,
                    completion="cancelled" if stopped else run.outcome,
                    changed_paths=tuple(
                        sorted(file.relative_path for file in files)
                    ),
                ),
            )
            with self.publisher.lock:
                if hold:
                    self.workflow.hold(
                        task.task_id,
                        task.attempt,
                        execution.execution_id,
                        cancelled=stopped,
                        completion_outcome=(
                            "cancelled" if stopped else run.outcome
                        ),
                        verification=verification,
                        file_count=len(files),
                    )
                    result = self.store.get_execution(task.task_id, task.attempt)
                    assert result is not None
                    return result
                if outcome == "operator_attention":
                    raise LlmCoordError(
                        ErrorCode.CHECKOUT_RECOVERY_REQUIRED,
                        "the retained shared batch needs recovery",
                    )
                self.coordinator.settle_shared_execution(
                    execution.execution_id, outcome=outcome
                )
            self.workflow.update(
                task.task_id,
                task.attempt,
                status="published"
                if outcome in {"published", "no_changes"}
                else "awaiting_review",
                publication_id=execution.execution_id
                if outcome == "published"
                else None,
                expires_at=self.store._now(None)
                + self.coordinator.terminal_retention_ms
                if outcome in {"published", "no_changes"}
                else None,
            )
            if outcome == "diverged":
                raise LlmCoordError(
                    ErrorCode.PATH_BASE_MISMATCH,
                    "shared files changed since they were read; the entire candidate "
                    "batch was preserved and no files were published",
                    details={"batch_id": execution.execution_id},
                )
        except Exception as exc:
            if (
                broker is not None
                and self.publisher.get(execution.execution_id) is None
            ):
                files = broker.candidates()
                if files or isinstance(exc, TaskCancelled):
                    self.workflow.retain(
                        task.task_id, task.attempt, execution.execution_id, files
                    )
                    verification = verification_status(
                        self.store, row, broker.worktree, files
                    )
                    self.workflow.hold(
                        task.task_id,
                        task.attempt,
                        execution.execution_id,
                        cancelled=self.workflow.stopped(task.task_id, task.attempt),
                        completion_outcome=(
                            "cancelled"
                            if isinstance(exc, TaskCancelled)
                            else "failed"
                        ),
                        verification=verification,
                        file_count=len(files),
                    )
                    if isinstance(exc, TaskCancelled):
                        result = self.store.get_execution(task.task_id, task.attempt)
                        assert result is not None
                        return result
            fresh = self.store.get_execution(task.task_id, task.attempt)
            if fresh is not None and fresh.state not in {"published", "failed"}:
                batch = self.publisher.get(execution.execution_id)
                if batch is not None and batch.state in {"published", "diverged"}:
                    self.coordinator.settle_shared_execution(
                        execution.execution_id, outcome=batch.state
                    )
                elif batch is None:
                    code = (
                        exc.code.value
                        if isinstance(exc, LlmCoordError)
                        else "EXECUTION_FAILED"
                    )
                    self.coordinator.settle_shared_execution(
                        execution.execution_id, outcome="failed_safe", failure_code=code
                    )
                else:
                    self.store.update_execution(
                        execution.execution_id,
                        state="operator_attention",
                        failure_code=ErrorCode.CHECKOUT_RECOVERY_REQUIRED.value,
                    )
            raise
        result = self.store.get_execution(task.task_id, task.attempt)
        assert result is not None
        return result

    def _finalize_response(
        self,
        *,
        driver: AgentDriver,
        task: TaskRecord,
        claim: ClaimRecord,
        execution: ExecutionRecord,
        broker: SharedToolBroker,
        instructions: str,
        run: RunResult,
        publish_mode: str,
        facts: SettlementFacts,
    ) -> None:
        """Turn a held draft into the accepted answer, from settlement facts.

        Only a task whose harness held its answer as a draft has anything to
        do. The draft stands when settlement is what the agent expected;
        otherwise one tools-disabled model turn rewrites it. Facts bound by
        an earlier attempt stay authoritative, so a restart cannot change
        what the answer may claim.
        """

        if not isinstance(driver, SettlementFinalizer):
            return
        saved = self.store.get_execution_checkpoint(execution.execution_id)
        state = checkpoint_finalization(saved.checkpoint) if saved else None
        if saved is None or state is None or state.status in {
            "completed",
            "interrupted",
        }:
            return
        bound = state.facts or facts
        request = FinalizationRequest(
            task_id=task.task_id,
            attempt=task.attempt,
            instructions=instructions,
            draft=run.answer,
            summary=run.summary,
            facts=bound,
            resume_state=saved.checkpoint,
            checkpoint=functools.partial(
                self.lifecycle._checkpoint_execution,
                execution_id=execution.execution_id,
                driver=driver.name,
            ),
            use_model=settlement_surprises(bound, publish_mode=publish_mode),
            on_event=broker.emit,
            cancelled=self.is_shutting_down,
        )
        try:
            if state.status == "prepared":
                request = replace(
                    request, resume_state=driver.prepare_finalization(request)
                )
            with self.lifecycle._renewing_lease(task=task, claim=claim):
                driver.finalize(request)
        except Exception as exc:
            # Settlement must still complete; the task keeps its files and
            # states that its answer is missing rather than inventing one.
            broker.emit(
                "model.finalization_failed", {"error": type(exc).__name__}
            )

    def recover(
        self,
        capabilities: Callable[[str], DriverCapabilities | None],
        *,
        can_launch: Callable[[ExecutionLaunchRecord], bool] | None = None,
    ) -> tuple[RecoveryOutcome, ...]:
        """Recover journaled effects first, then adopt private checkpoints.

        The caller holds the publication lock and has recovered filesystem
        journals before this method. A shared checkout is never a cleanup path.
        """

        with self.store.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM task_executions e WHERE workspace_id IS NOT NULL
                    AND (state = 'operator_attention' OR (
                        state IN ('preparing','running','validated','publishing')
                        AND (boot_id IS NOT ? OR EXISTS (
                            SELECT 1 FROM workspace_batches b
                            WHERE b.batch_id = e.execution_id
                            AND b.state IN ('published','diverged')))))
                    ORDER BY created_at, execution_id""",
                (self.lifecycle.boot_id,),
            ).fetchall()
        outcomes: list[RecoveryOutcome] = []
        for row in rows:
            execution = self.store.execution_from_row(row)
            resolution = "operator_attention"
            try:
                batch = self.publisher.get(execution.execution_id)
                if batch is not None and batch.state in {"published", "diverged"}:
                    self.coordinator.settle_shared_execution(
                        execution.execution_id, outcome=batch.state
                    )
                    resolution = (
                        "confirmed" if batch.state == "published" else "failed_safe"
                    )
                elif batch is not None:
                    self.store.update_execution(
                        execution.execution_id,
                        state="operator_attention",
                        failure_code=ErrorCode.CHECKOUT_RECOVERY_REQUIRED.value,
                    )
                elif (
                    workflow := self.workflow.get(
                        execution.task_id, execution.task_attempt
                    )
                ) and (
                    workflow["status"] in {"awaiting_review", "cancelled"}
                    or workflow["stop_requested"]
                ):
                    if workflow["proposal_json"] is None:
                        self._retain_checkpoint_candidates(execution)
                    retained = self.workflow.get(
                        execution.task_id, execution.task_attempt
                    )
                    encoded = retained["proposal_json"] if retained else None
                    files = decode_files(encoded) if isinstance(encoded, str) else ()
                    verification = verification_status(
                        self.store,
                        retained or workflow,
                        Path(execution.worktree_path),
                        files,
                    )
                    self.workflow.hold(
                        execution.task_id,
                        execution.task_attempt,
                        execution.execution_id,
                        cancelled=bool(workflow["stop_requested"]),
                        completion_outcome=(
                            "cancelled"
                            if workflow["stop_requested"]
                            else "completed"
                        ),
                        verification=verification,
                        file_count=len(files),
                    )
                    resolution = "failed_safe"
                elif self._can_resume(execution, capabilities, can_launch):
                    self._adopt(execution)
                    resolution = "resuming"
                else:
                    self.coordinator.settle_shared_execution(
                        execution.execution_id, outcome="failed_safe"
                    )
                    resolution = "failed_safe"
            except (ValueError, LlmCoordError, CoordinationError):
                # An unreadable/malformed execution is local operator work;
                # it must not prevent recovery of the other checkout tasks.
                self.store.update_execution(
                    execution.execution_id,
                    state="operator_attention",
                    failure_code=ErrorCode.CHECKOUT_RECOVERY_REQUIRED.value,
                )
            outcomes.append(
                RecoveryOutcome(
                    task_id=execution.task_id,
                    task_attempt=execution.task_attempt,
                    resolution=resolution,
                    detail=f"shared workspace execution {resolution}",
                    execution_id=execution.execution_id,
                )
            )
        return tuple(outcomes)

    def _retain_checkpoint_candidates(self, execution: ExecutionRecord) -> None:
        # Cancellation may be persisted while a provider call is in flight.
        # Preserve its last durable overlay before terminalizing the attempt,
        # since settled workflows no longer expose their running checkpoint.
        saved = self.store.get_execution_checkpoint(execution.execution_id)
        if saved is None:
            return
        claim = self.store.get_claim(execution.claim_id)
        task = self.store.get_task(execution.task_id)
        repository = self.store.get_repository(task.repository_id) if task else None
        usage = saved.checkpoint.get("tool_usage")
        if (
            saved.driver != execution.driver
            or claim is None
            or repository is None
            or claim.task_id != execution.task_id
            or claim.task_attempt != execution.task_attempt
            or not isinstance(usage, dict)
        ):
            raise ValueError("invalid shared checkpoint for retained proposal")
        workflow = self.workflow.get(execution.task_id, execution.task_attempt)
        broker = SharedToolBroker(
            worktree=Path(execution.worktree_path),
            scopes=claim.scopes,
            case_insensitive_filesystem=repository.path_case_insensitive,
            limits=self.lifecycle.limits,
            publication_lock=self.publisher.lock,
            agent_mode=workflow["agent_mode"] if workflow else "auto",
        )
        broker.restore_usage(usage)
        self.workflow.retain(
            execution.task_id,
            execution.task_attempt,
            execution.execution_id,
            broker.candidates(),
        )

    def _can_resume(
        self,
        execution: ExecutionRecord,
        capabilities: Callable[[str], DriverCapabilities | None],
        can_launch: Callable[[ExecutionLaunchRecord], bool] | None,
    ) -> bool:
        capability = capabilities(execution.driver)
        saved = self.store.get_execution_checkpoint(execution.execution_id)
        claim = self.store.get_claim(execution.claim_id)
        launch = self.store.get_execution_launch(
            execution.task_id, execution.task_attempt
        )
        task = self.store.get_task(execution.task_id)
        session = self.store.get_session(task.session_id or "") if task else None
        workspace = self.store.get_workspace(execution.workspace_id or "")
        checkout = self.store.get_checkout(session.checkout_id) if session else None
        return bool(
            capability
            and capability.shared_workspace
            and capability.resumable
            and capability.enforces_scope
            and saved
            and saved.driver == execution.driver
            and launch
            and launch.driver == execution.driver
            and (can_launch is None or can_launch(launch))
            and task
            and task.attempt == execution.task_attempt
            and task.current_claim_id == execution.claim_id
            and session
            and session.workspace_id == execution.workspace_id
            and session.workspace_mode == "shared"
            and session.state in {"active", "disconnected"}
            and workspace
            and workspace.kind == "shared_checkout"
            and workspace.mode == "shared"
            and workspace.state == "active"
            and workspace.checkout_id == session.checkout_id
            and workspace.canonical_path == execution.worktree_path
            and checkout
            and checkout.repository_id == task.repository_id
            and checkout.canonical_path == workspace.canonical_path
            and claim
            and claim.task_id == execution.task_id
            and claim.task_attempt == execution.task_attempt
            and (
                claim.scheduling_mode == "exclusive"
                or claim.workspace_id == execution.workspace_id
            )
            and claim.state in {ClaimState.ACTIVE_WORK, ClaimState.PUBLISHING}
            and execution.state in {"running", "validated", "publishing"}
        )

    def _adopt(self, execution: ExecutionRecord) -> None:
        # Startup precedes lease expiry reconciliation. No successor has been
        # activated for this still-reserved claim, so its private work can be
        # resumed with the same fence even after a long daemon outage.
        with self.store.connection() as connection, immediate_transaction(connection):
            now = self.store._now(None)
            connection.execute(
                """UPDATE claims SET state = 'active_work', lease_expires_at = ?,
                    updated_at = ? WHERE claim_id = ?
                    AND state IN ('active_work', 'publishing')""",
                (now + self.coordinator.launch_lease_ms, now, execution.claim_id),
            )
            connection.execute(
                """UPDATE tasks SET coordination_state = 'active_work', updated_at = ?
                    WHERE task_id = ? AND attempt = ?""",
                (now, execution.task_id, execution.task_attempt),
            )
            connection.execute(
                """UPDATE task_executions SET state = 'running', boot_id = ?,
                    updated_at = ? WHERE execution_id = ?""",
                (self.lifecycle.boot_id, now, execution.execution_id),
            )
        self.store.record_task_event(
            task_id=execution.task_id,
            claim_id=execution.claim_id,
            event_type="execution.resuming",
            payload={"execution_id": execution.execution_id},
        )
