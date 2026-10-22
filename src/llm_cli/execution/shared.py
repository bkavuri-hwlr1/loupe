"""Session execution against private candidates and a shared checkout journal."""

from __future__ import annotations

import difflib
import functools
import hashlib
import itertools
import json
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
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
from llm_cli.agent.shared_tools import CATCH_UP_ROUNDS, SharedToolBroker
from llm_cli.agent.source_policy import ensure_safe_content
from llm_cli.agent.tools import TaskCancelled, ToolOutcome
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
    WorkspaceExecutionRecord,
)
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.execution.checks import (
    CheckRunner,
    digest,
    published_verification,
    verification_current,
    verification_identity,
    verification_status,
)
from llm_cli.execution.commands import (
    DEPENDENCY_PATHS,
    CommandRunner,
    CommandSettings,
    HookRun,
)
from llm_cli.execution.environment import EnvironmentProvider
from llm_cli.execution.recovery import RecoveryOutcome
from llm_cli.execution.runner import TaskExecutionRunner
from llm_cli.execution.sandbox import available_sandbox
from llm_cli.execution.workspace import Execution, assert_execution_regime
from llm_cli.mcp.tools import McpToolset
from llm_cli.storage.connection import immediate_transaction
from llm_cli.workspace.batches import BatchFile, SharedBatchPublisher
from llm_cli.workspace.context import SharedCoordinationContext
from llm_cli.workspace.identity import SYMLINK_MODE, FileIdentity
from llm_cli.workspace.isolated import (
    IsolatedWorkspaceManager,
    SourceCaptureCache,
    capture_source,
)
from llm_cli.workspace.isolated_sync import IsolatedSynchronizer
from llm_cli.workspace.workflow import TaskWorkflow, decode_files, encode_files


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
        self.isolated: IsolatedWorkspaceManager | None = None
        self.workflow = TaskWorkflow(self.store, self.coordinator, publisher)
        capture_cache = SourceCaptureCache()
        self.isolated_sync = IsolatedSynchronizer(
            self.store,
            publisher,
            lambda root, protected: capture_source(
                root,
                publisher.candidate_dir,
                protected_paths=protected,
                cache=capture_cache,
            ),
            self._validate_isolated,
            refresh_capture=lambda root, protected: capture_source(
                root,
                publisher.candidate_dir,
                protected_paths=protected,
                cache=capture_cache,
                defer_changed=True,
            ),
        )
        self.isolated_sync.runtime_refresh = lambda workspace_id: (
            self.isolated.runtime.refresh(workspace_id)
            if self.isolated is not None
            else ()
        )
        self.workflow.isolated_sync = self.isolated_sync
        self.is_shutting_down: Callable[[], bool] = lambda: False
        # Set by the daemon. Without it, or without a working OS sandbox,
        # tasks are never offered model-chosen commands.
        self.commands: CommandSettings | None = None
        self.command_environment: EnvironmentProvider | None = None
        # Configured MCP servers; only declared read-only servers run in plan mode.
        self.mcp_servers: tuple[McpServerConfig, ...] = ()
        # User hooks, run in a sandbox around each task's tool calls.
        self.hooks: tuple[HookConfig, ...] = ()

    def _validate_isolated(self, workspace_id: str) -> object:
        if self.isolated is None:
            raise ClaimConflict("isolated workspace manager is unavailable")
        return self.isolated.validate(workspace_id)

    def _prepare_isolated(self, workspace_id: str) -> None:
        if self.isolated is None:
            raise ClaimConflict("isolated workspace manager is unavailable")
        state = self.isolated.validate(workspace_id)
        self.isolated_sync.register(
            workspace_id,
            state.source_manifest,
            state.source_sequence,
            state.runtime_paths,
        )
        self.isolated_sync.recover(workspace_id)

    def ensure_available(self, workspace_id: str) -> None:
        with self.store.connection() as connection:
            source = connection.execute(
                "SELECT source_workspace_id FROM workspaces WHERE workspace_id=? "
                "AND mode='isolated'",
                (workspace_id,),
            ).fetchone()
        if source is not None:
            workspace_id = str(source[0])
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
        from llm_cli.workspace.authority import managed_pin

        primary = Path(repository.main_worktree_path)
        if task.session_id:
            session = self.store.get_session(task.session_id)
            checkout = self.store.get_checkout(session.checkout_id) if session else None
            if checkout is not None:
                primary = Path(checkout.canonical_path)
        with managed_pin(self.store, primary):
            assert_execution_regime(self.store, primary)
            result = self._execute_owned(
                repository=repository,
                task=task,
                claim=claim,
                driver=driver,
                instructions=instructions,
                conversation_state=conversation_state,
                coordination_context=coordination_context,
                asker=asker,
            )
            assert isinstance(result, ExecutionRecord)
            return result

    def execute_workspace(
        self,
        *,
        repository: RepositoryRecord,
        task: TaskRecord,
        execution: WorkspaceExecutionRecord,
        driver: AgentDriver,
        instructions: str,
        conversation_state: Mapping[str, object] | None = None,
        coordination_context: str | None = None,
        asker: Callable[[str], str] | None = None,
    ) -> WorkspaceExecutionRecord:
        from llm_cli.workspace.authority import managed_pin

        session = self.store.get_session(execution.session_id)
        checkout = self.store.get_checkout(session.checkout_id) if session else None
        if checkout is None:
            raise ClaimConflict("workspace execution has no selected checkout")
        with managed_pin(self.store, Path(checkout.canonical_path)):
            assert_execution_regime(
                self.store, Path(checkout.canonical_path), execution
            )
            result = self._execute_owned(
                repository=repository,
                task=task,
                claim=None,
                workspace_execution=execution,
                driver=driver,
                instructions=instructions,
                conversation_state=conversation_state,
                coordination_context=coordination_context,
                asker=asker,
            )
            assert isinstance(result, WorkspaceExecutionRecord)
            return result

    def _settle(
        self, execution_id: str, *, outcome: str, failure_code: str | None = None
    ) -> None:
        execution = self.store.get_execution_by_id(execution_id)
        if isinstance(execution, WorkspaceExecutionRecord):
            self.coordinator.settle_workspace_execution(
                execution_id, outcome=outcome, failure_code=failure_code
            )
        else:
            self.coordinator.settle_shared_execution(
                execution_id, outcome=outcome, failure_code=failure_code
            )

    def _lease(
        self, task: TaskRecord, claim: ClaimRecord | None
    ) -> AbstractContextManager[None]:
        return (
            self.lifecycle._renewing_lease(task=task, claim=claim)
            if claim
            else nullcontext()
        )

    def _execute_owned(
        self,
        *,
        repository: RepositoryRecord,
        task: TaskRecord,
        claim: ClaimRecord | None,
        workspace_execution: WorkspaceExecutionRecord | None = None,
        driver: AgentDriver,
        instructions: str,
        conversation_state: Mapping[str, object] | None = None,
        coordination_context: str | None = None,
        asker: Callable[[str], str] | None = None,
    ) -> Execution:
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
        isolated = session is not None and session.workspace_mode == "isolated"
        if (
            session is None
            or workspace is None
            or session.state not in {"active", "disconnected"}
            or session.workspace_mode not in {"shared", "isolated"}
            or workspace.kind != ("linked_worktree" if isolated else "shared_checkout")
            or workspace.mode != session.workspace_mode
            or workspace.checkout_id != session.checkout_id
            or workspace.state != "active"
            or (
                not isolated
                and workspace.canonical_path != repository.main_worktree_path
            )
            or (
                claim is not None
                and claim.scheduling_mode == "optimistic"
                and claim.workspace_id != workspace.workspace_id
            )
        ):
            raise ClaimConflict("task no longer names its active shared checkout")
        if workspace_execution is not None:
            if claim is not None or (
                workspace_execution.task_id != task.task_id
                or workspace_execution.task_attempt != task.attempt
                or workspace_execution.session_id != session.session_id
                or workspace_execution.workspace_id != workspace.workspace_id
                or task.current_claim_id is not None
            ):
                raise ClaimConflict("workspace authority does not belong to this task")
            scopes = workspace_execution.scopes
        else:
            if (
                claim is None
                or claim.state is not ClaimState.ACTIVE_WORK
                or claim.fencing_token is None
            ):
                raise ClaimConflict("shared execution requires an active fenced claim")
            if claim.task_id != task.task_id or claim.task_attempt != task.attempt:
                raise ClaimConflict("claim does not belong to this task attempt")
            scopes = claim.scopes
        checkout = self.store.get_checkout(session.checkout_id)
        if (
            checkout is None
            or checkout.repository_id != repository.repository_id
            or (not isolated and checkout.canonical_path != workspace.canonical_path)
        ):
            raise ClaimConflict("shared execution checkout binding changed")
        isolated_state = None
        if isolated:
            if self.isolated is None:
                raise ClaimConflict("isolated workspace manager is unavailable")
            isolated_state = self.isolated.validate(workspace.workspace_id)
            self._prepare_isolated(workspace.workspace_id)
            self.ensure_available(workspace.workspace_id)
            self.isolated_sync.synchronize(workspace.workspace_id, refresh_runtime=True)
            isolated_state = self.isolated.validate(workspace.workspace_id)
        existing = self.store.get_execution(task.task_id, task.attempt)
        if workspace_execution is not None:
            if not isinstance(existing, WorkspaceExecutionRecord) or (
                existing != workspace_execution
                or existing.state not in {"preparing", "running"}
                or existing.driver != driver.name
                or existing.boot_id != self.lifecycle.boot_id
            ):
                raise ClaimConflict(
                    "workspace execution requires recovery before continuing"
                )
            assert_execution_regime(self.store, Path(checkout.canonical_path), existing)
            execution: Execution = existing
            resuming = existing.state == "running"
        else:
            if existing is not None and (
                not isinstance(existing, ExecutionRecord)
                or existing.workspace_id != workspace.workspace_id
                or existing.state != "running"
                or existing.driver != driver.name
            ):
                raise ClaimConflict(
                    "shared execution requires recovery before continuing"
                )
            assert claim is not None
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
                        f"workspace:{workspace.workspace_epoch}:{workspace.workspace_revision}"
                    )
                ),
                boot_id=self.lifecycle.boot_id,
                workspace_id=workspace.workspace_id,
            )
            resuming = existing is not None
        row = self.workflow.ensure(task.task_id)
        runtime_files = isolated_state.runtime_files if isolated_state else ()
        configured_runtime_paths = tuple(
            json.loads(row["config_json"]).get("runtime_paths", [])
        )
        command_runners: list[CommandRunner] = []
        sensitive_paths = (
            tuple(
                path
                for path, entry in isolated_state.source_manifest.items()
                if entry.get("screened")
            )
            if isolated_state
            else ()
        )
        if runtime_files:
            config = json.loads(row["config_json"])
            config["runtime_paths"] = sorted(
                set(config.get("runtime_paths", [])) | set(runtime_files)
            )
            row["config_json"] = json.dumps(config, sort_keys=True)
            self.workflow.update(
                task.task_id, task.attempt, config_json=row["config_json"]
            )
        self.workflow.update(
            task.task_id, task.attempt, execution_id=execution.execution_id
        )
        broker: SharedToolBroker | None = None
        try:
            if claim is not None:
                assert claim.fencing_token is not None
                self.coordinator.renew_claim(
                    task_id=task.task_id,
                    claim_id=claim.claim_id,
                    fencing_token=claim.fencing_token,
                    attempt=task.attempt,
                )
            if not resuming:
                execution = self.store.update_execution(
                    execution.execution_id, state="running"
                )
            saved = self.store.get_execution_checkpoint(execution.execution_id)
            if resuming and saved is None:
                raise ClaimConflict("shared execution has no durable checkpoint")

            def record_read(relative: str, identity: FileIdentity) -> None:
                if isolated:
                    return
                self.store.record_workspace_read(
                    session_id=session.session_id,
                    relative_path=relative,
                    identity=identity,
                )

            broker = SharedToolBroker(
                worktree=Path(workspace.canonical_path),
                scopes=scopes,
                project_search=(
                    functools.partial(
                        self.lifecycle.project_search,
                        checkout.checkout_id,
                        Path(workspace.canonical_path),
                    )
                    if self.lifecycle.project_search is not None
                    else None
                ),
                on_read=record_read,
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

            def candidate_files() -> tuple[BatchFile, ...]:
                files = broker.candidates()
                if row["agent_mode"] == "plan":
                    return files
                return (
                    self.isolated_sync.publication_files(workspace.workspace_id, files)
                    if isolated
                    else files
                )

            def reload_runtime_paths() -> None:
                nonlocal runtime_files
                if not isolated:
                    return
                assert self.isolated is not None
                state = self.isolated.get(workspace.workspace_id)
                if state is None:
                    raise ClaimConflict("isolated runtime workspace is unavailable")
                runtime_files = state.runtime_files
                runtime_paths = sorted(
                    set(configured_runtime_paths) | set(runtime_files)
                )
                if checker.config.get("runtime_paths", []) != runtime_paths:
                    checker.config["runtime_paths"] = runtime_paths
                    row["config_json"] = json.dumps(checker.config, sort_keys=True)
                    self.workflow.update(
                        task.task_id, task.attempt, config_json=row["config_json"]
                    )
                for runner in command_runners:
                    runner.runtime_files = runtime_files
                    runner.dependency_paths = (*DEPENDENCY_PATHS, *runtime_paths)

            @contextmanager
            def runtime_activity() -> Iterator[None]:
                activity = (
                    self.isolated.runtime.activity(workspace.workspace_id)
                    if isolated and self.isolated is not None
                    else nullcontext()
                )
                with activity:
                    # A helper may have installed a generation after this tool's
                    # safe point. Read its paths only after refresh is excluded.
                    reload_runtime_paths()
                    yield

            def safe_point(
                *, reconcile: bool = True, refresh_source: bool = True
            ) -> str:
                if not isolated:
                    return ""
                self.ensure_available(workspace.workspace_id)
                report = self.isolated_sync.synchronize(
                    workspace.workspace_id,
                    refresh_source=refresh_source,
                    refresh_runtime=refresh_source,
                )
                reload_runtime_paths()
                note = report.note()
                if note:
                    broker.emit("workspace.isolated_synchronized", {"notice": note})
                if reconcile:
                    pending = broker.reconcile()
                    if pending:
                        broker.emit("edits.reconciled", pending.payload())
                        note += "\n" + pending.note()
                note = note.strip()
                if note and broker.helper_call_active():
                    broker.notify_parent(note)
                return note

            deadline_value = saved.checkpoint.get("deadline_at") if saved else None
            deadline_at = (
                float(deadline_value)
                if isinstance(deadline_value, (float, int))
                else time.time() + self.lifecycle.limits.wall_clock_seconds
            )
            verification_root = (
                Path(checkout.canonical_path) if isolated else broker.worktree
            )
            runtime_root = broker.worktree if isolated else None
            checker = CheckRunner(
                self.workflow,
                row,
                verification_root,
                self.lifecycle.managed_worktree_root.parent / "check-worktrees",
                candidate_files,
                lambda: (
                    self.workflow.stopped(task.task_id, task.attempt)
                    or self.is_shutting_down()
                ),
                broker.emit,
                self.publisher.lock,
                deadline_at=deadline_at,
                on_result=broker.record_check,
                runtime_root=runtime_root,
                sensitive_paths=sensitive_paths,
                environment=(
                    functools.partial(self.command_environment, session.session_id)
                    if self.command_environment is not None
                    else None
                ),
            )

            def check_gate() -> ToolOutcome | None:
                with runtime_activity():
                    return checker.gate()

            checked = bool(checker.config.get("checks"))
            if row["agent_mode"] != "plan":
                # Before checks and finishing, pending edits are moved onto
                # files other sessions changed, so they verify what would
                # actually be published.
                def finish_gate() -> ToolOutcome | None:
                    safe_point()
                    with runtime_activity():
                        return broker.finish_gate_with(
                            check_gate if checked else None,
                            checker.current if checked else lambda: True,
                        )

                broker.finish_gate = finish_gate
            if checked and row["agent_mode"] != "plan":

                def run_check(name: str) -> ToolOutcome:
                    safe_point()
                    with runtime_activity():
                        return broker.catching_up(checker.run)(name)

                broker.check_runner = run_check
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
                commands = CommandRunner(
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
                    runtime_files=runtime_files,
                    sensitive_paths=sensitive_paths,
                    environment=(
                        functools.partial(self.command_environment, session.session_id)
                        if self.command_environment is not None
                        else None
                    ),
                )

                command_runners.append(commands)

                def run_command(argv: list[str], cwd: str, timeout: int) -> ToolOutcome:
                    safe_point()
                    with runtime_activity():
                        return broker.catching_up(commands.run)(argv, cwd, timeout)

                broker.command_runner = run_command
                broker.command_approval = self.commands.approval
            if self.hooks and self.commands is not None:
                hook_sandbox = available_sandbox()
                if hook_sandbox is None:
                    broker.emit(
                        "hooks.unavailable",
                        {"reason": "no working operating-system sandbox"},
                    )
                else:
                    hook_commands = CommandRunner(
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
                        runtime_files=runtime_files,
                        sensitive_paths=sensitive_paths,
                        environment=(
                            functools.partial(
                                self.command_environment, session.session_id
                            )
                            if self.command_environment is not None
                            else None
                        ),
                    )
                    command_runners.append(hook_commands)

                    def run_hook(
                        argv: list[str],
                        timeout: int,
                        *,
                        hook_input: object = None,
                        read_back: Sequence[str] = (),
                    ) -> HookRun:
                        safe_point()
                        with runtime_activity():
                            return hook_commands.run_hook(
                                argv,
                                timeout,
                                hook_input=hook_input,
                                read_back=read_back,
                            )

                    broker.hooks = HookRunner(self.hooks, run_hook, broker.emit)
            mcp = (
                McpToolset(
                    self.mcp_servers,
                    interactive=asker is not None,
                    emit=broker.emit,
                    read_only=row["agent_mode"] == "plan",
                )
                if self.mcp_servers
                else None
            )
            broker.mcp = mcp
            context = SharedCoordinationContext(
                self.store,
                session.session_id,
                scopes,
                scan_external=not isolated,
            )

            def refresh_coordination(after_sequence: int | None) -> CoordinationUpdate:
                note = safe_point()
                with self.publisher.lock:
                    self.ensure_available(workspace.workspace_id)
                    update = context(after_sequence)
                    return (
                        replace(
                            update,
                            facts_unchanged=False,
                            text="\n".join(
                                part for part in (update.text, note) if part
                            ),
                        )
                        if note
                        else update
                    )

            def review_context() -> Mapping[str, object]:
                # This snapshot is runner-authored after the finish gate has
                # reconciled edits and checked them. Never trust model-provided
                # diff text or check claims, or silently truncate the diff.
                with self.publisher.lock:
                    self.ensure_available(workspace.workspace_id)
                    files = candidate_files()
                    if not checker.current() or broker.stale_paths():
                        raise ValueError("the candidate/check snapshot changed")
                    sections: list[str] = []
                    contents: list[dict[str, object]] = []
                    line_counts: dict[str, int] = {}
                    for file in files:
                        before, after = file.original or b"", file.content or b""
                        ensure_safe_content(before)
                        ensure_safe_content(after)
                        if (
                            file.binary
                            or file.mode == SYMLINK_MODE
                            or file.base.mode == SYMLINK_MODE
                        ):
                            contents.append(
                                {
                                    "path": file.relative_path,
                                    "kind": "binary" if file.binary else "symlink",
                                    "before_digest": file.base.digest,
                                    "after_digest": hashlib.sha256(after).hexdigest(),
                                    "before_size": len(before),
                                    "after_size": len(after),
                                    "mode": file.mode,
                                }
                            )
                            line_counts[file.relative_path] = 0
                            sections.append(
                                f"Path {file.relative_path}: typed replacement "
                                f"({len(before)} -> {len(after)} bytes)\n"
                            )
                            continue
                        contents.append(
                            {
                                "path": file.relative_path,
                                "original": before.decode(),
                                "candidate": (
                                    None if file.content is None else after.decode()
                                ),
                                "mode": file.mode,
                                "permissions": file.permissions,
                            }
                        )
                        line_counts[file.relative_path] = len(
                            (before if file.content is None else after).splitlines()
                        )
                        sections.append(
                            f"Path {file.relative_path}: mode {file.base.mode} "
                            f"-> {file.mode}; permissions {file.permissions}\n"
                        )
                        sections.extend(
                            difflib.unified_diff(
                                before.decode().splitlines(keepends=True),
                                after.decode().splitlines(keepends=True),
                                fromfile="a/" + file.relative_path,
                                tofile="b/" + file.relative_path,
                            )
                        )
                    candidate = digest(encode_files(files))
                    source = verification_identity(
                        verification_root, checker.config, runtime_root=runtime_root
                    )
                    with self.store.connection() as connection:
                        checks = connection.execute(
                            "SELECT name,state,exit_code,output,truncated FROM "
                            "check_runs WHERE task_id=? AND attempt=? AND "
                            "candidate_digest=? AND source_digest=? AND "
                            "config_digest=? ORDER BY created_at,run_id",
                            (
                                task.task_id,
                                task.attempt,
                                candidate,
                                source,
                                digest(row["config_json"]),
                            ),
                        ).fetchall()
                    evidence = [dict(check) for check in checks]
                    ensure_safe_content(json.dumps(evidence))
                    return {
                        "diff": "".join(sections),
                        # Preserve exact text, including missing final newlines
                        # that a unified diff alone cannot represent clearly.
                        "files": contents,
                        "line_counts": line_counts,
                        "candidate_digest": candidate,
                        "source_digest": source,
                        "checks": evidence,
                        "verification": "passed" if checked else "not_applicable",
                    }

            request = RunRequest(
                task_id=task.task_id,
                attempt=task.attempt,
                instructions=instructions,
                scopes=scopes,
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
                workspace_mode=session.workspace_mode,
                agent_mode=row["agent_mode"],
                refresh_coordination=refresh_coordination,
                # An edit task's answer stays a draft until settlement, so
                # it can describe what was actually published or held.
                publication_aware_finalization=isinstance(driver, SettlementFinalizer),
                review_context=review_context,
            )
            # Starting servers can take longer than a lease, so it is renewed.
            with self._lease(task, claim):
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
            files = candidate_files()
            if row["agent_mode"] == "plan" and files:
                raise ValueError("plan tasks cannot publish file changes")
            self.workflow.retain(
                task.task_id, task.attempt, execution.execution_id, files
            )
            gave_up = False
            for rounds in itertools.count():
                if rounds:
                    gave_up = not self._catch_up(
                        task, claim, broker, check_gate if checked else None
                    )
                    if not gave_up or not isolated:
                        files = candidate_files()
                    self.workflow.retain(
                        task.task_id, task.attempt, execution.execution_id, files
                    )
                safe_point(reconcile=False)
                with self.publisher.lock:
                    self.ensure_available(workspace.workspace_id)
                    safe_point(reconcile=False, refresh_source=False)
                    stopped = self.workflow.stopped(task.task_id, task.attempt)
                    incomplete = run.outcome != "completed"
                    if incomplete and not files and not stopped:
                        self._settle(
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
                    # Another session may have published since this task's
                    # checks ran. An auto-mode task that finished catches up
                    # instead of being held: its edits are merged onto the
                    # current files and its checks rerun, outside the lock.
                    behind = (
                        not gave_up
                        and rounds < CATCH_UP_ROUNDS
                        and bool(files)
                        and not stopped
                        and not incomplete
                        and row["publish_mode"] != "review"
                        and bool(
                            broker.stale_paths()
                            or not verification_current(
                                self.store,
                                row,
                                verification_root,
                                files,
                                runtime_root=runtime_root,
                            )
                        )
                    )
                    if behind:
                        continue
                    hold = (
                        stopped
                        or incomplete
                        or (isolated and gave_up)
                        or bool(
                            files
                            and (
                                row["publish_mode"] == "review"
                                or not verification_current(
                                    self.store,
                                    row,
                                    verification_root,
                                    files,
                                    runtime_root=runtime_root,
                                )
                            )
                        )
                    )
                    verification = verification_status(
                        self.store,
                        row,
                        verification_root,
                        files,
                        runtime_root=runtime_root,
                    )
                    outcome = "held_for_review"
                    if not hold:
                        broker.emit("workflow.verification", {"status": verification})
                        assert_execution_regime(
                            self.store, Path(checkout.canonical_path), execution
                        )
                        if isinstance(execution, WorkspaceExecutionRecord):
                            self.coordinator.reserve_workspace_publication(
                                execution.execution_id,
                                regime_generation=execution.regime_generation,
                                changed_paths=(file.relative_path for file in files),
                            )
                        else:
                            assert claim is not None and claim.fencing_token is not None
                            self.coordinator.reserve_shared_publication(
                                execution.execution_id,
                                fencing_token=claim.fencing_token,
                                changed_paths=(file.relative_path for file in files),
                            )
                        if not files:
                            outcome = "no_changes"
                        else:
                            publication = (
                                self.isolated_sync if isolated else self.publisher
                            ).publish(
                                execution.execution_id,
                                session.session_id,
                                files,
                                scopes,
                                case_insensitive_filesystem=(
                                    repository.path_case_insensitive
                                ),
                            )
                            outcome = publication.state
                break
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
                    changed_paths=tuple(sorted(file.relative_path for file in files)),
                ),
            )
            with self.publisher.lock:
                if hold:
                    self.workflow.hold(
                        task.task_id,
                        task.attempt,
                        execution.execution_id,
                        cancelled=stopped,
                        completion_outcome=("cancelled" if stopped else run.outcome),
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
                self._settle(execution.execution_id, outcome=outcome)
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
            if broker is not None:
                saved_usage = self.store.get_execution_checkpoint(
                    execution.execution_id
                )
                totals = (
                    saved_usage.checkpoint.get("usage_total") if saved_usage else None
                )
                # An interrupted review still spent tokens. The harness saves
                # known totals before raising cancellation/deadline errors; keep
                # them when execution never returns a RunResult below.
                if (
                    isinstance(totals, dict)
                    and all(
                        isinstance(key, str) and type(value) is int and value >= 0
                        for key, value in totals.items()
                    )
                    and any(key.startswith("review_") for key in totals)
                ):
                    self.store.record_execution_usage(execution.execution_id, totals)
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
                        self.store,
                        row,
                        verification_root,
                        files,
                        runtime_root=runtime_root,
                    )
                    self.workflow.hold(
                        task.task_id,
                        task.attempt,
                        execution.execution_id,
                        cancelled=self.workflow.stopped(task.task_id, task.attempt),
                        completion_outcome=(
                            "cancelled" if isinstance(exc, TaskCancelled) else "failed"
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
                    # A diverged batch settles without an answer, so a
                    # response that cannot be finished must not block it.
                    with suppress(ValueError, LlmCoordError):
                        self._finish_interrupted_response(fresh, batch.state, driver)
                    self._settle(execution.execution_id, outcome=batch.state)
                elif batch is None:
                    code = (
                        exc.code.value
                        if isinstance(exc, LlmCoordError)
                        else "EXECUTION_FAILED"
                    )
                    self._settle(
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

    def _catch_up(
        self,
        task: TaskRecord,
        claim: ClaimRecord | None,
        broker: SharedToolBroker,
        gate: Callable[[], ToolOutcome | None] | None,
    ) -> bool:
        """Move a finished task's edits onto the current checkout and recheck.

        Only a clean merge is kept: a conflict needs the agent or the user, so
        the edits stay as the agent left them and the task is held. Checks run
        again on the merged edits, which is all publication then needs.
        """

        with self._lease(task, claim):
            report = broker.reconcile(only_if_clean=True)
            if not report.clean:
                return False
            if report:
                broker.emit("edits.reconciled", report.payload())
            return gate is None or gate() is None

    def _finalize_response(
        self,
        *,
        driver: AgentDriver,
        task: TaskRecord,
        claim: ClaimRecord | None,
        execution: Execution,
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
        if (
            saved is None
            or state is None
            or state.status
            in {
                "completed",
                "interrupted",
            }
        ):
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
            with self._lease(task, claim):
                finished = driver.finalize(request)
        except Exception as exc:
            # Settlement must still complete; the task keeps its files and
            # states that its answer is missing rather than inventing one.
            broker.emit("model.finalization_failed", {"error": type(exc).__name__})
            return
        # The run was recorded before settlement; a rewrite adds tokens.
        self.store.record_execution_usage(execution.execution_id, finished.usage)

    def recover(
        self,
        capabilities: Callable[[str], DriverCapabilities | None],
        *,
        can_launch: Callable[[ExecutionLaunchRecord], bool] | None = None,
        driver_for: Callable[[ExecutionLaunchRecord], AgentDriver] | None = None,
    ) -> tuple[RecoveryOutcome, ...]:
        """Recover journaled effects first, then adopt private checkpoints.

        The caller holds the publication lock and has recovered filesystem
        journals before this method. A shared checkout is never a cleanup path.
        ``driver_for`` rebuilds a task's driver so a response interrupted after
        publication can be finished, without a model request, before settling.
        """

        with self.store.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM all_task_executions e WHERE workspace_id IS NOT NULL
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
                primary_workspace = self.store.get_workspace(
                    execution.workspace_id or ""
                )
                primary_checkout = (
                    self.store.get_checkout(primary_workspace.checkout_id)
                    if primary_workspace
                    else None
                )
                if primary_checkout is None:
                    raise ValueError("recovery execution has no selected checkout")
                from llm_cli.workspace.authority import managed_pin

                with managed_pin(self.store, Path(primary_checkout.canonical_path)):
                    assert_execution_regime(
                        self.store, Path(primary_checkout.canonical_path), execution
                    )
                    workspace = self.store.get_workspace(execution.workspace_id or "")
                    batch = self.publisher.get(execution.execution_id)
                    if (
                        workspace
                        and workspace.mode == "isolated"
                        and not (
                            batch is not None
                            and batch.state in {"published", "diverged"}
                        )
                    ):
                        self._prepare_isolated(workspace.workspace_id)
                        self.isolated_sync.synchronize(workspace.workspace_id)
                    if batch is not None and batch.state in {"published", "diverged"}:
                        self._finish_interrupted_response(
                            execution,
                            batch.state,
                            self._recovery_driver(execution, driver_for),
                        )
                        self._settle(execution.execution_id, outcome=batch.state)
                        resolution = (
                            "confirmed" if batch.state == "published" else "failed_safe"
                        )
                        if (
                            workspace
                            and workspace.mode == "isolated"
                            and batch.state == "published"
                        ):
                            self.isolated_sync.after_publication(
                                execution.execution_id, workspace.workspace_id
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
                        files = (
                            decode_files(encoded) if isinstance(encoded, str) else ()
                        )
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
                        self._settle(execution.execution_id, outcome="failed_safe")
                        resolution = "failed_safe"
            except (ValueError, LlmCoordError, CoordinationError) as exc:
                if isinstance(exc, LlmCoordError) and exc.code in {
                    ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE,
                    ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED,
                    ErrorCode.CHECKOUT_IDENTITY_CHANGED,
                }:
                    outcomes.append(
                        RecoveryOutcome(
                            task_id=execution.task_id,
                            task_attempt=execution.task_attempt,
                            resolution="operator_attention",
                            detail=exc.code.value,
                            execution_id=execution.execution_id,
                        )
                    )
                    continue
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

    def _recovery_driver(
        self,
        execution: Execution,
        driver_for: Callable[[ExecutionLaunchRecord], AgentDriver] | None,
    ) -> AgentDriver | None:
        launch = self.store.get_execution_launch(
            execution.task_id, execution.task_attempt
        )
        if driver_for is None or launch is None:
            return None
        try:
            return driver_for(launch)
        except Exception:
            # Factories are extension code. Without the driver, a published
            # batch stays for operator attention; a diverged one settles.
            return None

    def _finish_interrupted_response(
        self,
        execution: Execution,
        publication: str,
        driver: AgentDriver | None,
    ) -> None:
        """Finish a response that stopped after its batch was journaled.

        Settlement promotes only a finished conversation, so the response is
        finished first, from facts the journal and checkpoint establish. It
        never sends a model request: a draft that assumed this outcome stands,
        as it would have live, and otherwise the answer states only the facts
        and is marked interrupted.
        """

        if not isinstance(driver, SettlementFinalizer):
            return
        saved = self.store.get_execution_checkpoint(execution.execution_id)
        state = checkpoint_finalization(saved.checkpoint) if saved else None
        if (
            saved is None
            or state is None
            or state.status
            in {
                "completed",
                "interrupted",
            }
        ):
            return
        workflow = self.workflow.get(execution.task_id, execution.task_attempt)
        launch = self.store.get_execution_launch(
            execution.task_id, execution.task_attempt
        )
        if workflow is None or launch is None:
            raise ValueError("an interrupted response has no task recipe")
        broker = self._checkpoint_broker(execution, saved.checkpoint)
        facts = state.facts or SettlementFacts(
            publication=publication,
            verification=published_verification(workflow),
            completion=broker.usage.outcome,
            changed_paths=tuple(
                sorted(file.relative_path for file in broker.candidates())
            ),
        )
        request = FinalizationRequest(
            task_id=execution.task_id,
            attempt=execution.task_attempt,
            instructions=launch.instructions,
            draft=broker.usage.answer,
            summary=broker.usage.summary,
            facts=facts,
            resume_state=saved.checkpoint,
            checkpoint=functools.partial(
                self.lifecycle._checkpoint_execution,
                execution_id=execution.execution_id,
                driver=execution.driver,
            ),
            use_model=settlement_surprises(
                facts, publish_mode=workflow["publish_mode"]
            ),
            # A rewrite the outcome calls for is recorded as interrupted
            # rather than sent.
            cancelled=lambda: True,
        )
        if state.status == "prepared":
            request = replace(
                request, resume_state=driver.prepare_finalization(request)
            )
        driver.finalize(request)

    def _checkpoint_broker(
        self, execution: Execution, checkpoint: Mapping[str, object]
    ) -> SharedToolBroker:
        """Rebuild a task's tool state, including its edits, from a checkpoint."""

        claim = self.store.get_claim(execution.claim_id) if execution.claim_id else None
        task = self.store.get_task(execution.task_id)
        repository = self.store.get_repository(task.repository_id) if task else None
        usage = checkpoint.get("tool_usage")
        if (
            repository is None
            or (
                not isinstance(execution, WorkspaceExecutionRecord)
                and (
                    claim is None
                    or claim.task_id != execution.task_id
                    or claim.task_attempt != execution.task_attempt
                )
            )
            or not isinstance(usage, dict)
        ):
            raise ValueError("invalid shared checkpoint for this task")
        workflow = self.workflow.get(execution.task_id, execution.task_attempt)
        broker = SharedToolBroker(
            worktree=Path(execution.worktree_path),
            scopes=execution.scopes
            if isinstance(execution, WorkspaceExecutionRecord)
            else claim.scopes
            if claim
            else (),
            case_insensitive_filesystem=repository.path_case_insensitive,
            limits=self.lifecycle.limits,
            publication_lock=self.publisher.lock,
            agent_mode=workflow["agent_mode"] if workflow else "auto",
        )
        broker.restore_usage(usage)
        return broker

    def _retain_checkpoint_candidates(self, execution: Execution) -> None:
        # Cancellation may be persisted while a provider call is in flight.
        # Preserve its last durable overlay before terminalizing the attempt,
        # since settled workflows no longer expose their running checkpoint.
        saved = self.store.get_execution_checkpoint(execution.execution_id)
        if saved is None:
            return
        if saved.driver != execution.driver:
            raise ValueError("invalid shared checkpoint for retained proposal")
        broker = self._checkpoint_broker(execution, saved.checkpoint)
        self.workflow.retain(
            execution.task_id,
            execution.task_attempt,
            execution.execution_id,
            self.isolated_sync.publication_files(
                execution.workspace_id, broker.candidates()
            )
            if (
                execution.workspace_id
                and (workspace := self.store.get_workspace(execution.workspace_id))
                and workspace.mode == "isolated"
            )
            else broker.candidates(),
        )

    def _can_resume(
        self,
        execution: Execution,
        capabilities: Callable[[str], DriverCapabilities | None],
        can_launch: Callable[[ExecutionLaunchRecord], bool] | None,
    ) -> bool:
        capability = capabilities(execution.driver)
        saved = self.store.get_execution_checkpoint(execution.execution_id)
        claim = self.store.get_claim(execution.claim_id) if execution.claim_id else None
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
            and (
                (saved and saved.driver == execution.driver)
                or (
                    isinstance(execution, WorkspaceExecutionRecord)
                    and execution.state == "preparing"
                    and saved is None
                )
            )
            and launch
            and launch.driver == execution.driver
            and (can_launch is None or can_launch(launch))
            and task
            and task.attempt == execution.task_attempt
            and task.current_claim_id == execution.claim_id
            and session
            and session.workspace_id == execution.workspace_id
            and session.workspace_mode in {"shared", "isolated"}
            and session.state in {"active", "disconnected"}
            and workspace
            and workspace.kind
            == (
                "linked_worktree"
                if session.workspace_mode == "isolated"
                else "shared_checkout"
            )
            and workspace.mode == session.workspace_mode
            and workspace.state == "active"
            and workspace.checkout_id == session.checkout_id
            and workspace.canonical_path == execution.worktree_path
            and checkout
            and checkout.repository_id == task.repository_id
            and (
                session.workspace_mode == "isolated"
                or checkout.canonical_path == workspace.canonical_path
            )
            and (
                (
                    isinstance(execution, WorkspaceExecutionRecord)
                    and execution.session_id == session.session_id
                    and task.current_claim_id is None
                    and task.state
                    not in {
                        "completed",
                        "failed",
                        "cancelled",
                        "reviewing",
                        "operator_attention",
                    }
                )
                or (
                    claim is not None
                    and claim.task_id == execution.task_id
                    and claim.task_attempt == execution.task_attempt
                    and (
                        claim.scheduling_mode == "exclusive"
                        or claim.workspace_id == execution.workspace_id
                    )
                    and claim.state in {ClaimState.ACTIVE_WORK, ClaimState.PUBLISHING}
                )
            )
            and (
                execution.state in {"running", "validated", "publishing"}
                or (
                    isinstance(execution, WorkspaceExecutionRecord)
                    and execution.state == "preparing"
                )
            )
        )

    def _adopt(self, execution: Execution) -> None:
        # Startup precedes lease expiry reconciliation. No successor has been
        # activated for this still-reserved claim, so its private work can be
        # resumed with the same fence even after a long daemon outage.
        with self.store.connection() as connection, immediate_transaction(connection):
            now = self.store._now(None)
            if isinstance(execution, ExecutionRecord):
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
            table = self.store.execution_table(connection, execution.execution_id)
            connection.execute(
                f"""UPDATE {table} SET state = ?, boot_id = ?,
                    updated_at = ? WHERE execution_id = ?""",
                (
                    "preparing"
                    if isinstance(execution, WorkspaceExecutionRecord)
                    and execution.state == "preparing"
                    else "running",
                    self.lifecycle.boot_id,
                    now,
                    execution.execution_id,
                ),
            )
        self.store.record_task_event(
            task_id=execution.task_id,
            claim_id=execution.claim_id,
            event_type="execution.resuming",
            payload={"execution_id": execution.execution_id},
        )
