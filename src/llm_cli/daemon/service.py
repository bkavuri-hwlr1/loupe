"""Validated daemon RPC dispatch over the authoritative local services."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, suppress
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from _typeshed import DataclassInstance

from llm_cli import PROTOCOL_VERSION, __version__
from llm_cli.agent.driver import AgentDriver
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.modes import resolve_agent_mode, validate_agent_mode
from llm_cli.agent.registry import DriverRegistry
from llm_cli.agent.tools import MAX_QUESTION_CHARACTERS, TaskCancelled
from llm_cli.build import code_identity
from llm_cli.config.models import Settings
from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import (
    CheckoutRecord,
    ClaimAuthorityError,
    ClaimConflict,
    ClaimNotFound,
    ClaimRecord,
    CoordinationError,
    ExecutionLaunchRecord,
    RepositoryRecord,
    SessionRecord,
    TaskEventRecord,
    TaskRecord,
    WorkspaceCandidateRecord,
    WorkspaceRecord,
)
from llm_cli.coordination.scopes import ScopeValidationError, normalize_changed_path
from llm_cli.doctor import run_doctor
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.execution.commands import CommandSettings, cleanup_command_snapshots
from llm_cli.execution.recovery import (
    ExecutionReconciler,
    RecoveryOutcome,
    RecoveryReport,
)
from llm_cli.execution.runner import (
    FixtureWrite,
    FixtureWriteDriver,
    FixtureWriteRunner,
    TaskExecutionRunner,
    parse_fixture_writes,
)
from llm_cli.execution.sandbox import protected_home_paths
from llm_cli.execution.shared import SharedTaskExecutionRunner
from llm_cli.git.inspect import RepositoryInfo, inspect_repository
from llm_cli.ids import new_id
from llm_cli.paths import AppPaths
from llm_cli.protocol.envelopes import Request, Response
from llm_cli.protocol.framing import DEFAULT_MAX_FRAME_BYTES
from llm_cli.providers.anthropic_provider import AnthropicProvider
from llm_cli.providers.codex_provider import CodexProvider
from llm_cli.providers.openai_provider import OpenAIProvider
from llm_cli.providers.registry import ProviderRegistry
from llm_cli.storage.connection import connect_knowledge, connect_vectors
from llm_cli.storage.control import ControlStore
from llm_cli.storage.migrations import (
    KNOWLEDGE_MIGRATIONS,
    VECTOR_MIGRATIONS,
    apply_migrations,
    current_schema_version,
)
from llm_cli.workspace.batches import SharedBatchPublisher
from llm_cli.workspace.broker import (
    MAX_SHARED_TEXT_BYTES,
    WorkspaceBrokerError,
    atomic_replace_regular_file,
    load_candidate_content,
    store_candidate_content,
    workspace_target,
)
from llm_cli.workspace.identity import (
    EXECUTABLE_MODE,
    REGULAR_MODE,
    FileIdentity,
    ObjectKind,
    content_identity,
    identify_path,
    read_identified_path,
)

# Section 15.1 requires these limits to be visible to operators rather than
# living only in implementation notes.
_WORKSPACE_GUARANTEES: tuple[str, ...] = (
    "one brokered regular-file replacement is atomically visible",
    "one brokered multi-file batch is logically atomic only to cooperative "
    "sessions that honor the publication barrier",
    "an editor, test run, language server, or shell command reading paths "
    "directly may observe a multi-file apply in progress",
    "an external writer may expose partial content before the daemon detects "
    "stabilization",
    "isolated mode is the right choice for snapshot-consistent pre-publication "
    "tests, broad formatters, generators, and migrations",
)

_ATTACH_BATCH = 200
_ANSWER_TIMEOUT_SECONDS = 600
_MAX_ANSWER_CHARACTERS = 4_000

# Only these classifications may leave the private provider error boundary.
# Remote response bodies can echo credentials or prompt content.
_PUBLIC_PROVIDER_ERRORS = frozenset(
    {
        "unsupported_parameter",
        "unsupported_model",
        "unsupported_effort",
        "authentication",
        "rate_limit",
        "request_rejected",
        "connection",
        "timeout",
        "incomplete_response",
        "invalid_response",
        "context_overflow",
    }
)
_ATTACH_POLL_SECONDS = 0.1
_SETTLED_TASK_STATES = frozenset(
    {
        "reviewing",
        "completed",
        "failed",
        "cancelled",
        "ready_for_integration",
        "operator_attention",
    }
)


@dataclass
class _PendingQuestion:
    """One model question waiting on an attached operator."""

    question: str
    question_id: str = field(default_factory=lambda: new_id("question"))
    ready: threading.Event = field(default_factory=threading.Event)
    answer: str = ""


class DaemonService:
    def __init__(
        self,
        paths: AppPaths,
        settings: Settings,
        shutdown: asyncio.Event,
        *,
        boot_id: str,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.shutdown = shutdown
        self.boot_id = boot_id
        self.store = ControlStore(paths.control_db)
        self.coordinator = RepositoryCoordinator(
            self.store,
            launch_lease_ms=settings.launch_lease_ms,
            work_lease_ms=settings.work_lease_ms,
            terminal_retention_ms=settings.terminal_retention_ms,
        )
        self.fixture_runner = FixtureWriteRunner(
            self.store,
            self.coordinator,
            managed_worktree_root=paths.data_dir / "worktrees",
            boot_id=boot_id,
        )
        self.runner = TaskExecutionRunner(
            self.store,
            self.coordinator,
            managed_worktree_root=paths.data_dir / "worktrees",
            boot_id=boot_id,
            renewal_interval_seconds=settings.renewal_interval_ms / 1_000,
        )
        self.workspace_batches = SharedBatchPublisher(
            self.store, paths.candidate_dir, threading.RLock()
        )
        self.shared_runner = SharedTaskExecutionRunner(
            self.runner, self.workspace_batches
        )
        self.workflow = self.shared_runner.workflow
        self.shared_runner.is_shutting_down = self.shutdown.is_set
        self.shared_runner.commands = CommandSettings(
            snapshot_root=paths.data_dir / "command-snapshots",
            # Loupe's credentials and state, plus the user's secret stores.
            protected=(
                paths.config_dir,
                paths.data_dir,
                paths.state_dir,
                paths.runtime_dir,
                *protected_home_paths(Path.home()),
            ),
            approval=settings.agent_commands,
        )
        # Registries separate the durable harness identity from the model
        # adapter selected for one launch. Provider construction does not open
        # a client or resolve credentials; that happens on first use.
        self.providers = ProviderRegistry()
        self.providers.register("anthropic", self._anthropic_provider)
        self.providers.register("openai", self._openai_provider)
        self.providers.register("codex", self._codex_provider)
        self.drivers = DriverRegistry()
        self.drivers.register(
            CodingAgentHarness.name,
            capabilities=CodingAgentHarness.capabilities,
            factory=self._coding_agent_driver,
        )
        self.drivers.register(
            FixtureWriteDriver.name,
            capabilities=FixtureWriteDriver.capabilities,
            factory=self._fixture_driver,
        )
        self.reconciler = ExecutionReconciler(
            self.store,
            self.coordinator,
            managed_worktree_root=paths.data_dir / "worktrees",
            boot_id=boot_id,
            driver_capabilities=self.drivers.capabilities,
        )
        self.startup_recovery: RecoveryReport | None = None
        self._authority_lock = asyncio.Lock()
        self._background_tasks: dict[tuple[str, int], asyncio.Task[None]] = {}
        # Model calls may block on network or user input for minutes. Keep
        # them off the default pool used by RPC, event streams and renewal.
        self._execution_pool = ThreadPoolExecutor(
            max_workers=32, thread_name_prefix="agent-execution"
        )
        # Reached from the worker thread and the event loop, so it is guarded
        # by a threading lock rather than the asyncio one.
        self._questions: dict[str, _PendingQuestion] = {}
        self._questions_lock = threading.Lock()
        self._session_recovery_done = False
        self._check_cleanup_ready_at = time.monotonic() + 5

    def initialize(self) -> None:
        self.paths.ensure()
        self.store.initialize()
        if not self._session_recovery_done:
            self.store.disconnect_active_sessions()
            with self.store.connection() as connection:
                connection.execute(
                    "UPDATE check_runs SET state='uncertain' WHERE state='running'"
                )
            # Each command's supervisor stops with its daemon, so no snapshot
            # left by an earlier boot can still be in use.
            cleanup_command_snapshots(self.paths.data_dir / "command-snapshots")
            self._session_recovery_done = True
        self._recover_shared_workspace_publications()
        with closing(connect_knowledge(self.paths.knowledge_db)) as connection:
            apply_migrations(connection, KNOWLEDGE_MIGRATIONS)
        with closing(connect_vectors(self.paths.vectors_db)) as connection:
            apply_migrations(connection, VECTOR_MIGRATIONS)
        # Nothing is served until every execution left behind by an earlier
        # boot has an outcome, so no session can observe a task that looks
        # runnable while a dead worker's reservation still blocks it.
        shared_recovery = self._recover_shared_executions()
        legacy_recovery = self.reconciler.reconcile()
        self.workflow.reconcile()
        self.startup_recovery = RecoveryReport(
            boot_id=self.boot_id,
            outcomes=shared_recovery + legacy_recovery.outcomes,
        )
        self._schedule_ready_launches()

    def _recover_shared_executions(self) -> tuple[RecoveryOutcome, ...]:
        with self.workspace_batches.lock:
            self._recover_shared_workspace_publications()
            self.workspace_batches.recover()
            return self.shared_runner.recover(
                self.drivers.capabilities, can_launch=self._can_reconstruct_launch
            )

    def _recover_shared_workspace_publications(self) -> None:
        """Resolve pre-replace journals strictly from the current path state."""

        for publication in self.store.list_recoverable_workspace_publications():
            candidate = self.store.get_workspace_candidate(publication.candidate_id)
            workspace = self.store.get_workspace(publication.workspace_id)
            if candidate is None or workspace is None:
                continue
            try:
                target = workspace_target(
                    Path(workspace.canonical_path), candidate.relative_path
                )
                current = identify_path(target)
                base = _candidate_identity(candidate, "base")
                result = _candidate_identity(candidate, "result")
                if _same_identity(current, result):
                    self.store.confirm_workspace_publication(
                        candidate_id=candidate.candidate_id
                    )
                elif _same_identity(current, base):
                    self.store.rollback_workspace_publication(
                        candidate_id=candidate.candidate_id
                    )
                else:
                    self.store.record_workspace_divergence(
                        candidate_id=candidate.candidate_id, current=current
                    )
            except (OSError, ScopeValidationError, ValueError):
                # The journal and candidate remain retained for a later, more
                # informed recovery. Startup must never overwrite a strange
                # filesystem state merely to make its own journal disappear.
                continue

    async def drain(self, timeout_ms: int | None = None) -> tuple[tuple[str, int], ...]:
        """Wait for in-process executions to finish, and report what did not.

        A worker runs on a thread, and cancelling the task awaiting it only
        stops watching -- the thread keeps going, potentially mid-``git``.
        Waiting is therefore the only clean way to end one.  Whatever is still
        running when the budget expires is left to boot recovery, which reads
        the same durable records the thread was writing.
        """

        pending = dict(self._background_tasks)
        if not pending:
            return ()
        budget = self.settings.shutdown_drain_ms if timeout_ms is None else timeout_ms
        _, unfinished = await asyncio.wait(
            set(pending.values()), timeout=budget / 1_000
        )
        abandoned = tuple(key for key, task in pending.items() if task in unfinished)
        for task_id, attempt in abandoned:
            with suppress(sqlite3.Error, KeyError, ValueError):
                self.store.record_task_event(
                    task_id=task_id,
                    claim_id=None,
                    event_type="execution.abandoned",
                    payload={"attempt": attempt, "reason": "daemon_shutdown"},
                )
        return abandoned

    def close(self) -> None:
        # Deliberately no cancellation: a task wrapping a worker thread cannot
        # be stopped by cancelling it, and pretending otherwise would hide the
        # work that outlives this process from the next boot's recovery.
        self._background_tasks.clear()
        self._execution_pool.shutdown(wait=False)

    async def reconcile_loop(self) -> None:
        interval = self.settings.reconciliation_interval_ms / 1_000
        while not self.shutdown.is_set():
            try:
                await asyncio.wait_for(self.shutdown.wait(), timeout=interval)
            except TimeoutError:
                async with self._authority_lock:
                    await asyncio.to_thread(self.coordinator.reconcile_expired)
                    await asyncio.to_thread(self.workflow.reconcile)
                    if time.monotonic() >= self._check_cleanup_ready_at:
                        from llm_cli.execution.checks import cleanup_interrupted_checks

                        await asyncio.to_thread(
                            cleanup_interrupted_checks,
                            self.store,
                            self.paths.data_dir / "check-worktrees",
                        )
                    self._schedule_ready_launches()

    async def handle(self, request: Request) -> Any:
        try:
            if request.method == "system.ping":
                return self._ping()
            if request.method == "system.shutdown":
                self.shutdown.set()
                return {"stopping": True, "boot_id": self.boot_id}
            if request.method == "task.attach":
                # Deliberately outside the authority lock. This generator lives
                # as long as the task does, and holding the lock for that would
                # stall every other session on the machine.
                return self._attach(
                    _string(request.params, "task_id"),
                    after=_non_negative_integer(request.params, "after", 0),
                    idle_timeout=_bounded_integer(
                        request.params,
                        "idle_timeout_ms",
                        300_000,
                        minimum=1_000,
                        maximum=3_600_000,
                    ),
                )
            if request.method == "session.attach":
                # Like task attachment, checkout replay must never pin the
                # service-wide authority lock while another terminal works.
                session = self._authenticated_session(request.params)
                return self._attach_session(
                    session,
                    after=_non_negative_integer(request.params, "after", 0),
                    idle_timeout=_bounded_integer(
                        request.params,
                        "idle_timeout_ms",
                        300_000,
                        minimum=1_000,
                        maximum=3_600_000,
                    ),
                )
            if request.method == "session.compact":
                # The summary is a model request. Hold the authority lock only
                # to read and to save, never while the provider responds.
                return await self._session_compact(request.params)
            async with self._authority_lock:
                return await self._dispatch(request)
        except LlmCoordError:
            raise
        except ScopeValidationError as exc:
            raise LlmCoordError(ErrorCode.SCOPE_VIOLATION, str(exc)) from exc
        except (ClaimAuthorityError, ClaimNotFound) as exc:
            raise LlmCoordError(ErrorCode.CLAIM_STALE, str(exc)) from exc
        except ClaimConflict as exc:
            raise LlmCoordError(ErrorCode.TASK_NOT_MUTABLE, str(exc)) from exc
        except KeyError as exc:
            raise LlmCoordError(
                ErrorCode.REPOSITORY_NOT_FOUND,
                "the requested local record was not found",
            ) from exc
        except ValueError as exc:
            raise LlmCoordError(ErrorCode.CONFIG_INVALID, str(exc)) from exc
        except CoordinationError as exc:
            raise LlmCoordError(ErrorCode.INTERNAL_RECOVERABLE, str(exc)) from exc
        except sqlite3.Error as exc:
            raise LlmCoordError(
                ErrorCode.INTERNAL_RECOVERABLE,
                "the local authority database could not complete the request",
            ) from exc

    def _task_view(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError("task")
        value = asdict(task)
        row = self.workflow.get(task_id)
        if row:
            value["agent_mode"] = row["agent_mode"]
            value["workflow_status"] = row["status"]
            if task.state == "reviewing":
                value["state"] = "awaiting_review"
            elif row["stop_requested"] and task.state not in _SETTLED_TASK_STATES:
                value["state"] = "stopping"
        return value

    async def _dispatch(self, request: Request) -> Any:
        method = request.method
        params = request.params
        if method == "system.init":
            self.initialize()
            return {"initialized": True, **self._ping(), "paths": self._safe_paths()}
        if method == "system.doctor":
            checks = await asyncio.to_thread(run_doctor, self.paths)
            return {
                "ok": all(
                    bool(check["ok"]) or bool(check["warning"]) for check in checks
                ),
                "checks": checks,
            }
        if method == "db.status":
            return self._db_status()
        if method == "db.check":
            return self._db_check()
        if method == "repo.add":
            return await self._repo_add(params)
        if method == "repo.list":
            return [asdict(record) for record in self.store.list_repositories()]
        if method == "repo.status":
            repository = await self._registered_repository_for_path(
                _path(params, "path")
            )
            return asdict(repository)
        if method == "session.open":
            return await self._session_open(params)
        if method == "session.set_mode":
            session = self._authenticated_session(params, require_active=True)
            updated = self.store.set_session_mode(
                session.session_id, validate_agent_mode(params.get("mode"))
            )
            return {"session": asdict(updated)}
        if method == "workspace.status":
            return await self._workspace_status(params)
        if method in {
            "workspace.read_file",
            "workspace.stage_file",
            "workspace.publish_candidate",
        }:
            # These three handlers do no asynchronous work. The same lock
            # gates worker-thread batches and manual broker reads/writes.
            with self.workspace_batches.lock:
                session = self._authenticated_session(params, require_active=True)
                if self.workspace_batches.blocks_workspace(session.workspace_id or ""):
                    raise LlmCoordError(
                        ErrorCode.CHECKOUT_RECOVERY_REQUIRED,
                        "the checkout has an unresolved multi-file publication",
                    )
                if method == "workspace.read_file":
                    return await self._workspace_read_file(params)
                if method == "workspace.stage_file":
                    return await self._workspace_stage_file(params)
                return await self._workspace_publish_candidate(params)
        if method == "session.resume":
            session = self._authenticated_session(params)
            resumed, event = self.store.resume_session(session.session_id)
            return {
                "session": asdict(resumed),
                "event": asdict(event) if event is not None else None,
                "cursor": _asdict_or_none(
                    self.store.get_session_cursor(session.session_id)
                ),
            }
        if method == "session.heartbeat":
            session = self._authenticated_session(params, require_active=True)
            return asdict(self.store.heartbeat_session(session.session_id))
        if method == "session.show":
            shown = self.store.get_session(_string(params, "session_id"))
            if shown is None:
                raise KeyError("session")
            return {
                "session": asdict(shown),
                "cursor": _asdict_or_none(
                    self.store.get_session_cursor(shown.session_id)
                ),
            }
        if method == "session.list":
            return await self._session_list(params)
        if method == "session.ack":
            session = self._authenticated_session(params)
            acknowledged, cursor, event = self.store.acknowledge_session(
                session.session_id,
                sequence=_acknowledged_sequence(params),
            )
            return {
                "session": asdict(acknowledged),
                "cursor": asdict(cursor),
                "event": asdict(event) if event is not None else None,
            }
        if method == "session.events":
            session = self._authenticated_session(params)
            return [
                asdict(event)
                for event in self.store.list_session_events(
                    session.session_id,
                    after_sequence=_non_negative_integer(params, "after", 0),
                    limit=_bounded_integer(
                        params, "limit", 100, minimum=1, maximum=500
                    ),
                )
            ]
        if method == "session.close":
            session = self._authenticated_session(params)
            closed, event = self.store.close_session(
                session.session_id,
                reason=_optional_string(params, "reason") or "operator_exit",
            )
            return {
                "session": asdict(closed),
                "event": asdict(event) if event is not None else None,
            }
        if method == "session.set_intent":
            session = self._authenticated_session(params, require_active=True)
            paths = params.get("paths")
            if not isinstance(paths, list) or not all(
                isinstance(path, str) for path in paths
            ):
                raise ValueError("paths must be a list of strings")
            intent, event, overlaps = self.store.set_session_intent(
                session.session_id,
                paths=tuple(paths),
                summary=_optional_string(params, "summary") or "",
            )
            return {
                "intent": asdict(intent),
                "event": asdict(event),
                "overlapping_session_ids": list(overlaps),
            }
        if method == "session.clear_intent":
            session = self._authenticated_session(params, require_active=True)
            cleared, cleared_event = self.store.clear_session_intent(session.session_id)
            return {
                "intent": _asdict_or_none(cleared),
                "event": _asdict_or_none(cleared_event),
            }
        if method == "session.intents":
            session = self._authenticated_session(params)
            return [
                asdict(intent)
                for intent in self.store.list_session_intents(
                    checkout_id=session.checkout_id,
                    active_only=bool(params.get("active_only", True)),
                )
            ]
        if method == "task.run":
            return await self._task_run(params)
        if method == "task.list":
            return [
                self._task_view(record.task_id) for record in self.store.list_tasks()
            ]
        if method in {
            "task.diff",
            "task.checks",
            "task.cancel",
            "task.apply",
            "task.undo",
        }:
            task_id = _string(params, "task_id")
            if method == "task.diff":
                return self.workflow.inspect(task_id)
            if method == "task.checks":
                return self.workflow.checks(task_id)
            if method == "task.cancel":
                with self.workspace_batches.lock:
                    workflow_result = self.workflow.cancel(task_id)
                with self._questions_lock:
                    pending = self._questions.get(task_id)
                    if pending:
                        pending.ready.set()
                self._schedule_ready_launches()
                return workflow_result
            workflow_result = await asyncio.to_thread(
                self.workflow.apply,
                task_id,
                undo=method == "task.undo",
                allow_unverified=bool(params.get("allow_unverified", False)),
            )
            self._schedule_ready_launches()
            return workflow_result
        if method in {"checks.configure", "checks.list"}:
            from llm_cli.execution.checks import validate_config

            repository = await self._registered_repository_for_path(
                _path(params, "path")
            )
            checkout = await self._registered_checkout_for_path(
                _path(params, "path"), repository
            )
            with self.store.connection() as connection:
                if method == "checks.configure":
                    raw = params.get("config")
                    if not isinstance(raw, dict):
                        raise ValueError("configuration must be an object")
                    config = validate_config(raw)
                    connection.execute(
                        (
                            "INSERT INTO check_configurations(checkout_id,config_jso"
                            "n) VALUES (?,?) ON CONFLICT(checkout_id) DO UPDATE SET "
                            "config_json=excluded.config_json"
                        ),
                        (checkout.checkout_id, json.dumps(config, sort_keys=True)),
                    )
                stored = connection.execute(
                    "SELECT config_json FROM check_configurations WHERE checkout_id=?",
                    (checkout.checkout_id,),
                ).fetchone()
            return json.loads(stored[0]) if stored else {"checks": {}}
        if method == "task.show":
            task = self.store.get_task(_string(params, "task_id"))
            if task is None:
                raise KeyError("task")
            return self._task_view(task.task_id)
        if method == "task.events":
            return self._task_events(request)
        if method == "task.retry":
            return asdict(self.store.begin_new_attempt(_string(params, "task_id")))
        if method == "task.discard":
            held = self.workflow.discard(_string(params, "task_id"))
            if held is not None:
                return held
            result = self.coordinator.discard_integration(_string(params, "task_id"))
            self._schedule_ready_launches()
            return asdict(result)
        if method == "task.recover":
            return await self._task_recover()
        if method == "task.question":
            return self._task_question(_string(params, "task_id"))
        if method == "task.answer":
            return self._task_answer(
                _string(params, "task_id"),
                _string(params, "answer"),
                question_id=_optional_string(params, "question_id"),
            )
        if method == "claim.list":
            repository = await self._registered_repository_for_path(
                _path(params, "path")
            )
            return [
                asdict(record) for record in self.store.list_claims(repository.repo_key)
            ]
        if method == "claim.show":
            claim = self.store.get_claim(_string(params, "claim_id"))
            if claim is None:
                raise ClaimNotFound("claim does not exist")
            return asdict(claim)
        if method == "claim.release":
            result = self.coordinator.release_claim(
                _string(params, "claim_id"), reason=_string(params, "reason")
            )
            self._schedule_ready_launches()
            return asdict(result)
        if method == "claim.renew":
            claim = self.coordinator.renew_claim(
                task_id=_string(params, "task_id"),
                claim_id=_string(params, "claim_id"),
                fencing_token=_integer(params, "fencing_token"),
                attempt=_integer(params, "attempt"),
            )
            return asdict(claim)
        if method == "claim.reconcile":
            reconciliation = self.coordinator.reconcile_expired()
            self._schedule_ready_launches()
            return asdict(reconciliation)
        raise LlmCoordError(
            ErrorCode.PROTOCOL_MISMATCH, f"unknown RPC method: {method}"
        )

    async def _repo_add(self, params: dict[str, Any]) -> dict[str, Any]:
        path = _path(params, "path")
        target = _optional_string(params, "target")
        coordinate_by_remote = params.get("coordinate_by_remote", True)
        if not isinstance(coordinate_by_remote, bool):
            raise ValueError("coordinate_by_remote must be a boolean")
        info = await asyncio.to_thread(
            inspect_repository,
            path,
            profile_id=self.paths.profile_id,
            target=target,
            coordinate_by_remote=coordinate_by_remote,
        )
        record = self.store.register_repository(
            repo_key=info.repo_key,
            display_name=info.display_name,
            git_common_dir=str(info.common_git_dir),
            main_worktree_path=str(info.main_worktree),
            target_ref=info.target_ref,
            profile_id=self.paths.profile_id,
            remote_identity=info.normalized_remote,
            object_format=info.object_format,
            integration_adapter=info.integration_adapter,
            coordination_mode=self.settings.coordination_mode,
            path_case_insensitive=info.path_case_insensitive,
            coordinate_by_remote=coordinate_by_remote,
        )
        checkout = self.store.ensure_checkout(
            repository=record,
            canonical_path=str(info.main_worktree.resolve()),
            git_common_dir=str(info.common_git_dir.resolve()),
        )
        return {
            **asdict(record),
            "checkout": asdict(checkout),
            "base_oid": info.base_oid,
        }

    async def _session_open(self, params: dict[str, Any]) -> dict[str, Any]:
        """Bind a durable conversation to one exact checkout and provider."""

        agent_mode = resolve_agent_mode(
            params.get("mode"), params.get("publish"), default="auto"
        )
        session_id = _string(params, "session_id")
        resume_token_hash = _string(params, "resume_token_hash")
        if len(resume_token_hash) != 64 or any(
            character not in "0123456789abcdef" for character in resume_token_hash
        ):
            raise ValueError("resume_token_hash must be a lowercase SHA-256 digest")
        repository = await self._registered_repository_for_path(_path(params, "path"))
        checkout = await self._registered_checkout_for_path(
            _path(params, "path"), repository
        )
        provider = _optional_string(params, "provider") or self.settings.agent_provider
        model = _optional_string(params, "model")
        effort = _optional_string(params, "effort")
        if model is None and provider == self.settings.agent_provider:
            model = self.settings.agent_model
        adapter = self.providers.create(provider, model, effort=effort)
        workspace_mode = _workspace_mode(params, self.settings.workspace_mode)
        if workspace_mode == "isolated":
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                "isolated workspaces are not implemented yet; they need worktree "
                "hydration of ignored runtime files, so the mode is refused "
                "rather than silently downgraded to shared",
            )
        workspace = await asyncio.to_thread(
            self.store.ensure_shared_workspace, checkout
        )
        session, event, bootstrap_sequence = self.store.open_session(
            session_id=session_id,
            checkout_id=checkout.checkout_id,
            resume_token_hash=resume_token_hash,
            provider=adapter.name,
            model=adapter.model,
            effort=effort,
            workspace_id=workspace.workspace_id,
            workspace_mode=workspace_mode,
            agent_mode=agent_mode,
        )
        return {
            "session": asdict(session),
            "checkout": asdict(checkout),
            "workspace": asdict(workspace),
            "bootstrap_event": asdict(event),
            "bootstrap_sequence": bootstrap_sequence,
        }

    async def _workspace_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Report the workspace for a checkout, including what it cannot promise.

        The guarantee limits are part of the status rather than buried in
        documentation: shared mode is only atomic for cooperative sessions that
        honor the publication barrier, and an operator deciding whether to
        trust a concurrent test run needs to see that where they are working.
        """

        repository = await self._registered_repository_for_path(_path(params, "path"))
        checkout = await self._registered_checkout_for_path(
            _path(params, "path"), repository
        )
        workspace = await asyncio.to_thread(
            self.store.ensure_shared_workspace, checkout
        )
        sessions = self.store.list_sessions(checkout_id=checkout.checkout_id)
        return {
            "workspace": asdict(workspace),
            "checkout": asdict(checkout),
            "active_session_ids": sorted(
                session.session_id
                for session in sessions
                if session.state in {"active", "opening"}
            ),
            "guarantees": _WORKSPACE_GUARANTEES,
        }

    async def _workspace_read_file(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read a bounded text file and issue the base identity for staging."""

        session = self._authenticated_session(params, require_active=True)
        relative_path = normalize_changed_path(_string(params, "relative_path"))
        workspace = _session_workspace(self.store, session)
        target = workspace_target(Path(workspace.canonical_path), relative_path)
        identity, raw = read_identified_path(target, max_bytes=MAX_SHARED_TEXT_BYTES)
        content: str | None = None
        if raw is not None:
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    "shared broker reads only UTF-8 text in this release"
                ) from exc
        self.store.record_workspace_read(
            session_id=session.session_id,
            relative_path=relative_path,
            identity=identity,
        )
        return {
            "workspace": asdict(workspace),
            "relative_path": relative_path,
            "identity": _identity_payload(identity),
            "content": content,
        }

    async def _workspace_stage_file(self, params: dict[str, Any]) -> dict[str, Any]:
        """Store a complete text replacement as a private candidate."""

        session = self._authenticated_session(params, require_active=True)
        candidate_id = _string(params, "candidate_id")
        relative_path = normalize_changed_path(_string(params, "relative_path"))
        base = _identity_from_payload(params.get("base"))
        text = params.get("content")
        if not isinstance(text, str):
            raise ValueError("content must be a string, including an empty string")
        content = text.encode("utf-8")
        executable = params.get("executable", base.mode == EXECUTABLE_MODE)
        if not isinstance(executable, bool):
            raise ValueError("executable must be a boolean")
        mode = EXECUTABLE_MODE if executable else REGULAR_MODE
        result = FileIdentity(
            kind=ObjectKind.REGULAR,
            mode=mode,
            digest=content_identity(ObjectKind.REGULAR, mode, content),
            size=len(content),
        )
        content_hash = store_candidate_content(self.paths.candidate_dir, content)
        candidate = self.store.stage_workspace_candidate(
            candidate_id=candidate_id,
            session_id=session.session_id,
            relative_path=relative_path,
            base=base,
            result=result,
            content_hash=content_hash,
            byte_count=len(content),
        )
        return {"candidate": asdict(candidate)}

    async def _workspace_publish_candidate(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Compare-and-swap one staged file under the short daemon barrier."""

        session = self._authenticated_session(params, require_active=True)
        candidate_id = _string(params, "candidate_id")
        candidate = self.store.get_workspace_candidate(candidate_id)
        if candidate is None:
            raise KeyError(f"candidate {candidate_id!r} does not exist")
        if candidate.session_id != session.session_id:
            raise ValueError("candidate belongs to another session")
        workspace = _session_workspace(self.store, session)
        if candidate.workspace_id != workspace.workspace_id:
            raise ValueError("candidate belongs to another workspace")
        target = workspace_target(
            Path(workspace.canonical_path), candidate.relative_path
        )
        base = _candidate_identity(candidate, "base")
        result = _candidate_identity(candidate, "result")

        if candidate.state == "published":
            publication = self.store.get_workspace_publication(candidate_id)
            assert publication is not None
            return {
                "outcome": "published",
                "candidate": asdict(candidate),
                "publication": asdict(publication),
                "workspace": asdict(workspace),
            }
        if candidate.state == "diverged":
            divergence = self.store.get_workspace_divergence(candidate_id)
            assert divergence is not None
            return {
                "outcome": "diverged",
                "candidate": asdict(candidate),
                "divergence": asdict(divergence),
            }

        # A response may have been lost after the rename but before it reached
        # the client. Reconcile the one prepared journal before making any new
        # decision; this is the same proof startup recovery uses.
        if candidate.state == "publishing":
            current = identify_path(target)
            if _same_identity(current, result):
                confirmed = self.store.confirm_workspace_publication(
                    candidate_id=candidate_id
                )
                return _published_workspace_response(*confirmed)
            if _same_identity(current, base):
                self.store.rollback_workspace_publication(candidate_id=candidate_id)
                candidate = self.store.get_workspace_candidate(candidate_id)
                assert candidate is not None
            else:
                divergence, event = self.store.record_workspace_divergence(
                    candidate_id=candidate_id, current=current
                )
                return _diverged_workspace_response(candidate, divergence, event)

        current = identify_path(target)
        if not _same_identity(current, base):
            divergence, event = self.store.record_workspace_divergence(
                candidate_id=candidate_id, current=current
            )
            return _diverged_workspace_response(candidate, divergence, event)

        content = load_candidate_content(
            self.paths.candidate_dir, candidate.content_hash
        )
        if (
            len(content) != candidate.byte_count
            or content_identity(ObjectKind.REGULAR, result.mode, content)
            != result.digest
        ):
            raise LlmCoordError(
                ErrorCode.CHECKOUT_RECOVERY_REQUIRED,
                "candidate content no longer matches its durable identity",
            )
        candidate, publication, workspace = self.store.begin_workspace_publication(
            candidate_id=candidate_id, session_id=session.session_id
        )
        if publication.operation_state == "confirmed":
            return _published_workspace_response(
                candidate,
                publication,
                workspace,
                _published_event(self.store, publication),
            )
        if publication.operation_state != "prepared":
            raise LlmCoordError(
                ErrorCode.CHECKOUT_RECOVERY_REQUIRED,
                "candidate publication needs an explicit recovery decision",
            )
        current = identify_path(target)
        if not _same_identity(current, base):
            divergence, event = self.store.record_workspace_divergence(
                candidate_id=candidate_id, current=current
            )
            return _diverged_workspace_response(candidate, divergence, event)
        try:
            atomic_replace_regular_file(
                checkout_root=Path(workspace.canonical_path),
                git_common_dir=Path(workspace.git_dir or ""),
                relative_path=candidate.relative_path,
                content=content,
                mode=result.mode,
                publication_id=publication.publication_id,
            )
        except WorkspaceBrokerError:
            # If no target effect began, recovery is unnecessary: preserve the
            # candidate as retryable rather than stranding a prepared journal.
            if _same_identity(identify_path(target), base):
                self.store.rollback_workspace_publication(candidate_id=candidate_id)
            raise
        observed = identify_path(target)
        if _same_identity(observed, result):
            confirmed = self.store.confirm_workspace_publication(
                candidate_id=candidate_id
            )
            return _published_workspace_response(*confirmed)
        if not _same_identity(observed, base):
            divergence, event = self.store.record_workspace_divergence(
                candidate_id=candidate_id, current=observed
            )
            return _diverged_workspace_response(candidate, divergence, event)
        raise LlmCoordError(
            ErrorCode.CHECKOUT_RECOVERY_REQUIRED,
            "atomic publication left the candidate outcome unresolved",
        )

    async def _session_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        state = _optional_string(params, "state")
        path = params.get("path")
        if path is None:
            return [
                asdict(session) for session in self.store.list_sessions(state=state)
            ]
        checkout = await self._registered_checkout_for_path(_path(params, "path"))
        return [
            asdict(session)
            for session in self.store.list_sessions(
                checkout_id=checkout.checkout_id, state=state
            )
        ]

    def _authenticated_session(
        self, params: Mapping[str, Any], *, require_active: bool = False
    ) -> SessionRecord:
        session_id = _string(dict(params), "session_id")
        secret = _string(dict(params), "resume_secret")
        try:
            session = self.store.authenticate_session(session_id, secret)
        except ValueError as exc:
            raise LlmCoordError(
                ErrorCode.SESSION_AUTH_REQUIRED,
                "session credentials were rejected",
            ) from exc
        if require_active and session.state != "active":
            raise LlmCoordError(
                ErrorCode.SESSION_NOT_ACTIVE,
                "the session has not completed its bootstrap or must be resumed",
                details={"state": session.state},
            )
        return session

    async def _registered_checkout_for_path(
        self,
        path: Path,
        repository: RepositoryRecord | None = None,
    ) -> CheckoutRecord:
        """Resolve an invoked working tree to its additive physical checkout row."""

        info: RepositoryInfo = await asyncio.to_thread(
            inspect_repository, path, profile_id=self.paths.profile_id
        )
        record = repository or await self._registered_repository_for_path(path)
        return self.store.ensure_checkout(
            repository=record,
            canonical_path=str(info.main_worktree.resolve()),
            git_common_dir=str(info.common_git_dir.resolve()),
        )

    def _execution_repository(
        self, repository: RepositoryRecord, task: TaskRecord
    ) -> RepositoryRecord:
        """Use a session's exact checkout instead of a legacy mutable path."""

        if task.session_id is None:
            return repository
        session = self.store.get_session(task.session_id)
        if session is None:
            raise ValueError("task references a missing session")
        checkout = self.store.get_checkout(session.checkout_id)
        if checkout is None or checkout.repository_id != repository.repository_id:
            raise ValueError("task session checkout is no longer valid")
        return replace(repository, main_worktree_path=checkout.canonical_path)

    async def _task_run(self, params: dict[str, Any]) -> dict[str, Any]:
        requested_path = _path(params, "path")
        repository = await self._registered_repository_for_path(requested_path)
        session_id = _optional_string(params, "session_id")
        session: SessionRecord | None = None
        execution_repository = repository
        if session_id is not None:
            session = self._authenticated_session(params, require_active=True)
            if session.session_id != session_id:
                raise LlmCoordError(
                    ErrorCode.SESSION_AUTH_REQUIRED,
                    "the supplied session credentials do not match the task session",
                )
            checkout = await self._registered_checkout_for_path(
                requested_path, repository
            )
            if checkout.checkout_id != session.checkout_id:
                raise ValueError("task path is outside the session's selected checkout")
            # Legacy records can still point at another clone sharing the same
            # remote identity. A session-associated task is never allowed to
            # inherit that mutable path; it executes against its exact checkout.
            execution_repository = replace(
                repository, main_worktree_path=checkout.canonical_path
            )
        if "mode" in params:
            requested_mode = validate_agent_mode(params["mode"])
            if session is None:
                raise ValueError("agent modes require a shared session")
            if requested_mode != session.agent_mode:
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID,
                    "task mode does not match the session's current mode",
                    {"agent_mode": session.agent_mode},
                )
        scopes = params.get("scopes")
        if not isinstance(scopes, list) or not all(
            isinstance(item, str) for item in scopes
        ):
            raise ValueError("scopes must be a list of strings")
        fixture_values = params.get("fixture_writes", [])
        if not isinstance(fixture_values, list) or not all(
            isinstance(item, str) for item in fixture_values
        ):
            raise ValueError("fixture_writes must be a list of PATH=CONTENT strings")
        fixture_writes = parse_fixture_writes(fixture_values) if fixture_values else ()
        task_id = _optional_string(params, "task_id")
        # A task that has been retried is on a later attempt, so it must be
        # reused rather than recreated: creation is pinned to attempt 1 and
        # would reject its own task ID as belonging to another generation.
        existing_task = self.store.get_task(task_id) if task_id else None
        prior_launch = (
            self.store.get_execution_launch(
                existing_task.task_id, existing_task.attempt
            )
            if existing_task is not None
            else None
        )
        if session_id is not None:
            with self.store.connection() as connection:
                active = connection.execute(
                    """SELECT t.task_id FROM tasks t JOIN claims c
                        ON c.claim_id = t.current_claim_id
                        WHERE t.session_id = ? AND t.task_id <> ?
                        AND c.state IN (
                            'queued','active_work','publishing','active_integration')
                        LIMIT 1""",
                    (session_id, task_id or ""),
                ).fetchone()
            if active is not None:
                raise LlmCoordError(
                    ErrorCode.TASK_NOT_MUTABLE,
                    "finish the session's current task before another prompt",
                    {"active_task_id": str(active["task_id"])},
                )
        claim_only = bool(params.get("claim_only", False))
        if session is not None and session.agent_mode != "auto" and (
            fixture_values or claim_only
        ):
            raise ValueError("plan and normal modes require the coding-agent driver")
        interactive = params.get("interactive", False)
        if not isinstance(interactive, bool):
            raise ValueError("interactive must be a boolean")
        launch: ExecutionLaunchRecord | None = None
        if not claim_only:
            provider_name = _optional_string(params, "provider")
            model_name = _optional_string(params, "model")
            effort_name = _optional_string(params, "effort")
            if fixture_values and any(
                value is not None for value in (provider_name, model_name, effort_name)
            ):
                raise ValueError(
                    "provider, model, and effort cannot be combined with fixture writes"
                )
            driver_name = (
                FixtureWriteDriver.name if fixture_values else CodingAgentHarness.name
            )
            if fixture_values:
                launch_parameters: dict[str, object] = {
                    "writes": [
                        {"path": write.path, "content": write.content}
                        for write in fixture_writes
                    ]
                }
            else:
                selected_provider = provider_name or (
                    session.provider
                    if session is not None
                    else self.settings.agent_provider
                )
                selected_model = (
                    model_name
                    if model_name is not None
                    else (
                        session.model
                        if session is not None
                        else (
                            self.settings.agent_model
                            if selected_provider == self.settings.agent_provider
                            else None
                        )
                    )
                )
                selected_effort = (
                    effort_name
                    if "effort" in params
                    else session.effort
                    if session is not None
                    else None
                )
                if session is not None and (
                    selected_provider != session.provider
                    or selected_model != session.model
                    or selected_effort != session.effort
                ):
                    raise ValueError(
                        "a session task must use the provider and model "
                        "it opened with, "
                        "including its effort level"
                    )
                # Adapter construction is side-effect free. Resolve it before
                # acquiring a claim so a typo cannot become a stuck waiter.
                self.providers.create(
                    selected_provider, selected_model, effort=selected_effort
                )
                launch_parameters = {
                    "provider": selected_provider,
                    "model": selected_model,
                    **(
                        {"effort": selected_effort}
                        if selected_effort is not None
                        else {}
                    ),
                }
                if session is not None and (
                    prior_launch is None or "agent_mode" in prior_launch.parameters
                ):
                    launch_parameters["agent_mode"] = (
                        prior_launch.parameters["agent_mode"]
                        if prior_launch is not None
                        else session.agent_mode
                    )
        # Validate the launch before reserving the session's next task slot;
        # refused model/effort settings must leave it usable for another prompt.
        if existing_task is not None:
            if existing_task.repository_id != repository.repository_id:
                raise ValueError("task ID is already bound to another repository")
            if existing_task.session_id != session_id:
                raise ValueError("task ID is already bound to another session")
            task = existing_task
        else:
            task = self.store.create_task(
                repository_id=repository.repository_id,
                task_id=task_id,
                title=_string(params, "title")[:500],
                coordination_mode=self.settings.coordination_mode,
                session_id=session_id,
            )
        if not claim_only:
            launch = self.store.save_execution_launch(
                task_id=task.task_id,
                attempt=task.attempt,
                driver=driver_name,
                instructions=(_optional_string(params, "title") or task.title)[:4000],
                interactive=interactive,
                parameters=launch_parameters,
            )
        if session is not None and not claim_only:
            self.workflow.ensure(task.task_id)
        driver = self._driver_for_launch(launch) if launch is not None else None
        prior_claim = (
            self.store.get_claim(task.current_claim_id)
            if task.current_claim_id is not None
            else None
        )
        # Mode belongs to the durable attempt. Old waiters retain their FIFO
        # reservation, and a repeated request cannot upgrade an existing claim.
        scheduling_mode = "exclusive"
        optimistic_driver = None
        if prior_claim is not None and prior_claim.task_attempt == task.attempt:
            scheduling_mode = prior_claim.scheduling_mode
        elif (
            session is not None
            and session.workspace_mode == "shared"
            and driver is not None
            and self._shared_capable(driver)
        ):
            scheduling_mode = "optimistic"
            optimistic_driver = driver.name
        claim = self.coordinator.request_claim(
            task.task_id,
            scopes,
            scheduling_mode=scheduling_mode,
            optimistic_driver=optimistic_driver,
        )
        # ask_user is offered only when the operator opted into interaction. A
        # background run must never be able to park on a question nobody is
        # there to answer, and attaching alone races the driver's start.
        if claim.state.value == "active_work" and launch is not None:
            assert driver is not None
            scheduled, event = self._schedule_execution(
                repository=execution_repository,
                task=task,
                claim=claim,
                driver=driver,
                instructions=launch.instructions,
                interactive=launch.interactive,
            )
            self._schedule_ready_launches()
            return {
                "task": self._task_view(task.task_id),
                "claim": asdict(self.store.get_claim(claim.claim_id) or claim),
                "execution": scheduled,
                "driver": driver.name,
                "event_sequence": event.sequence,
                "notice": "poll 'llm-coord task events TASK_ID' for lifecycle changes",
            }
        self._schedule_ready_launches()
        return {
            "task": self._task_view(task.task_id),
            "claim": asdict(claim),
            "execution": "queued" if claim.state.value == "queued" else "not_started",
            "notice": (
                "the claim is queued behind overlapping work; the daemon will start it "
                "when it becomes active"
                if claim.state.value == "queued" and launch is not None
                else "the claim is held; no driver was started"
            ),
        }

    def _task_events(self, request: Request) -> list[dict[str, Any]]:
        """Page complete events without overflowing a single RPC response.

        A transcript page is bounded by bytes as well as event count. Clients
        continue after the last returned sequence until a page is empty; a
        short page does not imply that all history has been delivered.
        """

        params = request.params
        events = self.store.list_task_events(
            _string(params, "task_id"),
            after_sequence=_non_negative_integer(params, "after_sequence", 0),
            limit=_bounded_integer(params, "limit", 100, minimum=1, maximum=500),
        )
        # Match framing's UTF-8/JSON encoding, including escaped controls and
        # request identity. Reserve room for the server's numeric revision.
        envelope = Response(request_id=request.request_id, ok=True, result=[])
        used = (
            len(
                json.dumps(
                    envelope.to_dict(), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
            + 64
        )
        page: list[dict[str, Any]] = []
        for event in events:
            item = asdict(event)
            size = len(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            ) + bool(page)
            if used + size > DEFAULT_MAX_FRAME_BYTES:
                if not page:
                    raise LlmCoordError(
                        ErrorCode.CONTEXT_TOO_LARGE,
                        "a stored task event exceeds the local RPC frame limit",
                        {"sequence": event.sequence},
                    )
                break
            page.append(item)
            used += size
        return page

    async def _attach(
        self, task_id: str, *, after: int, idle_timeout: int
    ) -> AsyncIterator[dict[str, Any]]:
        """Replay durable events after ``after``, then follow the task live.

        The durable event table is the only source, so a reconnecting session
        replays from its last sequence and misses nothing -- no subscription
        state on the daemon, and no difference between attaching before the
        work starts and attaching in the middle of it.
        """

        if self.store.get_task(task_id) is None:
            raise LlmCoordError(
                ErrorCode.REPOSITORY_NOT_FOUND, f"task {task_id!r} does not exist"
            )
        cursor = after
        idle_deadline = time.monotonic() + idle_timeout / 1_000
        while True:
            # Observe settlement before reading. A worker writes its final
            # events (including the answer) and then settles, so checking only
            # after an empty read could end the stream just before those events.
            settled = self._task_is_settled(task_id)
            events = await asyncio.to_thread(
                functools.partial(
                    self.store.list_task_events,
                    task_id,
                    after_sequence=cursor,
                    limit=_ATTACH_BATCH,
                )
            )
            for event in events:
                cursor = event.sequence
                yield asdict(event)
            if events:
                idle_deadline = time.monotonic() + idle_timeout / 1_000
                continue
            if settled:
                return
            if time.monotonic() >= idle_deadline or self.shutdown.is_set():
                return
            await asyncio.sleep(_ATTACH_POLL_SECONDS)

    async def _attach_session(
        self, session: SessionRecord, *, after: int, idle_timeout: int
    ) -> AsyncIterator[dict[str, Any]]:
        """Replay one checkout's durable events for a session attachment."""

        cursor = after
        idle_deadline = time.monotonic() + idle_timeout / 1_000
        while not self.shutdown.is_set():
            events = await asyncio.to_thread(
                self.store.list_session_events,
                session.session_id,
                after_sequence=cursor,
                limit=_ATTACH_BATCH,
            )
            for event in events:
                cursor = event.sequence
                yield asdict(event)
            if events:
                idle_deadline = time.monotonic() + idle_timeout / 1_000
                continue
            if time.monotonic() >= idle_deadline:
                return
            await asyncio.sleep(_ATTACH_POLL_SECONDS)

    def _task_is_settled(self, task_id: str) -> bool:
        """Report whether anything can still produce events for this task.

        A task is only settled once no worker is running it in this process
        either: the terminal state is written before the last events land, so
        checking state alone would truncate the stream at its final moment.
        """

        task = self.store.get_task(task_id)
        if task is None or task.state not in _SETTLED_TASK_STATES:
            return False
        return not any(
            not background.done()
            for (running_id, _), background in self._background_tasks.items()
            if running_id == task_id
        )

    def _task_question(self, task_id: str) -> dict[str, Any]:
        """Return the live question independently of historical replay."""

        if self.store.get_task(task_id) is None:
            raise KeyError("task")
        with self._questions_lock:
            pending = self._questions.get(task_id)
            return {
                "task_id": task_id,
                "pending": pending is not None,
                "question_id": pending.question_id if pending is not None else None,
                "question": pending.question if pending is not None else None,
            }

    def _task_answer(
        self, task_id: str, answer: str, *, question_id: str | None = None
    ) -> dict[str, Any]:
        """Deliver an operator's answer to a worker blocked on ``ask_user``."""

        with self._questions_lock:
            pending = self._questions.get(task_id)
            if pending is None:
                raise LlmCoordError(
                    ErrorCode.TASK_NOT_MUTABLE,
                    "that task is not waiting for an answer",
                )
            if question_id is not None and question_id != pending.question_id:
                raise LlmCoordError(
                    ErrorCode.TASK_NOT_MUTABLE,
                    "that question is no longer waiting for an answer",
                )
            self._questions.pop(task_id)
        pending.answer = answer[:_MAX_ANSWER_CHARACTERS]
        with suppress(sqlite3.Error, KeyError, ValueError):
            self.store.record_task_event(
                task_id=task_id,
                claim_id=None,
                event_type="question.answered",
                payload={
                    "question_id": pending.question_id,
                    "characters": len(pending.answer),
                },
            )
        # Record resolution before waking the worker, which may ask again.
        pending.ready.set()
        return {
            "delivered": True,
            "task_id": task_id,
            "question_id": pending.question_id,
        }

    def _ask_operator(self, task_id: str, question: str) -> str:
        """Publish a model question and block the worker until it is answered.

        This runs on the worker thread, so the wait is a plain threading
        primitive.  It is bounded: an operator who closes the terminal must not
        leave a claim held by a worker parked forever on a prompt.
        """

        pending = _PendingQuestion(question=question[:MAX_QUESTION_CHARACTERS])
        with self._questions_lock:
            # Pair registration with task.cancel's wakeup: cancellation may
            # arrive after the broker's tool-entry check but before this point.
            # If it arrives after this check, it observes and wakes this entry.
            workflow = self.workflow.get(task_id)
            if workflow and workflow["stop_requested"]:
                raise TaskCancelled("task stopped before waiting for an answer")
            self._questions[task_id] = pending
        self.store.record_task_event(
            task_id=task_id,
            claim_id=None,
            event_type="question.asked",
            payload={
                "question_id": pending.question_id,
                "question": pending.question,
            },
        )
        answered = pending.ready.wait(timeout=_ANSWER_TIMEOUT_SECONDS)
        with self._questions_lock:
            if self._questions.get(task_id) is pending:
                self._questions.pop(task_id)
        if not answered:
            with suppress(sqlite3.Error, KeyError, ValueError):
                self.store.record_task_event(
                    task_id=task_id,
                    claim_id=None,
                    event_type="question.unanswered",
                    payload={
                        "question_id": pending.question_id,
                        "timeout_seconds": _ANSWER_TIMEOUT_SECONDS,
                    },
                )
            return (
                "No operator answered within the time limit. Do not wait again; "
                "do not invent an answer. If the missing information is required, "
                "call finish_task and explain what is still needed. Otherwise "
                "continue only with work that does not depend on this answer."
            )
        return pending.answer

    async def _task_recover(self) -> dict[str, Any]:
        """Resolve every execution an earlier boot abandoned, on demand.

        Startup already runs this pass.  Rerunning it matters after an operator
        repairs whatever made an outcome unreadable -- a repository that was on
        an unmounted volume, say -- because the intents it could not decide
        then are still waiting for a decision now.
        """

        shared = await asyncio.to_thread(self._recover_shared_executions)
        legacy = await asyncio.to_thread(self.reconciler.reconcile)
        report = RecoveryReport(boot_id=self.boot_id, outcomes=shared + legacy.outcomes)
        self._schedule_ready_launches()
        self.workflow.reconcile()
        return _recovery_payload(report)

    def _schedule_ready_launches(self) -> None:
        """Start every durable recipe whose claim has become active.

        This is deliberately a scan of durable state rather than an in-memory
        notification. The same method handles a release, lease reconciliation,
        and daemon restart; duplicate scans are harmless because the
        background-task registry makes scheduling idempotent.
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # ``initialize`` is also used by synchronous inspection helpers.
            # They establish durable state but do not own an event loop that
            # could supervise work; the real daemon calls this again on boot.
            return
        for launch in self.store.list_execution_launches():
            task = self.store.get_task(launch.task_id)
            if task is None or task.attempt != launch.task_attempt:
                continue
            if task.current_claim_id is None:
                continue
            claim = self.store.get_claim(task.current_claim_id)
            if (
                claim is None
                or claim.task_attempt != task.attempt
                or claim.state.value != "active_work"
            ):
                continue
            execution = self.store.get_execution(task.task_id, task.attempt)
            if (
                execution is not None
                and execution.workspace_id is not None
                and execution.state != "running"
            ):
                # Recovery owns unresolved shared executions. In particular,
                # a malformed checkpoint marked operator_attention must not
                # be relaunched and terminalized as an ordinary launch failure.
                continue
            repository = self.store.get_repository_by_key(task.repo_key)
            if repository is None:
                continue
            key = (task.task_id, task.attempt)
            existing = self._background_tasks.get(key)
            if existing is not None and not existing.done():
                continue
            try:
                driver = self._driver_for_launch(launch)
            except Exception as exc:
                # Factories are extension code: missing SDKs and configuration
                # failures are local to this recipe, not daemon failures.
                self._fail_unstarted_launch(task, claim, exc)
                continue
            try:
                repository = self._execution_repository(repository, task)
                self._schedule_execution(
                    repository=repository,
                    task=task,
                    claim=claim,
                    driver=driver,
                    instructions=launch.instructions,
                    interactive=launch.interactive,
                )
            except (ValueError, KeyError, LlmCoordError, CoordinationError) as exc:
                # One unavailable provider must not abort startup or keep
                # unrelated durable launches from getting their turn.
                self._fail_unstarted_launch(task, claim, exc)

    def _driver_for_launch(self, launch: ExecutionLaunchRecord) -> AgentDriver:
        return self.drivers.create(launch.driver, launch.parameters)

    def _can_reconstruct_launch(self, launch: ExecutionLaunchRecord) -> bool:
        try:
            self._driver_for_launch(launch)
        except Exception:
            return False
        return True

    @staticmethod
    def _shared_capable(driver: AgentDriver) -> bool:
        return (
            driver.capabilities.shared_workspace
            and driver.capabilities.resumable
            and driver.capabilities.enforces_scope
        )

    def _fail_unstarted_launch(
        self, task: TaskRecord, claim: ClaimRecord, error: Exception
    ) -> None:
        """Retire private work only when no checkout journal can be lost."""

        code = (
            error.code.value if isinstance(error, LlmCoordError) else "EXECUTION_FAILED"
        )
        with self.workspace_batches.lock, suppress(CoordinationError):
            execution = self.store.get_execution(task.task_id, task.attempt)
            if execution is None:
                self.coordinator.release_claim(
                    claim.claim_id,
                    reason="execution_launch_unavailable",
                    expected_fencing_token=claim.fencing_token,
                    task_state="failed",
                    failure_code=code,
                )
            elif execution.workspace_id is not None and execution.state not in {
                "published",
                "failed",
            }:
                batch = self.workspace_batches.get(execution.execution_id)
                if batch is None:
                    self.coordinator.settle_shared_execution(
                        execution.execution_id, outcome="failed_safe", failure_code=code
                    )
                # Journaled work belongs to recovery; driver availability says
                # nothing about whether its filesystem effect was completed.
        payload: dict[str, object] = {"failure_code": code}
        if (
            isinstance(error, LlmCoordError)
            and error.code is ErrorCode.PROVIDER_UNAVAILABLE
            and error.details is not None
        ):
            status = error.details.get("status_code")
            if type(status) is int and 100 <= status <= 599:
                # Provider response text can echo a private prompt. Only the
                # numeric status and fixed classifications belong in replay.
                payload["provider_status"] = status
            category = error.details.get("provider_error")
            if isinstance(category, str) and category in _PUBLIC_PROVIDER_ERRORS:
                payload["provider_error"] = category
        self.store.record_task_event(
            task_id=task.task_id,
            claim_id=claim.claim_id,
            event_type="execution.background_failed",
            payload=payload,
        )

    def _codex_provider(
        self, model: str | None, *, effort: str | None = None
    ) -> CodexProvider:
        return (
            CodexProvider(paths=self.paths, effort=effort)
            if model is None
            else CodexProvider(paths=self.paths, model=model, effort=effort)
        )

    def _openai_provider(
        self, model: str | None, *, effort: str | None = None
    ) -> OpenAIProvider:
        return (
            OpenAIProvider(paths=self.paths, effort=effort)
            if model is None
            else OpenAIProvider(paths=self.paths, model=model, effort=effort)
        )

    def _anthropic_provider(
        self, model: str | None, *, effort: str | None = None
    ) -> AnthropicProvider:
        if model is None:
            return AnthropicProvider(paths=self.paths, effort=effort)
        return AnthropicProvider(
            paths=self.paths, model=model, effort=effort, fallback_model=None
        )

    def _coding_agent_driver(
        self, parameters: Mapping[str, object]
    ) -> CodingAgentHarness:
        if not {"provider", "model"} <= set(parameters) or set(parameters) - {
            "provider", "model", "effort", "agent_mode"
        }:
            raise ValueError("the coding-agent launch recipe is malformed")
        if "agent_mode" in parameters:
            validate_agent_mode(parameters["agent_mode"])
        provider = parameters.get("provider")
        model = parameters.get("model")
        effort = parameters.get("effort")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("the coding-agent launch has no provider")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("the coding-agent launch model is malformed")
        if effort is not None and not isinstance(effort, str):
            raise ValueError("the coding-agent launch effort is malformed")
        return CodingAgentHarness(self.providers.create(provider, model, effort=effort))

    @staticmethod
    def _fixture_driver(parameters: Mapping[str, object]) -> FixtureWriteDriver:
        if set(parameters) != {"writes"}:
            raise ValueError("the fixture launch recipe is malformed")
        raw_writes = parameters.get("writes")
        if not isinstance(raw_writes, list) or not raw_writes:
            raise ValueError("the fixture launch recipe has no writes")
        parsed: list[FixtureWrite] = []
        for raw_write in raw_writes:
            if not isinstance(raw_write, dict):
                raise ValueError("the fixture launch recipe is malformed")
            path = raw_write.get("path")
            content = raw_write.get("content")
            if not isinstance(path, str) or not isinstance(content, str):
                raise ValueError("the fixture launch recipe is malformed")
            parsed.append(FixtureWrite(path=path, content=content))
        return FixtureWriteDriver(tuple(parsed))

    def _schedule_execution(
        self,
        *,
        repository: RepositoryRecord,
        task: TaskRecord,
        claim: ClaimRecord,
        driver: AgentDriver,
        instructions: str = "",
        interactive: bool = False,
    ) -> tuple[str, TaskEventRecord]:
        """Start one background execution without holding the RPC authority lock."""

        key = (task.task_id, task.attempt)
        existing = self._background_tasks.get(key)
        if existing is not None and not existing.done():
            event = self.store.record_task_event(
                task_id=task.task_id,
                claim_id=claim.claim_id,
                event_type="execution.schedule_observed",
                payload={"attempt": task.attempt},
            )
            return "already_scheduled", event
        event = self.store.record_task_event(
            task_id=task.task_id,
            claim_id=claim.claim_id,
            event_type="execution.scheduled",
            payload={"attempt": task.attempt, "driver": driver.name},
        )
        background = asyncio.create_task(
            self._run_execution(
                repository=repository,
                task=task,
                claim=claim,
                driver=driver,
                instructions=instructions,
                interactive=interactive,
            ),
            name=f"execution:{task.task_id}:{task.attempt}",
        )
        self._background_tasks[key] = background
        background.add_done_callback(
            lambda completed: self._forget_background_task(key, completed)
        )
        return "scheduled", event

    def _forget_background_task(
        self, key: tuple[str, int], completed: asyncio.Task[None]
    ) -> None:
        if self._background_tasks.get(key) is completed:
            self._background_tasks.pop(key, None)

    async def _run_execution(
        self,
        *,
        repository: RepositoryRecord,
        task: TaskRecord,
        claim: ClaimRecord,
        driver: AgentDriver,
        instructions: str = "",
        interactive: bool = False,
    ) -> None:
        """Run outside the request lock and surface failure to every session."""

        try:
            operation = functools.partial(
                contextvars.copy_context().run,
                self._execute,
                repository=repository,
                task=task,
                claim=claim,
                driver=driver,
                instructions=instructions,
                interactive=interactive,
            )
            await asyncio.get_running_loop().run_in_executor(
                self._execution_pool, operation
            )
            if task.session_id is not None:
                await asyncio.to_thread(self._settle_session_task, task)
        except asyncio.CancelledError:
            self.store.record_task_event(
                task_id=task.task_id,
                claim_id=claim.claim_id,
                event_type="execution.interrupted",
                payload={"reason": "daemon_shutdown"},
            )
            raise
        except Exception as exc:
            self._fail_unstarted_launch(task, claim, exc)

    def _settle_session_task(self, task: TaskRecord) -> None:
        """End a session task's reservation once its result is safely published.

        A durable session keeps the conversation; its tasks must not keep write
        authority between prompts.  Only a confirmed publication is settled --
        a run that failed has already released, and one whose outcome is
        unknown must stay blocking, so both raise here and are ignored.
        """

        with suppress(CoordinationError):
            self.coordinator.settle_publication(task.task_id)

    def _execute(
        self,
        *,
        repository: RepositoryRecord,
        task: TaskRecord,
        claim: ClaimRecord,
        driver: AgentDriver,
        instructions: str,
        interactive: bool = False,
    ) -> None:
        """Dispatch to the driver, honoring a substituted fixture runner.

        The fixture path stays routed through ``fixture_runner`` so a test can
        replace that one attribute and control an execution's timing without
        reaching into the general lifecycle.
        """

        existing = self.store.get_execution(task.task_id, task.attempt)
        if claim.scheduling_mode == "optimistic" and (
            task.session_id is None
            or not self._shared_capable(driver)
            or claim.workspace_id is None
            or (existing is not None and existing.workspace_id != claim.workspace_id)
        ):
            raise ClaimConflict("optimistic claims require their shared execution path")
        if isinstance(driver, FixtureWriteDriver):
            self.fixture_runner.execute(
                repository=repository,
                task=task,
                claim=claim,
                writes=driver.writes,
            )
            return
        conversation_state, coordination_context = self._session_task_context(task)
        if (
            task.session_id is not None
            and self._shared_capable(driver)
            and (existing is None or existing.workspace_id is not None)
        ):
            self.shared_runner.execute(
                repository=repository,
                task=task,
                claim=claim,
                driver=driver,
                instructions=instructions,
                asker=(
                    functools.partial(self._ask_operator, task.task_id)
                    if interactive
                    else None
                ),
                conversation_state=conversation_state,
                coordination_context=coordination_context,
            )
            return
        result = self.runner.execute(
            repository=repository,
            task=task,
            claim=claim,
            driver=driver,
            instructions=instructions,
            asker=(
                functools.partial(self._ask_operator, task.task_id)
                if interactive
                else None
            ),
            conversation_state=conversation_state,
            coordination_context=coordination_context,
            cancelled=lambda: self.workflow.stopped(task.task_id, task.attempt),
        )
        # A no-change isolated run promotes its conversation in the same
        # transaction that releases the claim. Re-saving it after worktree
        # cleanup can race a newly admitted prompt and overwrite newer context.
        # Mutating isolated runs still hold their publication reservation here,
        # so save their conversation before `_settle_session_task` releases it.
        if result.publication_intent is not None:
            self._save_completed_session_conversation(
                task, result.execution.execution_id
            )

    def _session_task_context(
        self, task: TaskRecord
    ) -> tuple[Mapping[str, object] | None, str | None]:
        """Supply prior native context plus bounded advisory path awareness."""

        if task.session_id is None:
            return None, None
        session = self.store.get_session(task.session_id)
        if session is None:
            raise ValueError("task references a missing durable session")
        prior = self.store.session_conversation(session.session_id)
        conversation: Mapping[str, object] | None = None
        if prior is not None:
            provider, model, snapshot = prior
            if provider != session.provider or model != session.model:
                raise ValueError("stored session conversation provider/model mismatch")
            conversation = snapshot
        if session.workspace_mode == "shared":
            # The shared runner refreshes this at each model request, including
            # resumed requests. Avoid appending a second, launch-only snapshot.
            return conversation, None
        lines: list[str] = []
        workspace = self.store.get_workspace(session.workspace_id or "")
        if workspace is not None:
            lines.append(
                f"Shared checkout revision {workspace.workspace_revision}; "
                "read current files before relying on prior prompts."
            )
        for intent in self.store.list_session_intents(checkout_id=session.checkout_id):
            if intent.session_id == session.session_id:
                continue
            paths = ", ".join(intent.paths[:8])
            suffix = " …" if len(intent.paths) > 8 else ""
            lines.append(f"- session {intent.session_id}: {paths}{suffix}")
            if len(lines) >= 20:
                lines.append("- additional active session intents omitted")
                break
        return conversation, "\n".join(lines) if lines else None

    async def _session_compact(self, params: Mapping[str, Any]) -> dict[str, object]:
        """Summarize an idle session's saved conversation to free context."""

        async with self._authority_lock:
            session = self._authenticated_session(params, require_active=True)
            if self.store.session_has_active_task(session.session_id):
                raise LlmCoordError(
                    ErrorCode.TASK_NOT_MUTABLE,
                    "a task is still running in this conversation; let it finish "
                    "before summarizing",
                )
            prior = self.store.session_conversation(session.session_id)
            if prior is None:
                return {"compacted": False}
            provider, model, conversation = prior
            if provider != session.provider or model != session.model:
                raise ValueError("stored session conversation provider/model mismatch")
            checkout = self.store.get_checkout(session.checkout_id)
            if checkout is None:
                raise KeyError("checkout")
            harness = CodingAgentHarness(
                self.providers.create(
                    session.provider, session.model, effort=session.effort
                )
            )
            revision = session.conversation_revision
        updated, before, after = await asyncio.to_thread(
            harness.compact_conversation,
            conversation,
            worktree=Path(checkout.canonical_path),
        )
        async with self._authority_lock:
            current = self.store.get_session(session.session_id)
            if (
                current is None
                or current.conversation_revision != revision
                or self.store.session_has_active_task(session.session_id)
            ):
                raise LlmCoordError(
                    ErrorCode.TASK_NOT_MUTABLE,
                    "the conversation changed while it was being summarized; "
                    "run /compact again",
                )
            self.store.save_session_conversation(
                session.session_id,
                provider=session.provider,
                model=session.model,
                conversation=updated,
            )
        return {"compacted": True, "context_tokens": before, "summary_tokens": after}

    def _save_completed_session_conversation(
        self, task: TaskRecord, execution_id: str
    ) -> None:
        """Promote only a finished task's native snapshot into its session."""

        if task.session_id is None:
            return
        checkpoint = self.store.get_execution_checkpoint(execution_id)
        if checkpoint is None:
            return
        state = checkpoint.checkpoint
        if state.get("phase") != "finished":
            return
        provider = state.get("provider")
        model = state.get("model")
        native = state.get("session")
        if (
            not isinstance(provider, str)
            or not isinstance(model, str)
            or not isinstance(native, Mapping)
        ):
            raise ValueError("finished coding-agent checkpoint is malformed")
        self.store.save_session_conversation(
            task.session_id,
            provider=provider,
            model=model,
            conversation={
                "version": 1,
                "provider": provider,
                "model": model,
                "session": dict(native),
                "coordination_sequence": state.get("coordination_sequence"),
                "context_tokens": state.get("context_tokens"),
            },
        )

    async def _registered_repository_for_path(self, path: Path) -> RepositoryRecord:
        """Resolve a filesystem path to the repository it was registered as.

        The derived key depends on the target ref and on whether the operator
        registered by remote, so recomputing it here cannot be assumed to
        reproduce the registered key: checking out another branch changes the
        derived target.  Fall back to the Git common directory, which is stable
        across checkouts, and still require an unambiguous match so two
        registered targets in one repository are never silently conflated.
        """

        info: RepositoryInfo = await asyncio.to_thread(
            inspect_repository, path, profile_id=self.paths.profile_id
        )
        repository = self.store.get_repository_by_key(info.repo_key)
        if repository is not None:
            return repository
        same_repository = [
            record
            for record in self.store.list_repositories()
            if Path(record.git_common_dir) == info.common_git_dir
        ]
        exact_target = [
            record for record in same_repository if record.target_ref == info.target_ref
        ]
        for candidates in (exact_target, same_repository):
            if len(candidates) == 1:
                return candidates[0]
        if len(same_repository) > 1:
            raise LlmCoordError(
                ErrorCode.REPOSITORY_NOT_FOUND,
                "this repository has several registered targets; select one "
                "explicitly with --target",
                details={
                    "registered_targets": sorted(
                        record.target_ref for record in same_repository
                    )
                },
            )
        raise LlmCoordError(
            ErrorCode.REPOSITORY_NOT_FOUND,
            "repository target is not registered; run 'llm-coord repo add' first",
        )

    def _ping(self) -> dict[str, Any]:
        identity = code_identity()
        payload: dict[str, Any] = {
            "ready": True,
            "version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "profile_id": self.paths.profile_id,
            "boot_id": self.boot_id,
            "code_fingerprint": identity["fingerprint"],
            "code_path": identity["path"],
        }
        if self.startup_recovery is not None and self.startup_recovery.outcomes:
            payload["startup_recovery"] = _recovery_payload(self.startup_recovery)
        return payload

    def _safe_paths(self) -> dict[str, str]:
        return {
            "data": str(self.paths.data_dir),
            "state": str(self.paths.state_dir),
            "runtime": str(self.paths.runtime_dir),
        }

    def _db_status(self) -> dict[str, Any]:
        statuses: dict[str, Any] = {}
        databases = (
            ("control", self.paths.control_db),
            ("knowledge", self.paths.knowledge_db),
            ("vectors", self.paths.vectors_db),
        )
        for name, database_path in databases:
            if name == "control":
                connection = self.store.connect()
            elif name == "knowledge":
                connection = connect_knowledge(database_path)
            else:
                connection = connect_vectors(database_path)
            with closing(connection):
                statuses[name] = {
                    "path": str(database_path),
                    "bytes": database_path.stat().st_size,
                    "schema_version": current_schema_version(connection),
                    "journal_mode": str(
                        connection.execute("PRAGMA journal_mode").fetchone()[0]
                    ).lower(),
                    "synchronous": int(
                        connection.execute("PRAGMA synchronous").fetchone()[0]
                    ),
                }
        return statuses

    def _db_check(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, path in (
            ("control", self.paths.control_db),
            ("knowledge", self.paths.knowledge_db),
            ("vectors", self.paths.vectors_db),
        ):
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
            try:
                value = connection.execute("PRAGMA integrity_check").fetchone()
            finally:
                connection.close()
            detail = str(value[0]) if value is not None else "no result"
            result[name] = {"ok": detail == "ok", "detail": detail}
        result["ok"] = all(bool(item["ok"]) for item in result.values())
        return result


def _recovery_payload(report: RecoveryReport) -> dict[str, Any]:
    return {
        "boot_id": report.boot_id,
        "summary": report.summary,
        "outcomes": [asdict(outcome) for outcome in report.outcomes],
        "blocking_task_ids": sorted({outcome.task_id for outcome in report.blocking}),
    }


def _session_workspace(store: ControlStore, session: SessionRecord) -> WorkspaceRecord:
    if session.workspace_id is None:
        raise ValueError("session has no workspace binding")
    workspace = store.get_workspace(session.workspace_id)
    if workspace is None:
        raise ValueError("session workspace no longer exists")
    if workspace.mode != "shared":
        raise ValueError("this operation currently requires a shared workspace")
    return workspace


def _identity_payload(identity: FileIdentity) -> dict[str, object]:
    return {
        "kind": str(identity.kind),
        "mode": identity.mode,
        "digest": identity.digest,
        "size": identity.size,
    }


def _identity_from_payload(value: object) -> FileIdentity:
    if not isinstance(value, dict):
        raise ValueError(
            "base must be an identity object returned by workspace.read_file"
        )
    kind_value = value.get("kind")
    mode = value.get("mode")
    digest = value.get("digest")
    if (
        not isinstance(kind_value, str)
        or not isinstance(mode, str)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("base identity is malformed")
    try:
        kind = ObjectKind(kind_value)
    except ValueError as exc:
        raise ValueError("base identity has an unknown kind") from exc
    if kind is ObjectKind.ABSENT and mode:
        raise ValueError("an absent base identity must have an empty mode")
    if kind is ObjectKind.REGULAR and mode not in {REGULAR_MODE, EXECUTABLE_MODE}:
        raise ValueError("a regular base identity has an invalid mode")
    return FileIdentity(kind=kind, mode=mode, digest=digest)


def _candidate_identity(
    candidate: WorkspaceCandidateRecord, prefix: str
) -> FileIdentity:
    try:
        return FileIdentity(
            kind=ObjectKind(str(getattr(candidate, f"{prefix}_kind"))),
            mode=str(getattr(candidate, f"{prefix}_mode")),
            digest=str(getattr(candidate, f"{prefix}_digest")),
        )
    except ValueError as exc:  # pragma: no cover - database CHECKs enforce this
        raise ValueError("candidate holds an invalid identity") from exc


def _same_identity(left: FileIdentity, right: FileIdentity) -> bool:
    return (
        left.kind is right.kind
        and left.mode == right.mode
        and left.digest == right.digest
    )


def _published_event(store: ControlStore, publication: Any) -> Any:
    sequence = publication.checkout_event_sequence
    if sequence is None:
        raise ValueError("confirmed publication has no checkout event")
    event = store.get_checkout_event(publication.checkout_id, sequence)
    if event is None:
        raise ValueError("confirmed publication event is unavailable")
    return event


def _published_workspace_response(
    candidate: WorkspaceCandidateRecord,
    publication: Any,
    workspace: WorkspaceRecord,
    event: Any,
) -> dict[str, Any]:
    return {
        "outcome": "published",
        "candidate": asdict(candidate),
        "publication": asdict(publication),
        "workspace": asdict(workspace),
        "event": asdict(event),
    }


def _diverged_workspace_response(
    candidate: WorkspaceCandidateRecord,
    divergence: Any,
    event: Any | None,
) -> dict[str, Any]:
    # The update and divergence record committed together. Reflect that durable
    # terminal state even though the caller still holds the pre-update DTO.
    candidate = replace(
        candidate,
        state="diverged",
        diverged_at=divergence.created_at,
        updated_at=divergence.updated_at,
    )
    return {
        "outcome": "diverged",
        "candidate": asdict(candidate),
        "divergence": asdict(divergence),
        "event": asdict(event) if event is not None else None,
    }


def _asdict_or_none(value: DataclassInstance | None) -> dict[str, Any] | None:
    return asdict(value) if value is not None else None


def _string(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_string(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string or null")
    return value


def _workspace_mode(params: dict[str, Any], configured: str) -> str:
    """Resolve the workspace mode by the documented precedence.

    Lowest to highest: the compiled default, the user's profile default, then
    an explicit request for this session. A repository default deliberately
    cannot force isolated mode.
    """

    requested = _optional_string(params, "workspace") or configured
    if requested not in {"shared", "isolated"}:
        raise ValueError("workspace must be shared or isolated")
    return requested


def _acknowledged_sequence(params: dict[str, Any]) -> int:
    """Read the sequence a session claims to have consumed.

    Unlike a replay cursor this has no meaningful default: acknowledging an
    unspecified position would silently advance nothing while reporting
    success, so the key is required.
    """

    if "sequence" not in params:
        raise ValueError("sequence is required to acknowledge a session")
    return _non_negative_integer(params, "sequence", 0)


def _integer(params: dict[str, Any], key: str) -> int:
    value = params.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _non_negative_integer(params: dict[str, Any], key: str, default: int) -> int:
    value = params.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _bounded_integer(
    params: dict[str, Any], key: str, default: int, *, minimum: int, maximum: int
) -> int:
    value = _non_negative_integer(params, key, default)
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _path(params: dict[str, Any], key: str) -> Path:
    return Path(_string(params, key)).expanduser().absolute()


__all__ = ["DaemonService"]
