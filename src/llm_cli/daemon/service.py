"""Validated daemon RPC dispatch over the authoritative local services."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, suppress
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from _typeshed import DataclassInstance

from llm_cli import PROTOCOL_VERSION, __version__
from llm_cli.agent.driver import AgentDriver
from llm_cli.agent.finalization import checkpoint_finalization
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.modes import resolve_agent_mode, validate_agent_mode
from llm_cli.agent.registry import DriverRegistry
from llm_cli.agent.tools import MAX_QUESTION_CHARACTERS, TaskCancelled
from llm_cli.build import code_identity
from llm_cli.config.models import Settings
from llm_cli.config.operations import (
    effective_config,
    initial_sources,
    set_config_value,
)
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
    WorkspaceExecutionRecord,
    WorkspaceRecord,
)
from llm_cli.coordination.scopes import (
    ScopeValidationError,
    normalize_changed_path,
    normalize_scopes,
)
from llm_cli.doctor import run_doctor
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.execution.commands import CommandSettings, cleanup_command_snapshots
from llm_cli.execution.environment import (
    CommandEnvironment,
    SessionEnvironments,
    filtered_environment,
    unavailable,
)
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
from llm_cli.knowledge.indexer import KnowledgeIndex
from llm_cli.paths import AppPaths, validate_owned_sqlite_paths
from llm_cli.protocol.capabilities import WORKSPACE_CAPABILITIES
from llm_cli.protocol.envelopes import Request, Response
from llm_cli.protocol.framing import DEFAULT_MAX_FRAME_BYTES
from llm_cli.providers.anthropic_provider import AnthropicProvider
from llm_cli.providers.codex_provider import CodexProvider
from llm_cli.providers.openai_provider import OpenAIProvider
from llm_cli.providers.registry import ProviderRegistry
from llm_cli.storage.backup import BackupError, backup_profile
from llm_cli.storage.control import ControlStore
from llm_cli.storage.migrations import (
    current_schema_version,
)
from llm_cli.storage.retention import prune_task_output
from llm_cli.workspace.authority import OwnershipPin, PhysicalAuthority
from llm_cli.workspace.batches import SharedBatchPublisher
from llm_cli.workspace.broker import (
    MAX_SHARED_TEXT_BYTES,
    WorkspaceBrokerError,
    atomic_replace_regular_file,
    load_candidate_content,
    store_candidate_content,
    workspace_target,
)
from llm_cli.workspace.external import ExternalWorkspaceScanner
from llm_cli.workspace.identity import (
    EXECUTABLE_MODE,
    REGULAR_MODE,
    FileIdentity,
    ObjectKind,
    content_identity,
    identify_path,
    read_identified_path,
)
from llm_cli.workspace.isolated import IsolatedWorkspaceManager
from llm_cli.workspace.regime import CutoverManager

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
# Execution states whose checkpoint may still change; its usage is not final.
_LIVE_EXECUTION_STATES = frozenset(
    {"preparing", "worktree_ready", "running", "validated", "publishing"}
)
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
        config_sources: Mapping[str, str] | None = None,
        authority_root: Path | None = None,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.session_environments = SessionEnvironments()
        self._environment_waiters: set[str] = set()
        self.config_sources = (
            dict(config_sources)
            if config_sources is not None
            else initial_sources(paths.config_file, settings)
        )
        self.shutdown = shutdown
        self.boot_id = boot_id
        self.store = ControlStore(paths.control_db)
        self.physical_authority = PhysicalAuthority(
            self.store, paths.profile_id, boot_id, paths.socket, root=authority_root
        )
        self.store.authority = self.physical_authority
        self.cutovers = CutoverManager(self.store, self.physical_authority)
        self.knowledge = KnowledgeIndex(
            self.store,
            paths.knowledge_db,
            paths.candidate_dir,
            enabled=settings.knowledge_index == "lexical",
        )
        self._knowledge_task: asyncio.Task[None] | None = None
        self._ownership_idle_task: asyncio.Task[None] | None = None
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
        self.runner.project_search = (
            self.knowledge.project_search if self.knowledge.enabled else None
        )
        self.workspace_batches = SharedBatchPublisher(
            self.store, paths.candidate_dir, threading.RLock()
        )
        self.workspace_batches.refuse_staged_paths = settings.refuse_staged_paths
        self.shared_runner = SharedTaskExecutionRunner(
            self.runner, self.workspace_batches
        )
        self.isolated_workspaces = IsolatedWorkspaceManager(
            self.store,
            paths.data_dir / "isolated-worktrees",
            paths.candidate_dir,
            self.workspace_batches.lock,
            self.shared_runner.ensure_available,
            rebuild=settings.isolated_rebuild,
            rebuild_protected=(
                paths.config_dir,
                paths.data_dir,
                paths.state_dir,
                paths.runtime_dir,
            ),
        )
        self.shared_runner.isolated = self.isolated_workspaces
        self.workflow = self.shared_runner.workflow
        self.shared_runner.command_environment = self._command_environment
        self.workflow.command_environment = self._command_environment
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
        self.runner.web_fetch = settings.agent_web_fetch
        self.runner.web_domains = settings.agent_web_domains
        self.runner.web_proxy = settings.agent_web_proxy
        self.shared_runner.mcp_servers = settings.mcp_servers
        self.shared_runner.hooks = settings.hooks
        # Registries separate the durable harness identity from the model
        # adapter selected for one launch. Provider construction does not open
        # a client or resolve credentials; that happens on first use.
        self.providers = ProviderRegistry()
        self.providers.register(
            "anthropic",
            self._anthropic_provider,
            credential_environment_names=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        )
        self.providers.register(
            "openai",
            self._openai_provider,
            credential_environment_names=("OPENAI_API_KEY",),
        )
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
        self._external_scanner = ExternalWorkspaceScanner(self.store)
        self._next_external_scan_at = 0.0
        self._authority_lock = asyncio.Lock()
        self._pending_session_opens: set[str] = set()
        self._background_tasks: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._workspace_workers: set[tuple[str, int]] = set()
        self._workspace_workers_lock = threading.RLock()
        # Executions whose answer is being written again right now.
        self._answer_rewrites: set[str] = set()
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
        self.isolated_workspaces.recover()
        self.knowledge.initialize()
        self._start_knowledge_worker()
        # Nothing is served until every execution left behind by an earlier
        # boot has an outcome, so no session can observe a task that looks
        # runnable while a dead worker's reservation still blocks it.
        shared_recovery = self._recover_shared_executions()
        legacy_recovery = self.reconciler.reconcile()
        self.workflow.reconcile()
        self.isolated_workspaces.cleanup_closed()
        self.startup_recovery = RecoveryReport(
            boot_id=self.boot_id,
            outcomes=shared_recovery + legacy_recovery.outcomes,
        )
        self._schedule_ready_launches()
        self._start_ownership_idle_worker()

    def _recover_shared_executions(self) -> tuple[RecoveryOutcome, ...]:
        with self.workspace_batches.lock:
            self._recover_shared_workspace_publications()
            self.workspace_batches.recover()
            self._recover_authority_markers()
            return self.shared_runner.recover(
                self.drivers.capabilities,
                can_launch=self._can_reconstruct_launch,
                driver_for=self._driver_for_launch,
            )

    def _recover_authority_markers(self) -> None:
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT c.canonical_path FROM authority_journals j JOIN "
                "authority_checkouts a ON a.physical_key=j.physical_key JOIN checkouts"
                " c ON c.checkout_id=a.checkout_id WHERE j.kind IN ('manual','batch')"
            ).fetchall()
        for row in rows:
            with suppress(LlmCoordError, OSError, ValueError):
                self.physical_authority.finish_recovery(Path(row[0]))

    def _recover_shared_workspace_publications(self) -> None:
        """Resolve pre-replace journals strictly from the current path state."""

        for publication in self.store.list_recoverable_workspace_publications():
            candidate = self.store.get_workspace_candidate(publication.candidate_id)
            workspace = self.store.get_workspace(publication.workspace_id)
            if candidate is None or workspace is None:
                continue
            try:
                with self.physical_authority.mutation(
                    Path(workspace.canonical_path),
                    "manual",
                    candidate.candidate_id,
                    recovery=True,
                ) as operation:
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
                    if _same_identity(current, result) or _same_identity(current, base):
                        operation.confirm()

            except (LlmCoordError, OSError, ScopeValidationError, ValueError):
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
        self.session_environments.clear()
        if self._knowledge_task is not None:
            self._knowledge_task.cancel()
        if self._ownership_idle_task is not None:
            self._ownership_idle_task.cancel()
        self._execution_pool.shutdown(wait=False)
        self.physical_authority.close()

    def _start_ownership_idle_worker(self) -> None:
        if self._ownership_idle_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._ownership_idle_task = loop.create_task(
            self._ownership_idle_loop(), name="physical-ownership-idle"
        )

    async def _ownership_idle_loop(self) -> None:
        # Legacy reconciliation may run only once every 30 seconds. An idle
        # release must not wait for a second 30-second scheduling boundary.
        while not self.shutdown.is_set():
            try:
                await asyncio.wait_for(self.shutdown.wait(), timeout=0.5)
            except TimeoutError:
                with suppress(LlmCoordError, OSError, sqlite3.Error):
                    await asyncio.to_thread(self.physical_authority.release_idle)

    def _start_knowledge_worker(self) -> None:
        if not self.knowledge.enabled or self._knowledge_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._knowledge_task = loop.create_task(
            self._knowledge_loop(), name="optional-lexical-indexer"
        )

    async def _knowledge_loop(self) -> None:
        while not self.shutdown.is_set():
            try:
                await asyncio.wait_for(self.shutdown.wait(), timeout=1)
            except TimeoutError:
                try:
                    await asyncio.to_thread(self.knowledge.process_pending)
                except Exception as exc:
                    # The optional index must not silently stop serving future
                    # publications after an unexpected projection failure.
                    self.knowledge.record_worker_failure(exc)

    async def _knowledge_request(self, request: Request) -> dict[str, Any]:
        # Discovery never pins the daemon authority lock while reading Git,
        # updating FTS or waiting for an optional database.
        self._start_knowledge_worker()
        method, params = request.method, request.params
        if method == "index.status" and "path" not in params:
            return await asyncio.to_thread(self.knowledge.status)
        checkout = await self._registered_checkout_for_path(_path(params, "path"))
        if method == "index.status":
            return await asyncio.to_thread(self.knowledge.status, checkout.checkout_id)
        if method in {"index.build", "index.rebuild"}:
            return await asyncio.to_thread(
                self.knowledge.build,
                checkout.checkout_id,
                rebuild=method == "index.rebuild",
            )
        if method == "index.drop":
            return await asyncio.to_thread(self.knowledge.drop, checkout.checkout_id)
        if method == "graph.neighbors":
            return await asyncio.to_thread(
                self.knowledge.graph_neighbors,
                checkout.checkout_id,
                _string(params, "node"),
                edge=(None if params.get("edge") is None else _string(params, "edge")),
                depth=_bounded_integer(params, "depth", 1, minimum=1, maximum=4),
            )
        return await asyncio.to_thread(
            self.knowledge.search_lexical,
            checkout.checkout_id,
            _string(params, "query"),
            limit=_bounded_integer(params, "limit", 8, minimum=1, maximum=20),
            explain=bool(params.get("explain", False)),
        )

    async def reconcile_loop(self) -> None:
        interval = self.settings.reconciliation_interval_ms / 1_000
        while not self.shutdown.is_set():
            try:
                await asyncio.wait_for(self.shutdown.wait(), timeout=interval)
            except TimeoutError:
                async with self._authority_lock:
                    await asyncio.to_thread(self.coordinator.reconcile_expired)
                    await asyncio.to_thread(self.workflow.reconcile)
                    await asyncio.to_thread(self.isolated_workspaces.cleanup_closed)
                    if time.monotonic() >= self._check_cleanup_ready_at:
                        from llm_cli.execution.checks import cleanup_interrupted_checks

                        await asyncio.to_thread(
                            cleanup_interrupted_checks,
                            self.store,
                            self.paths.data_dir / "check-worktrees",
                        )
                    self._schedule_ready_launches()
                if time.monotonic() >= self._next_external_scan_at:
                    await asyncio.to_thread(self._scan_external_changes)
                    self._next_external_scan_at = time.monotonic() + 2.0
                self.physical_authority.release_idle()

    def _scan_external_changes(self) -> None:
        with self.workspace_batches.lock:
            self._external_scanner.scan_active()

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
            if request.method == "session.open":
                # Private dependency installation may take minutes. Setup and
                # activation lock independently; the installer never pins the
                # dispatch barrier used by every other terminal.
                session_id = _string(request.params, "session_id")
                async with self._authority_lock:
                    if session_id in self._pending_session_opens:
                        raise LlmCoordError(
                            ErrorCode.REPOSITORY_UNSAFE,
                            "session setup is already running",
                        )
                    self._pending_session_opens.add(session_id)
                try:
                    return await self._session_open(request.params)
                finally:
                    # Only the event loop touches this reservation. Keep it
                    # through registration, including the worker-return gap.
                    self._pending_session_opens.discard(session_id)
            if request.method == "session.compact":
                # The summary is a model request. Hold the authority lock only
                # to read and to save, never while the provider responds.
                return await self._session_compact(request.params)
            if request.method == "workspace.runtime.export":
                # Local byte copying must not pin the daemon-wide RPC barrier.
                return await asyncio.to_thread(
                    self._workspace_runtime_export, request.params
                )
            if request.method == "session.retry_answer":
                # Likewise a model request, locked only to read and to save.
                return await self._session_retry_answer(request.params)
            if request.method in {
                "index.status",
                "index.build",
                "index.rebuild",
                "index.drop",
                "search.lexical",
                "graph.neighbors",
            }:
                return await self._knowledge_request(request)
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
        if task.session_id is not None:
            value["command_environment"] = self._environment_plan(task.session_id)
        if task_id in self._environment_waiters:
            value["state"] = "awaiting_session_environment"
        return value

    def _task_retry(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError("task")
        if task.session_id is not None:
            # Nothing would launch a conversation task's next attempt. It
            # would hide the retained proposal from apply and, while queued,
            # refuse every later prompt in the conversation.
            raise LlmCoordError(
                ErrorCode.TASK_NOT_MUTABLE,
                "conversation tasks cannot be retried; apply or discard the "
                "retained proposal, or ask for the change again in the "
                "conversation",
                {"task_id": task_id},
            )
        return asdict(self.store.begin_new_attempt(task_id))

    async def _dispatch(self, request: Request) -> Any:
        method, params = request.method, request.params
        managed = {
            "session.open",
            "session.resume",
            "workspace.read_file",
            "workspace.stage_file",
            "workspace.publish_candidate",
            "task.run",
            "task.retry",
            "task.apply",
            "task.undo",
            "task.discard",
            "checks.configure",
            "claim.release",
            "claim.renew",
        }
        if method not in managed or (
            method == "workspace.read_file" and params.get("read_only") is True
        ):
            return await self._dispatch_owned(request)
        root: Path | None = None
        if "path" in params:
            # Git discovers the selected worktree, never another remote clone.
            requested = _path(params, "path")
            requested_checkout = await self._registered_checkout_for_path(requested)
            # Observe before choosing the task's immutable legacy checkout;
            # the final selected root is pinned below before any mutation.
            requested_regime = (
                self.physical_authority.regime(Path(requested_checkout.canonical_path))
                if method == "task.run" and params.get("session_id") is None
                else None
            )
            if requested_regime is not None and requested_regime["regime"] == "legacy":
                legacy = await self._registered_repository_for_path(requested)
                prior = self.store.get_task(str(params.get("task_id", "")))
                if prior is not None:
                    legacy = self._execution_repository(legacy, prior)
                checkout = await self._registered_checkout_for_path(
                    Path(legacy.main_worktree_path), repository=legacy
                )
            else:
                checkout = requested_checkout
            root = Path(checkout.canonical_path)
        elif "session_id" in params:
            session = self._authenticated_session(params)
            session_checkout = self.store.get_checkout(session.checkout_id)
            if session_checkout is not None:
                root = Path(session_checkout.canonical_path)
        else:
            task_id = params.get("task_id")
            if not task_id and "claim_id" in params:
                claim = self.store.get_claim(_string(params, "claim_id"))
                task_id = claim.task_id if claim is not None else None
            task = self.store.get_task(str(task_id)) if task_id else None
            if task is not None:
                repository = self.store.get_repository_by_key(task.repo_key)
                if repository is not None:
                    root = Path(
                        self._execution_repository(repository, task).main_worktree_path
                    )
        if root is None:
            return await self._dispatch_owned(request)
        with self.physical_authority.pin(root):
            if method in {"claim.release", "claim.renew"}:
                self.physical_authority.assert_regime(root, allowed=("legacy",))
            return await self._dispatch_owned(request)

    async def _dispatch_owned(self, request: Request) -> Any:
        method = request.method
        params = request.params
        if method == "workspace.authority.maintenance":
            values = {
                name: params.get(name, False)
                for name in (
                    "prune",
                    "apply",
                    "include_unrecorded",
                    "include_other_profiles",
                )
            }
            if any(type(value) is not bool for value in values.values()):
                raise ValueError("authority maintenance options must be booleans")
            return await asyncio.to_thread(self.physical_authority.records, **values)
        if method.startswith("workspace.cutover."):
            return await asyncio.to_thread(self._cutover_request, method, params)
        if method == "system.init":
            self.initialize()
            return {"initialized": True, **self._ping(), "paths": self._safe_paths()}
        if method == "system.doctor":
            checks = await asyncio.to_thread(
                run_doctor,
                self.paths,
                knowledge_status=self.knowledge.status(),
                authority_status=[
                    self.physical_authority.status(Path(c.canonical_path))
                    for c in self.store.list_checkouts()
                ],
            )
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
        if method == "db.backup":
            output = _optional_string(params, "out")
            try:
                return await asyncio.to_thread(
                    backup_profile, self.paths, Path(output) if output else None
                )
            except (BackupError, OSError) as exc:
                raise LlmCoordError(
                    ErrorCode.INTERNAL_RECOVERABLE,
                    f"the verified database backup could not complete: {exc}",
                ) from exc
        if method == "db.prune":
            apply = params.get("apply", False)
            if not isinstance(apply, bool):
                raise ValueError("apply must be a boolean")
            return await asyncio.to_thread(
                prune_task_output,
                self.store,
                older_than=_optional_string(params, "older_than") or "30d",
                apply=apply,
            )
        if method == "config.show":
            return await asyncio.to_thread(
                effective_config,
                self.paths.config_file,
                self.settings,
                self.config_sources,
            )
        if method == "config.set":
            return await asyncio.to_thread(
                set_config_value,
                self.paths.config_file,
                profile_id=self.paths.profile_id,
                key=_string(params, "key"),
                value=_string(params, "value"),
            )
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
        if method == "workspace.observe":
            checkout = await self._registered_checkout_for_path(_path(params, "path"))
            relative = normalize_changed_path(_string(params, "relative_path"))
            from llm_cli.agent.source_policy import ensure_safe_content, excluded_paths

            if relative in excluded_paths(Path(checkout.canonical_path), [relative]):
                raise LlmCoordError(
                    ErrorCode.REPOSITORY_UNSAFE,
                    "read-only observation is restricted to ordinary source paths",
                )
            identity, raw = read_identified_path(
                workspace_target(Path(checkout.canonical_path), relative),
                max_bytes=MAX_SHARED_TEXT_BYTES,
            )
            if raw is not None:
                ensure_safe_content(raw)
            return {
                "relative_path": relative,
                "content": raw.decode("utf-8") if raw is not None else None,
                "identity": _identity_payload(identity),
                "cooperative_read_barrier": False,
                "edit_capability": False,
                "authority": self.physical_authority.status(
                    Path(checkout.canonical_path)
                ),
            }
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
            if session.state == "closed":
                raise ValueError("a closed session cannot be resumed")
            self._accept_session_environment(session, params)
            resumed, event = self.store.resume_session(session.session_id)
            self._schedule_ready_launches()
            return {
                "session": asdict(resumed),
                "command_environment": self._environment_plan(session.session_id),
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
                "command_environment": self._environment_plan(shown.session_id),
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
            for task_id in tuple(self._environment_waiters):
                task = self.store.get_task(task_id)
                if task is not None and task.session_id == session.session_id:
                    self.workflow.cancel(task_id)
                    self._finish_environment_waiter(task_id)
            self.session_environments.discard(session.session_id)
            closed, event = self.store.close_session(
                session.session_id,
                reason=_optional_string(params, "reason") or "operator_exit",
            )
            await asyncio.to_thread(
                self.isolated_workspaces.cleanup_closed, session.session_id
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
        if method == "session.usage":
            return self._session_usage(self._authenticated_session(params))
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
                if task_id in self._environment_waiters:
                    self._finish_environment_waiter(task_id)
                    workflow_result = {"state": "cancelled"}
                self._schedule_ready_launches()
                return workflow_result
            environment = (
                self._apply_environment(task_id, params)
                if method == "task.apply"
                else None
            )
            try:
                workflow_result = await asyncio.to_thread(
                    self.workflow.apply,
                    task_id,
                    undo=method == "task.undo",
                    allow_unverified=bool(params.get("allow_unverified", False)),
                    environment=(
                        (lambda: environment) if environment is not None else None
                    ),
                )
            finally:
                if environment is not None:
                    environment.invalidate()
            await asyncio.to_thread(self.isolated_workspaces.cleanup_closed)
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
            return self._task_retry(_string(params, "task_id"))
        if method == "task.discard":
            held = self.workflow.discard(_string(params, "task_id"))
            if held is not None:
                await asyncio.to_thread(self.isolated_workspaces.cleanup_closed)
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

    def _workspace_runtime_export(self, params: dict[str, Any]) -> dict[str, Any]:
        from llm_cli.storage.connection import immediate_transaction
        from llm_cli.workspace.runtime_export import export_runtime

        flags = {
            name: params.get(name, False)
            for name in ("yes", "allow_secret", "into_checkout")
        }
        if any(type(value) is not bool for value in flags.values()):
            raise ValueError("runtime export approvals must be booleans")
        with ExitStack() as activity:
            with self.workspace_batches.lock:
                session = self._authenticated_session(params, require_active=True)
                if session.workspace_mode != "isolated":
                    raise LlmCoordError(
                        ErrorCode.REPOSITORY_UNSAFE,
                        "runtime export requires an active isolated session",
                    )
                workspace_id = session.workspace_id or ""
                with self.store.connection() as con:
                    busy = con.execute(
                        "SELECT 1 FROM tasks WHERE session_id=? AND state NOT IN "
                        "('completed','failed','cancelled') LIMIT 1",
                        (session.session_id,),
                    ).fetchone()
                    switching = con.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='mode_switch_intents'"
                    ).fetchone()
                    if switching:
                        switching = con.execute(
                            "SELECT 1 FROM mode_switch_intents WHERE "
                            "source_workspace_id=? AND state NOT IN "
                            "('confirmed','rolled_back') LIMIT 1",
                            (workspace_id,),
                        ).fetchone()
                    if busy or switching:
                        raise LlmCoordError(
                            ErrorCode.REPOSITORY_UNSAFE,
                            "runtime export requires an idle workspace without a "
                            "pending mode switch",
                        )
                state = self.isolated_workspaces.validate(workspace_id)
                workspace = self.store.get_workspace(workspace_id)
                checkout = self.store.get_checkout(session.checkout_id)
                assert workspace is not None and checkout is not None
                activity.enter_context(
                    self.physical_authority.pin(Path(checkout.canonical_path))
                )
                activity.enter_context(
                    self.isolated_workspaces.runtime.activity(workspace_id)
                )
                checkouts = tuple(
                    Path(record.canonical_path)
                    for record in self.store.list_checkouts()
                )
                protected: tuple[Path, ...] = (
                    self.paths.config_dir,
                    self.paths.data_dir,
                    self.paths.data_dir.parent.parent,
                    self.paths.state_dir,
                    self.paths.state_dir.parent.parent,
                    self.paths.runtime_dir,
                    self.paths.runtime_dir.parent,
                    self.isolated_workspaces.managed_root,
                    self.physical_authority.root,
                )
                with self.store.connection() as con:
                    protected += tuple(
                        Path(row[0])
                        for row in con.execute(
                            "SELECT canonical_path FROM workspaces WHERE "
                            "mode='isolated' UNION SELECT git_common_dir FROM "
                            "checkouts UNION SELECT git_dir FROM workspaces "
                            "WHERE git_dir IS NOT NULL"
                        )
                    )
            result = export_runtime(
                Path(workspace.canonical_path),
                _string(params, "relative_path"),
                Path(_string(params, "destination")),
                checkout=Path(checkout.canonical_path),
                runtime_paths=state.runtime_paths,
                source_paths=tuple(state.source_manifest),
                shared_checkouts=checkouts,
                protected_roots=protected,
                staging_root=Path(workspace.git_dir) if workspace.git_dir else None,
                **flags,
            )
            if result["state"] == "exported":
                # Paths, file values and content identities never enter the ledger.
                with self.store.connection() as con, immediate_transaction(con):
                    self.store.append_checkout_event(
                        con,
                        checkout_id=session.checkout_id,
                        event_type="session.runtime_exported",
                        caused_by_session_id=session.session_id,
                        payload={
                            key: result[key]
                            for key in (
                                "kind", "bytes", "entries", "sensitivity", "state"
                            )
                        },
                        now=int(time.time() * 1000),
                    )
            return result

    def _cutover_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        root = _path(params, "path")
        if method == "workspace.cutover.plan":
            return self.cutovers.preview(root)
        if method == "workspace.cutover.status":
            return self.cutovers.status(root)
        if method == "workspace.cutover.recovery":
            return self.cutovers.recovery(
                root,
                _string(params, "intent_id"),
                checkpoint=_string(params, "checkpoint"),
            )
        if method == "workspace.cutover.apply":
            dispositions = params.get("dispositions", {})
            if not isinstance(dispositions, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in dispositions.items()
            ):
                raise ValueError("dispositions must map legacy identifiers to actions")
            with self.workspace_batches.lock:
                return self.cutovers.begin(
                    root,
                    checkpoint=_string(params, "checkpoint"),
                    dispositions=dispositions,
                    intent_id=_optional_string(params, "intent_id"),
                )
        if method == "workspace.cutover.resume":
            with self.workspace_batches.lock:
                return self.cutovers.resume(
                    root,
                    intent_id=_string(params, "intent_id"),
                    checkpoint=_string(params, "checkpoint"),
                    accept_current_history=_optional_string(
                        params, "accept_current_history"
                    ),
                )
        if method == "workspace.cutover.export":
            return self._cutover_export(root)
        raise LlmCoordError(ErrorCode.PROTOCOL_MISMATCH, "unknown cutover operation")

    def _cutover_export(self, root: Path) -> dict[str, Any]:
        # Export only the enumerated metadata columns. Checkpoints, command
        # output, conversations and private runtime/source content stay out.
        plan = self.cutovers.inventory(root)
        mappings = plan.get("mappings", [])
        keys = sorted(
            {
                str(mapping["repo_key"])
                for mapping in mappings
                if isinstance(mapping, dict)
                and isinstance(mapping.get("repo_key"), str)
            }
        )
        tasks: list[dict[str, Any]] = []
        claims: list[dict[str, Any]] = []
        executions: list[dict[str, Any]] = []
        refs: list[dict[str, Any]] = []
        if keys:
            slots = ",".join("?" for _ in keys)
            with self.store.connection() as connection:
                tasks = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT task_id,repository_id,repo_key,state,attempt,"
                        "session_id,"
                        "created_at,updated_at FROM tasks "
                        f"WHERE repo_key IN ({slots})",
                        keys,
                    )
                ]
                claims = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT claim_id,task_id,task_attempt,repo_key,state,"
                        "created_at,updated_at FROM claims "
                        f"WHERE repo_key IN ({slots})",
                        keys,
                    )
                ]
                executions = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT e.execution_id,e.task_id,e.task_attempt,e.claim_id,"
                        "e.driver,e.state,e.base_oid,e.result_tree_id,e.result_commit_id,"
                        "e.patch_hash,e.created_at,e.updated_at FROM task_executions e "
                        f"JOIN tasks t USING(task_id) WHERE t.repo_key IN ({slots})",
                        keys,
                    )
                ]
                refs = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT intent_id,task_id,task_attempt,repo_key,"
                        "target_ref,task_ref,"
                        "result_tree_id,result_commit_id,operation_state "
                        f"FROM publication_intents WHERE repo_key IN ({slots})",
                        keys,
                    )
                ]
        return {
            "format": "loupe.legacy-metadata.v1",
            "checkpoint": plan["checkpoint"],
            "mappings": mappings,
            "tasks": tasks,
            "claims": claims,
            "executions": executions,
            "publication_refs": refs,
            "dispositions": plan.get("dispositions", []),
            "notice": "Metadata export only; retained diffs and refs remain in place.",
        }

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
            canonical_path=str(info.worktree.resolve()),
            git_common_dir=str(info.common_git_dir.resolve()),
        )
        return {
            **asdict(record),
            "checkout": asdict(checkout),
            "base_oid": info.base_oid,
        }

    def _environment_plan(self, session_id: str) -> dict[str, object]:
        if self.settings.agent_command_environment == "minimal":
            return {"policy": "minimal", "state": "ready"}
        return self.session_environments.plan(session_id)

    def _command_environment(self, session_id: str) -> CommandEnvironment | None:
        if self.settings.agent_command_environment == "minimal":
            return None
        return self.session_environments.get(session_id)

    def _accept_session_environment(
        self, session: SessionRecord, params: dict[str, Any]
    ) -> None:
        if (
            self.settings.agent_command_environment != "session"
            or "command_environment" not in params
        ):
            return
        values, removed = filtered_environment(
            params["command_environment"],
            registered=self.providers.credential_environment_names,
        )
        try:
            prior = self.session_environments.get(session.session_id)
        except LlmCoordError:
            prior = None
        if prior is not None and prior.matches(values):
            return
        environment = self._build_session_environment(session, values, removed)

        def invalidate_checks() -> None:
            # Values are never persisted. Invalidation and binding replacement
            # serialize with the final successful check write under one lease.
            with self.store.connection() as connection:
                connection.execute(
                    "UPDATE check_runs SET state='stale' WHERE state='passed' "
                    "AND task_id IN (SELECT task_id FROM tasks WHERE session_id=?)",
                    (session.session_id,),
                )

        self.session_environments.replace(
            session.session_id, environment, before_replace=invalidate_checks
        )

    def _build_session_environment(
        self, session: SessionRecord, values: dict[str, str], removed: tuple[str, ...]
    ) -> CommandEnvironment:
        checkout = self.store.get_checkout(session.checkout_id)
        assert checkout is not None
        environment = CommandEnvironment(
            values,
            removed,
            source_root=Path(checkout.canonical_path),
            credential_names=self.providers.credential_environment_names,
        )
        if session.workspace_mode == "isolated":
            state = self.isolated_workspaces.validate(session.workspace_id or "")
            workspace = self.store.get_workspace(session.workspace_id or "")
            assert workspace is not None
            environment.hydrated_root = Path(workspace.canonical_path)
            environment.hydrated_paths = tuple(
                sorted(set(state.source_manifest) | set(state.runtime_files))
            )
        return environment

    def _apply_environment(
        self, task_id: str, params: dict[str, Any]
    ) -> CommandEnvironment | None:
        if (
            self.settings.agent_command_environment != "session"
            or "command_environment" not in params
            or params.get("allow_unverified", False)
        ):
            return None
        task = self.store.get_task(task_id)
        session = self.store.get_session(task.session_id or "") if task else None
        workflow = self.workflow.get(task_id)
        if (
            session is None
            or workflow is None
            or workflow["status"] not in {"awaiting_review", "cancelled"}
            or workflow["proposal_json"] is None
        ):
            return None
        try:
            self.session_environments.get(session.session_id)
        except LlmCoordError:
            pass
        else:
            # Another applying terminal cannot replace this session's origin.
            return None
        values, removed = filtered_environment(
            params["command_environment"],
            registered=self.providers.credential_environment_names,
        )
        environment = self._build_session_environment(session, values, removed)
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE check_runs SET state='stale' "
                "WHERE state='passed' AND task_id=?",
                (task_id,),
            )
        return environment

    def _finish_environment_waiter(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if task is None:
            self._environment_waiters.discard(task_id)
            return
        execution = self.store.get_execution(task_id, task.attempt)
        with self.workspace_batches.lock:
            if execution is not None and execution.state == "running":
                self.shared_runner._retain_checkpoint_candidates(execution)
                self.workflow.hold(
                    task_id,
                    task.attempt,
                    execution.execution_id,
                    cancelled=True,
                    completion_outcome="cancelled",
                )
            elif (
                isinstance(execution, WorkspaceExecutionRecord)
                and execution.state == "preparing"
            ):
                if self.workspace_batches.get(execution.execution_id) is not None:
                    raise ClaimAuthorityError(
                        "preparing workspace journal needs recovery"
                    )
                self.coordinator.settle_workspace_execution(
                    execution.execution_id,
                    outcome="failed_safe",
                    failure_code="CANCELLED",
                )
                self.workflow.update(task_id, task.attempt, status="cancelled")
            elif task.current_claim_id is not None:
                self.coordinator.release_claim(
                    task.current_claim_id, reason="cancelled", task_state="cancelled"
                )
                self.workflow.update(task_id, task.attempt, status="cancelled")
        self._environment_waiters.discard(task_id)
        launch = self.store.get_execution_launch(task_id, task.attempt)
        if launch is not None:
            self._close_one_shot_if_settled(task, launch)

    async def _session_open(self, params: dict[str, Any]) -> dict[str, Any]:
        """Bind a durable conversation to one exact checkout and provider."""

        async with self._authority_lock:
            if self.settings.agent_command_environment == "session":
                if "command_environment" not in params:
                    raise unavailable()
                filtered_environment(
                    params["command_environment"],
                    registered=self.providers.credential_environment_names,
                )
            agent_mode = resolve_agent_mode(
                params.get("mode"), params.get("publish"), default="auto"
            )
            session_id = _string(params, "session_id")
            resume_token_hash = _string(params, "resume_token_hash")
            if len(resume_token_hash) != 64 or any(
                character not in "0123456789abcdef" for character in resume_token_hash
            ):
                raise ValueError("resume_token_hash must be a lowercase SHA-256 digest")
            repository = await self._registered_repository_for_path(
                _path(params, "path")
            )
            checkout = await self._registered_checkout_for_path(
                _path(params, "path"), repository
            )
            self.physical_authority.assert_regime(
                Path(checkout.canonical_path), allowed=("legacy", "workspace")
            )
            provider = (
                _optional_string(params, "provider") or self.settings.agent_provider
            )
            model = _optional_string(params, "model")
            effort = _optional_string(params, "effort")
            if model is None and provider == self.settings.agent_provider:
                model = self.settings.agent_model
            adapter = self.providers.create(provider, model, effort=effort)
            workspace_mode = _workspace_mode(params, self.settings.workspace_mode)
            approved = params.get("approved_runtime_files", [])
            if (
                not isinstance(approved, list)
                or len(approved) > 1000
                or any(not isinstance(path, str) for path in approved)
            ):
                raise ValueError("approved_runtime_files must be exact relative paths")
            if approved and workspace_mode != "isolated":
                raise ValueError("runtime consent requires isolated mode")
        notices: tuple[str, ...] = ()
        if workspace_mode == "isolated":
            workspace, notices = await asyncio.to_thread(
                self.isolated_workspaces.open,
                checkout,
                session_id,
                approved_secrets=tuple(sorted(set(approved))),
            )
        else:
            async with self._authority_lock:
                with self.physical_authority.pin(Path(checkout.canonical_path)):
                    workspace = await asyncio.to_thread(
                        self.store.ensure_shared_workspace, checkout
                    )
        async with self._authority_lock:
            with self.physical_authority.pin(Path(checkout.canonical_path)):
                if self.physical_authority.status(Path(checkout.canonical_path)).get(
                    "history_gap"
                ):
                    notices += (
                        "Checkout ownership changed; earlier ownership history "
                        "is incomplete. "
                        "Re-read current source before applying retained proposals.",
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
                self._accept_session_environment(session, params)
                return {
                    "session": asdict(session),
                    "command_environment": self._environment_plan(session.session_id),
                    "checkout": asdict(checkout),
                    "workspace": asdict(workspace),
                    "bootstrap_event": asdict(event),
                    "bootstrap_sequence": bootstrap_sequence,
                    "workspace_notices": list(notices),
                    "approved_runtime_files": sorted(set(approved)),
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
            "authority": self.physical_authority.status(Path(checkout.canonical_path)),
        }

    async def _workspace_read_file(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read a bounded text file and issue the base identity for staging."""

        session = self._authenticated_session(params, require_active=True)
        relative_path = normalize_changed_path(_string(params, "relative_path"))
        workspace = _session_workspace(self.store, session)
        read_only = params.get("read_only", False)
        if not isinstance(read_only, bool):
            raise ValueError("read_only must be a boolean")
        if read_only:
            from llm_cli.agent.source_policy import excluded_paths

            if relative_path in excluded_paths(
                Path(workspace.canonical_path), [relative_path]
            ):
                raise LlmCoordError(
                    ErrorCode.REPOSITORY_UNSAFE,
                    "read-only observation is restricted to ordinary source paths",
                )
        target = workspace_target(Path(workspace.canonical_path), relative_path)
        identity, raw = read_identified_path(target, max_bytes=MAX_SHARED_TEXT_BYTES)
        if read_only and raw is not None:
            from llm_cli.agent.source_policy import ensure_safe_content

            ensure_safe_content(raw)
        content: str | None = None
        if raw is not None:
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    "shared broker reads only UTF-8 text in this release"
                ) from exc
        if not read_only:
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
            "cooperative_read_barrier": not read_only,
            "edit_capability": not read_only,
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
        if session.workspace_mode == "isolated":
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                "isolated publication requires the isolated batch publisher",
            )
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
        from llm_cli.workspace.gitstate import publication as git_publication

        with (
            git_publication(
                self.store,
                Path(workspace.canonical_path),
                (candidate.relative_path,),
                refuse_staged_paths=self.settings.refuse_staged_paths,
                candidate_dir=self.paths.candidate_dir,
            ),
            self.physical_authority.mutation(
                Path(workspace.canonical_path), "manual", candidate_id
            ) as operation,
        ):
            candidate, publication, workspace = self.store.begin_workspace_publication(
                candidate_id=candidate_id, session_id=session.session_id
            )
            if publication.operation_state == "confirmed":
                operation.confirm()
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
                operation.confirm()
                return _diverged_workspace_response(candidate, divergence, event)
            try:
                self.physical_authority.validate_current()
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
                    operation.confirm()
                raise
            observed = identify_path(target)
            if _same_identity(observed, result):
                confirmed = self.store.confirm_workspace_publication(
                    candidate_id=candidate_id
                )
                operation.confirm()
                return _published_workspace_response(*confirmed)
            if not _same_identity(observed, base):
                divergence, event = self.store.record_workspace_divergence(
                    candidate_id=candidate_id, current=observed
                )
                # A post-effect third state is not proof of a settled result.
                # Preserve the owning recovery gate and the user's later edit.
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
        record = repository or self._registered_repository_for_info(info)
        return self.store.ensure_checkout(
            repository=record,
            canonical_path=str(info.worktree.resolve()),
            git_common_dir=str(info.common_git_dir.resolve()),
        )

    def _execution_repository(
        self, repository: RepositoryRecord, task: TaskRecord
    ) -> RepositoryRecord:
        """Use a session's exact checkout instead of a legacy mutable path."""

        if task.session_id is None:
            if task.current_claim_id is None:
                return repository
            with self.store.connection() as con:
                targets = con.execute(
                    "SELECT DISTINCT c.canonical_path FROM authority_journals j "
                    "JOIN authority_checkouts a USING(physical_key) "
                    "JOIN checkouts c ON c.checkout_id=a.checkout_id "
                    "WHERE (j.kind='legacy_claim' AND j.reference_id=?) OR "
                    "(j.kind='legacy_execution' AND j.reference_id IN "
                    "(SELECT execution_id FROM task_executions WHERE task_id=? "
                    "AND task_attempt=?))",
                    (task.current_claim_id, task.task_id, task.attempt),
                ).fetchall()
            if len(targets) != 1:
                raise LlmCoordError(
                    ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED,
                    "the legacy claim's physical checkout cannot be proved uniquely",
                )
            return replace(repository, main_worktree_path=str(targets[0][0]))
        session = self.store.get_session(task.session_id)
        if session is None:
            raise ValueError("task references a missing session")
        checkout = self.store.get_checkout(session.checkout_id)
        if checkout is None or checkout.repository_id != repository.repository_id:
            raise ValueError("task session checkout is no longer valid")
        return replace(repository, main_worktree_path=checkout.canonical_path)

    def _one_shot_session(
        self, checkout: CheckoutRecord, params: dict[str, Any]
    ) -> SessionRecord:
        provider = _optional_string(params, "provider") or self.settings.agent_provider
        model = _optional_string(params, "model")
        if model is None and provider == self.settings.agent_provider:
            model = self.settings.agent_model
        effort = _optional_string(params, "effort")
        adapter = self.providers.create(provider, model, effort=effort)
        if self.settings.agent_command_environment == "session":
            if "command_environment" not in params:
                raise unavailable()
            filtered_environment(
                params["command_environment"],
                registered=self.providers.credential_environment_names,
            )
        agent_mode = resolve_agent_mode(
            params.get("mode"), params.get("publish"), default="auto"
        )
        workspace = self.store.ensure_shared_workspace(checkout)
        session, _event, sequence = self.store.open_session(
            session_id=new_id("oneshot"),
            checkout_id=checkout.checkout_id,
            resume_token_hash=hashlib.sha256(new_id("resume").encode()).hexdigest(),
            provider=adapter.name,
            model=adapter.model,
            effort=effort,
            workspace_id=workspace.workspace_id,
            workspace_mode="shared",
            agent_mode=agent_mode,
        )
        session, _cursor, _ready = self.store.acknowledge_session(
            session.session_id, sequence=sequence
        )
        self._accept_session_environment(session, params)
        return session

    async def _task_run(self, params: dict[str, Any]) -> dict[str, Any]:
        requested_path = _path(params, "path")
        # This discovery is fresh after the dispatch pin. Reuse its Git facts
        # for repository and checkout selection within this request.
        info = await asyncio.to_thread(
            inspect_repository, requested_path, profile_id=self.paths.profile_id
        )
        repository = self._registered_repository_for_info(info)
        # Validate request identity before allocating a one-shot session or
        # retaining any terminal environment for it.
        _string(params, "title")
        _optional_string(params, "task_id")
        fixture_values = params.get("fixture_writes", [])
        if not isinstance(fixture_values, list) or any(
            not isinstance(value, str) for value in fixture_values
        ):
            raise ValueError("fixture_writes must be a list of PATH=CONTENT strings")
        scopes = params.get("scopes")
        if not isinstance(scopes, list) or any(not isinstance(x, str) for x in scopes):
            raise ValueError("scopes must be a list of strings")
        normalize_scopes(scopes)
        if not isinstance(params.get("interactive", False), bool):
            raise ValueError("interactive must be a boolean")
        session_id = _optional_string(params, "session_id")
        session: SessionRecord | None = None
        execution_repository = repository
        requested_checkout = self.store.ensure_checkout(
            repository=repository,
            canonical_path=str(info.worktree.resolve()),
            git_common_dir=str(info.common_git_dir.resolve()),
        )
        regime = self.physical_authority.regime(Path(requested_checkout.canonical_path))
        if regime["regime"] == "transition":
            raise LlmCoordError(
                ErrorCode.CUTOVER_BLOCKED, "checkout conversion is awaiting recovery"
            )
        claim_free = regime["regime"] == "workspace"
        one_shot = False
        if claim_free and (params.get("claim_only") or params.get("fixture_writes")):
            raise LlmCoordError(
                ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED,
                "claim and fixture/ref workflows require a legacy checkout; "
                "inspect or export retained legacy work here",
                {"current_regime": "workspace", "required_contract": "legacy"},
            )
        if claim_free and session_id is None:
            prior_task = self.store.get_task(str(params.get("task_id", "")))
            prior_recipe = (
                self.store.get_execution_launch(prior_task.task_id, prior_task.attempt)
                if prior_task is not None
                else None
            )
            if (
                prior_task is not None
                and prior_recipe is not None
                and prior_recipe.parameters.get("one_shot_session") is True
            ):
                session = self.store.get_session(prior_task.session_id or "")
                if session is None:
                    raise ValueError("one-shot task session is missing")
                if (
                    prior_task.repository_id != repository.repository_id
                    or session.checkout_id != requested_checkout.checkout_id
                ):
                    raise ValueError("one-shot task belongs to another checkout")
                prior_execution = self.store.get_execution(
                    prior_task.task_id, prior_task.attempt
                )
                if isinstance(
                    prior_execution, WorkspaceExecutionRecord
                ) and prior_execution.state in {"published", "failed"}:
                    # An idempotent replay is observation, including after the
                    # one-shot session and its in-memory environment close.
                    return {
                        "task": self._task_view(prior_task.task_id),
                        "claim": None,
                        "execution": prior_execution.state,
                        "execution_contract": "workspace_v1",
                    }
            else:
                if prior_task is not None:
                    raise ValueError(
                        "task ID is already bound to another execution contract"
                    )
                session = self._one_shot_session(requested_checkout, params)
            session_id = session.session_id
            one_shot = True
            if "command_environment" in params:
                self._accept_session_environment(session, params)
        if session_id is not None:
            if not one_shot:
                session = self._authenticated_session(params, require_active=True)
            assert session is not None
            if session.session_id != session_id:
                raise LlmCoordError(
                    ErrorCode.SESSION_AUTH_REQUIRED,
                    "the supplied session credentials do not match the task session",
                )
            checkout = requested_checkout
            if checkout.checkout_id != session.checkout_id:
                raise ValueError("task path is outside the session's selected checkout")
            if session.agent_mode != "plan":
                self._command_environment(session.session_id)
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
        if session is None and existing_task is not None:
            execution_repository = self._execution_repository(repository, existing_task)
        if session_id is not None:
            with self.store.connection() as connection:
                active = connection.execute(
                    """SELECT t.task_id FROM tasks t LEFT JOIN claims c
                        ON c.claim_id=t.current_claim_id
                        LEFT JOIN workspace_executions e ON e.task_id=t.task_id
                        AND e.task_attempt=t.attempt
                        WHERE t.session_id=? AND t.task_id<>? AND (
                            (c.state IN ('queued','active_work','publishing',
                                         'active_integration')
                             AND NOT (?=1 AND c.state='queued'))
                            OR e.state IN ('preparing','running','validated',
                                           'publishing','operator_attention'))
                        LIMIT 1""",
                    (session_id, task_id or "", int(claim_free)),
                ).fetchone()
            if active is not None:
                raise LlmCoordError(
                    ErrorCode.TASK_NOT_MUTABLE,
                    "finish the session's current task before another prompt",
                    {"active_task_id": str(active["task_id"])},
                )
        claim_only = bool(params.get("claim_only", False))
        if (
            session is not None
            and session.agent_mode != "auto"
            and (fixture_values or claim_only)
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
        if one_shot:
            launch_parameters["one_shot_session"] = True
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
                workspace_execution=claim_free,
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
        if claim_free:
            if session is None or session.workspace_id is None or driver is None:
                raise ValueError(
                    "workspace execution requires a durable coding session"
                )
            if not self._shared_capable(driver):
                raise LlmCoordError(
                    ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED,
                    "driver does not support workspace execution",
                )
            workspace = self.store.get_workspace(session.workspace_id)
            if workspace is None:
                raise ValueError("session workspace is missing")
            existing = self.store.get_execution(task.task_id, task.attempt)
            if existing is not None and not isinstance(
                existing, WorkspaceExecutionRecord
            ):
                raise LlmCoordError(
                    ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED,
                    "legacy attempt remains inspectable or exportable",
                )
            source = await asyncio.to_thread(
                inspect_repository,
                Path(workspace.canonical_path),
                profile_id=self.paths.profile_id,
            )
            execution = existing or self.store.create_workspace_execution(
                task_id=task.task_id,
                attempt=task.attempt,
                session_id=session.session_id,
                workspace_id=workspace.workspace_id,
                scopes=normalize_scopes(
                    scopes,
                    case_insensitive_filesystem=requested_checkout.path_case_insensitive,
                ),
                physical_key=str(regime["physical_key"]),
                regime_generation=int(regime["generation"]),
                driver=driver.name,
                worktree_path=workspace.canonical_path,
                base_oid=source.base_oid,
                boot_id=self.boot_id,
            )
            assert launch is not None
            if execution.state in {"published", "failed", "operator_attention"}:
                return {
                    "task": self._task_view(task.task_id),
                    "claim": None,
                    "execution": execution.state,
                    "execution_contract": "workspace_v1",
                }
            scheduled, event = self._schedule_workspace_execution(
                repository=execution_repository,
                task=task,
                execution=execution,
                driver=driver,
                launch=launch,
            )
            return {
                "task": self._task_view(task.task_id),
                "claim": None,
                "execution": scheduled,
                "driver": driver.name,
                "execution_contract": "workspace_v1",
                "event_sequence": event.sequence,
                "notice": "poll 'loupe task events TASK_ID' for lifecycle changes",
            }
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
            and session.workspace_mode in {"shared", "isolated"}
            and driver is not None
            and self._shared_capable(driver)
        ):
            scheduling_mode = "optimistic"
            optimistic_driver = driver.name
        claim_checkout = (
            requested_checkout
            if session is not None
            else await self._registered_checkout_for_path(
                Path(execution_repository.main_worktree_path), repository
            )
        )
        claim = self.coordinator.request_claim(
            task.task_id,
            scopes,
            scheduling_mode=scheduling_mode,
            optimistic_driver=optimistic_driver,
            checkout_id=claim_checkout.checkout_id,
        )
        if session is None:
            with self.physical_authority.mutation(
                Path(execution_repository.main_worktree_path),
                "legacy_claim",
                claim.claim_id,
            ) as operation:
                operation.confirm()
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
        self._schedule_ready_workspace_launches()
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
            if (
                self.settings.agent_command_environment == "session"
                and task.session_id is not None
            ):
                try:
                    self.session_environments.get(task.session_id)
                except LlmCoordError:
                    row = self.workflow.get(task.task_id)
                    session = self.store.get_session(task.session_id)
                    if (row and row["stop_requested"]) or (
                        session and session.state == "closed"
                    ):
                        self._finish_environment_waiter(task.task_id)
                        continue
                    if claim.fencing_token is not None:
                        with suppress(CoordinationError):
                            self.coordinator.renew_claim(
                                task_id=task.task_id,
                                claim_id=claim.claim_id,
                                fencing_token=claim.fencing_token,
                                attempt=task.attempt,
                            )
                    if task.task_id not in self._environment_waiters:
                        self._environment_waiters.add(task.task_id)
                        self.store.record_task_event(
                            claim_id=claim.claim_id,
                            task_id=task.task_id,
                            event_type="execution.awaiting_session_environment",
                            payload={"state": "awaiting_session_environment"},
                        )
                    continue
            self._environment_waiters.discard(task.task_id)
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
        # Durable scheduler metadata is not part of the driver's strict recipe.
        parameters = dict(launch.parameters)
        if "one_shot_session" in parameters and not isinstance(
            parameters.pop("one_shot_session"), bool
        ):
            raise ValueError("one-shot launch metadata is invalid")
        return self.drivers.create(launch.driver, parameters)

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
        if not isinstance(error, LlmCoordError):
            # The class name alone, never the message, which may echo a prompt.
            payload["error_type"] = type(error).__name__
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
            "provider",
            "model",
            "effort",
            "agent_mode",
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
        return CodingAgentHarness(
            self.providers.create(provider, model, effort=effort),
            explorations=self.settings.agent_explore,
            explore_effort=self.settings.agent_explore_effort,
            review=self.settings.agent_review,
            review_effort=self.settings.agent_review_effort,
        )

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

    def _schedule_workspace_execution(
        self,
        *,
        repository: RepositoryRecord,
        task: TaskRecord,
        execution: WorkspaceExecutionRecord,
        driver: AgentDriver,
        launch: ExecutionLaunchRecord,
    ) -> tuple[str, TaskEventRecord]:
        key = (task.task_id, task.attempt)
        existing = self._background_tasks.get(key)
        observed = existing is not None and not existing.done()
        with self._workspace_workers_lock:
            observed = observed or key in self._workspace_workers
        event = self.store.record_task_event(
            task_id=task.task_id,
            claim_id=None,
            event_type="execution.schedule_observed"
            if observed
            else "execution.scheduled",
            payload={
                "attempt": task.attempt,
                "driver": driver.name,
                "execution_contract": "workspace_v1",
            },
        )
        if observed:
            return "already_scheduled", event
        root = Path(repository.main_worktree_path)
        pin = self.physical_authority.acquire(root)
        started = threading.Event()
        background = asyncio.create_task(
            self._run_workspace_execution(
                pin=pin,
                started=started,
                repository=repository,
                task=task,
                execution=execution,
                driver=driver,
                launch=launch,
            ),
            name=f"workspace-execution:{task.task_id}:{task.attempt}",
        )
        self._background_tasks[key] = background

        def forget(completed: asyncio.Task[None]) -> None:
            self._forget_background_task(key, completed)
            if not started.is_set():
                pin.close()

        background.add_done_callback(forget)
        return "scheduled", event

    async def _run_workspace_execution(
        self,
        *,
        pin: OwnershipPin,
        started: threading.Event,
        repository: RepositoryRecord,
        task: TaskRecord,
        execution: WorkspaceExecutionRecord,
        driver: AgentDriver,
        launch: ExecutionLaunchRecord,
    ) -> None:
        started.set()
        submitted = False

        def execute_pinned() -> None:
            try:
                pin.validate()
                with self.physical_authority.pin(Path(repository.main_worktree_path)):
                    self.physical_authority.assert_regime(
                        Path(repository.main_worktree_path),
                        allowed=("workspace",),
                        generation=execution.regime_generation,
                    )
                    conversation, coordination = self._session_task_context(task)
                    self.shared_runner.execute_workspace(
                        repository=repository,
                        task=task,
                        execution=execution,
                        driver=driver,
                        instructions=launch.instructions,
                        asker=functools.partial(self._ask_operator, task.task_id)
                        if launch.interactive
                        else None,
                        conversation_state=conversation,
                        coordination_context=coordination,
                    )
            except Exception as exc:
                self._fail_workspace_launch(execution, exc)
            finally:
                # Follow the actual worker, even if its asyncio waiter was
                # cancelled while a provider or command was still running.
                pin.close()

        key = (task.task_id, task.attempt)
        with self._workspace_workers_lock:
            self._workspace_workers.add(key)

        def completed_worker(_completed: object) -> None:
            with self._workspace_workers_lock:
                self._workspace_workers.discard(key)
            try:
                self._close_one_shot_if_settled(task, launch)
            finally:
                pin.close()

        try:
            worker = self._execution_pool.submit(
                contextvars.copy_context().run, execute_pinned
            )
            submitted = True
            worker.add_done_callback(completed_worker)
            await asyncio.wrap_future(worker)
        except asyncio.CancelledError:
            self.store.record_task_event(
                task_id=task.task_id,
                claim_id=None,
                event_type="execution.interrupted",
                payload={
                    "reason": "daemon_shutdown",
                    "execution_contract": "workspace_v1",
                },
            )
            raise
        finally:
            if not submitted:
                with self._workspace_workers_lock:
                    self._workspace_workers.discard(key)
                pin.close()

    def _fail_workspace_launch(
        self, execution: WorkspaceExecutionRecord, error: Exception
    ) -> None:
        code = (
            error.code.value if isinstance(error, LlmCoordError) else "EXECUTION_FAILED"
        )
        with self.workspace_batches.lock:
            fresh = self.store.get_execution_by_id(execution.execution_id)
            batch = self.workspace_batches.get(execution.execution_id)
            # A journaled outcome belongs to exact recovery, not this error.
            if (
                fresh is not None
                and fresh.state not in {"published", "failed"}
                and batch is None
            ):
                try:
                    self.coordinator.settle_workspace_execution(
                        execution.execution_id,
                        outcome="failed_safe",
                        failure_code=code,
                    )
                except (CoordinationError, LlmCoordError):
                    self.store.update_execution(
                        execution.execution_id,
                        state="operator_attention",
                        failure_code=code,
                    )
        self.store.record_task_event(
            task_id=execution.task_id,
            claim_id=None,
            event_type="execution.background_failed",
            payload={
                "failure_code": code,
                "error_type": type(error).__name__,
                "execution_contract": "workspace_v1",
            },
        )

    def _close_one_shot_if_settled(
        self, task: TaskRecord, launch: ExecutionLaunchRecord
    ) -> None:
        if launch.parameters.get("one_shot_session") is not True:
            return
        with self._workspace_workers_lock:
            if (task.task_id, task.attempt) in self._workspace_workers:
                return
        execution = self.store.get_execution(task.task_id, task.attempt)
        if (
            execution is not None
            and execution.state in {"published", "failed"}
            and task.session_id is not None
        ):
            self.store.close_session(task.session_id, reason="one_shot_settled")
            self.session_environments.discard(task.session_id)

    def _schedule_ready_workspace_launches(self) -> None:
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT task_id,task_attempt FROM workspace_executions "
                "WHERE state IN ('preparing','running') "
                "ORDER BY created_at,execution_id"
            ).fetchall()
        for row in rows:
            task = self.store.get_task(str(row["task_id"]))
            if task is None or task.attempt != int(row["task_attempt"]):
                continue
            execution = self.store.get_execution(task.task_id, task.attempt)
            launch = self.store.get_execution_launch(task.task_id, task.attempt)
            if not isinstance(execution, WorkspaceExecutionRecord) or launch is None:
                continue
            if execution.boot_id != self.boot_id or not self._can_reconstruct_launch(
                launch
            ):
                continue
            workflow = self.workflow.get(task.task_id)
            session = self.store.get_session(execution.session_id)
            if (workflow and workflow["stop_requested"]) or (
                session is not None and session.state == "closed"
            ):
                self._finish_environment_waiter(task.task_id)
                self._close_one_shot_if_settled(task, launch)
                continue
            if self.settings.agent_command_environment == "session":
                try:
                    self.session_environments.get(execution.session_id)
                except LlmCoordError:
                    self._environment_waiters.add(task.task_id)
                    continue
            repository = self.store.get_repository_by_key(task.repo_key)
            if repository is None:
                continue
            self._schedule_workspace_execution(
                repository=self._execution_repository(repository, task),
                task=task,
                execution=execution,
                driver=self._driver_for_launch(launch),
                launch=launch,
            )

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
        pin = self.physical_authority.acquire(Path(repository.main_worktree_path))
        ownership_started = threading.Event()
        background = asyncio.create_task(
            self._run_execution(
                ownership_pin=pin,
                ownership_started=ownership_started,
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

        def forget(completed: asyncio.Task[None]) -> None:
            self._forget_background_task(key, completed)
            if not ownership_started.is_set():
                pin.close()

        background.add_done_callback(forget)
        return "scheduled", event

    def _forget_background_task(
        self, key: tuple[str, int], completed: asyncio.Task[None]
    ) -> None:
        if self._background_tasks.get(key) is completed:
            self._background_tasks.pop(key, None)

    async def _run_execution(
        self,
        *,
        ownership_pin: OwnershipPin,
        ownership_started: threading.Event,
        repository: RepositoryRecord,
        task: TaskRecord,
        claim: ClaimRecord,
        driver: AgentDriver,
        instructions: str = "",
        interactive: bool = False,
    ) -> None:
        """Run outside the request lock and surface failure to every session."""

        ownership_started.set()
        submitted = False

        def execute_pinned(**kwargs: Any) -> None:
            try:
                ownership_pin.validate()
                with self.physical_authority.pin(Path(repository.main_worktree_path)):
                    self._execute(**kwargs)
            finally:
                ownership_pin.close()

        try:
            operation = functools.partial(
                contextvars.copy_context().run,
                execute_pinned,
                repository=repository,
                task=task,
                claim=claim,
                driver=driver,
                instructions=instructions,
                interactive=interactive,
            )
            # A concurrent-future callback follows the actual worker lifetime,
            # even when cancelling the asyncio waiter cannot stop that thread.
            worker = self._execution_pool.submit(operation)
            submitted = True
            worker.add_done_callback(lambda _completed: ownership_pin.close())
            await asyncio.wrap_future(worker)
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
        finally:
            if not submitted:
                ownership_pin.close()

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
        if session.workspace_mode in {"shared", "isolated"}:
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

    def _session_usage(self, session: SessionRecord) -> dict[str, object]:
        """Total token usage across a session's task runs, and its context size.

        A run that has not recorded final usage, because it is still running
        or it failed, contributes the usage in its latest checkpoint.
        """

        totals: dict[str, int] = {}
        runs = 0
        live_context: int | None = None
        for execution_id, state, usage in self.store.session_execution_usage(
            session.session_id
        ):
            live = state in _LIVE_EXECUTION_STATES
            checkpoint: Mapping[str, object] | None = None
            if live or not usage:
                record = self.store.get_execution_checkpoint(execution_id)
                checkpoint = record.checkpoint if record is not None else None
            if not usage and checkpoint is not None:
                usage = _counts(checkpoint.get("usage_total"))
            if "prompt_tokens" not in usage and "input_tokens" in usage:
                # Runs recorded before prompt_tokens existed. Anthropic's
                # input_tokens excluded cached tokens, so these may undercount.
                usage = {**usage, "prompt_tokens": usage["input_tokens"]}
            if usage:
                runs += 1
                for key, value in usage.items():
                    totals[key] = totals.get(key, 0) + value
            if live and checkpoint is not None:
                tokens = checkpoint.get("context_tokens")
                if type(tokens) is int and tokens >= 0:
                    live_context = tokens
        context = live_context
        if context is None:
            prior = self.store.session_conversation(session.session_id)
            tokens = prior[2].get("context_tokens") if prior is not None else None
            context = tokens if type(tokens) is int and tokens >= 0 else None
        return {
            "task_runs": runs,
            "usage": totals,
            "context_tokens": context,
            "context_budget": self._context_budget(session),
        }

    def _context_budget(self, session: SessionRecord) -> int | None:
        """The session model's input budget; adapter construction is local."""

        try:
            provider = self.providers.create(
                session.provider, session.model, effort=session.effort
            )
        except (LlmCoordError, ValueError):
            return None
        budget = getattr(provider, "input_token_budget", None)
        return budget if type(budget) is int and budget > 0 else None

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
        return {
            "compacted": True,
            "context_tokens": before,
            "summary_tokens": after,
            "context_budget": self._context_budget(session),
        }

    async def _session_retry_answer(
        self, params: Mapping[str, Any]
    ) -> dict[str, object]:
        """Write the last task's interrupted answer again, without its tools.

        This is the explicit response-only retry: one tools-disabled request
        from the conversation, draft, and settlement facts the interrupted
        answer had. It never reruns edits or reacquires publication authority.
        """

        async with self._authority_lock:
            session = self._authenticated_session(params, require_active=True)
            if self.store.session_has_active_task(session.session_id):
                raise LlmCoordError(
                    ErrorCode.TASK_NOT_MUTABLE,
                    "a task is still running in this conversation; let it finish "
                    "before writing an answer again",
                )
            execution = self.store.latest_session_execution(session.session_id)
            saved = (
                self.store.get_execution_checkpoint(execution.execution_id)
                if execution is not None
                else None
            )
            state = checkpoint_finalization(saved.checkpoint) if saved else None
            if (
                execution is None
                or saved is None
                or state is None
                or state.status != "interrupted"
                or not saved.terminal_event_persisted
            ):
                return {"retried": False, "reason": "complete"}
            native = saved.checkpoint.get("session")
            if not isinstance(native, Mapping):
                raise ValueError("an interrupted answer has no saved conversation")
            if execution.execution_id in self._answer_rewrites:
                return {"retried": False, "reason": "in_progress"}
            if self.store.answer_revisions(execution.execution_id):
                return {"retried": False, "reason": "rewritten"}
            launch = self.store.get_execution_launch(
                execution.task_id, execution.task_attempt
            )
            if launch is None:
                raise KeyError("execution launch")
            driver = self._driver_for_launch(launch)
            if not isinstance(driver, CodingAgentHarness):
                raise LlmCoordError(
                    ErrorCode.PROTOCOL_MISMATCH,
                    "this task's driver cannot write its answer again",
                )
            self._answer_rewrites.add(execution.execution_id)
            # Recorded before the request, so every paid attempt is visible.
            self.store.record_task_event(
                task_id=execution.task_id,
                claim_id=execution.claim_id,
                event_type="model.answer_retrying",
                payload={"attempt": "retry"},
            )
        try:
            rewritten = await asyncio.to_thread(
                driver.rewrite_answer,
                saved.checkpoint,
                instructions=launch.instructions,
            )
        except Exception as exc:
            async with self._authority_lock:
                self._answer_rewrites.discard(execution.execution_id)
                self.store.record_task_event(
                    task_id=execution.task_id,
                    claim_id=execution.claim_id,
                    event_type="model.answer_retry_failed",
                    payload={"error": type(exc).__name__},
                )
            if isinstance(exc, LlmCoordError):
                raise
            raise LlmCoordError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "the answer could not be written again; run /retry-answer to try "
                "once more",
            ) from exc
        async with self._authority_lock:
            try:
                after, promoted = self.store.save_answer_revision(
                    execution.execution_id,
                    text=rewritten.text,
                    usage=rewritten.usage,
                    session=rewritten.session,
                    replaces=native,
                    context_tokens=rewritten.context_tokens,
                )
            finally:
                self._answer_rewrites.discard(execution.execution_id)
        return {
            "retried": True,
            "task_id": execution.task_id,
            # The rewrite's events follow this sequence on the task's stream.
            "after": after - 1,
            "conversation_updated": promoted,
        }

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
        return self._registered_repository_for_info(info)

    def _registered_repository_for_info(self, info: RepositoryInfo) -> RepositoryRecord:
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
            "capabilities": list(WORKSPACE_CAPABILITIES),
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
        for name, database_path in (
            ("control", self.paths.control_db),
            ("knowledge", self.paths.knowledge_db),
            ("vectors", self.paths.vectors_db),
        ):
            if not database_path.exists():
                statuses[name] = {"path": str(database_path), "state": "not_created"}
                continue
            try:
                if name == "knowledge":
                    validate_owned_sqlite_paths(database_path)
                with closing(
                    sqlite3.connect(
                        f"{database_path.as_uri()}?mode=ro", uri=True, timeout=0.25
                    )
                ) as connection:
                    connection.row_factory = sqlite3.Row
                    statuses[name] = {
                        "path": str(database_path),
                        "state": "available",
                        "bytes": database_path.stat().st_size,
                        "schema_version": current_schema_version(connection),
                        "journal_mode": str(
                            connection.execute("PRAGMA journal_mode").fetchone()[0]
                        ).lower(),
                        "synchronous": int(
                            connection.execute("PRAGMA synchronous").fetchone()[0]
                        ),
                    }
            except (sqlite3.Error, OSError, LlmCoordError) as exc:
                statuses[name] = {
                    "path": str(database_path),
                    "state": "unavailable",
                    "error": type(exc).__name__,
                }
        statuses["knowledge"]["index"] = self.knowledge.status()
        return statuses

    def _db_check(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, path in (
            ("control", self.paths.control_db),
            ("knowledge", self.paths.knowledge_db),
            ("vectors", self.paths.vectors_db),
        ):
            if not path.exists() and name != "control":
                result[name] = {"ok": True, "detail": "not created (optional)"}
                continue
            try:
                if name == "knowledge":
                    validate_owned_sqlite_paths(path)
                with closing(
                    sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.25)
                ) as connection:
                    value = connection.execute("PRAGMA integrity_check").fetchone()
                detail = str(value[0]) if value is not None else "no result"
                result[name] = {"ok": detail == "ok", "detail": detail}
            except (sqlite3.Error, OSError, LlmCoordError) as exc:
                result[name] = {"ok": False, "detail": type(exc).__name__}
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


def _counts(value: object) -> dict[str, int]:
    """Read a checkpoint's token counts, ignoring anything malformed."""

    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): count
        for key, count in value.items()
        if isinstance(key, str) and type(count) is int and count >= 0
    }


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
