"""The task lifecycle every driver runs inside, whatever decides the changes.

This module owns the parts a driver is never trusted with: converting the
launch lease to a work lease and renewing it, creating the linked worktree at
an exact base commit, validating the real Git tree against the claim, and
compare-and-swapping the result into place.  The driver only decides file
contents, and it does so through a tool broker bounded by the same claim.
"""

from __future__ import annotations

import functools
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from llm_cli.agent.driver import AgentDriver, DriverCapabilities, RunRequest, RunResult
from llm_cli.agent.limits import DEFAULT_LIMITS, ExecutionLimits
from llm_cli.agent.tools import TaskCancelled, ToolBroker
from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import (
    ClaimAuthorityError,
    ClaimConflict,
    ClaimRecord,
    ClaimState,
    CoordinationError,
    ExecutionRecord,
    PublicationIntentRecord,
    RepositoryRecord,
    TaskRecord,
)
from llm_cli.coordination.scopes import ScopeValidationError, normalize_changed_path
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.inspect import inspect_repository
from llm_cli.git.integrate import (
    TaskRefPublication,
    create_result_commit,
    publish_task_ref,
    read_task_ref,
)
from llm_cli.git.validate import WorktreeValidation, validate_worktree
from llm_cli.git.worktrees import (
    ManagedWorktree,
    create_managed_worktree,
    remove_managed_worktree,
    resume_managed_worktree,
)
from llm_cli.storage.control import ControlStore
from llm_cli.web.access import WebAccess

_MAX_FIXTURE_WRITES = 50
_MAX_FIXTURE_CONTENT_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class FixtureWrite:
    """One explicit fixture-driver write, expressed as a Git-relative path."""

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Durable outcome of a completed fixture execution attempt."""

    task: TaskRecord
    claim: ClaimRecord
    execution: ExecutionRecord
    worktree_cleaned: bool
    run: RunResult
    validation: WorktreeValidation | None = None
    publication_intent: PublicationIntentRecord | None = None
    publication: TaskRefPublication | None = None


def parse_fixture_writes(values: list[str]) -> tuple[FixtureWrite, ...]:
    """Parse ``PATH=CONTENT`` CLI values without accepting arbitrary commands."""

    if not values:
        raise ValueError("at least one fixture write is required")
    if len(values) > _MAX_FIXTURE_WRITES:
        raise ValueError(f"fixture execution exceeds {_MAX_FIXTURE_WRITES} writes")
    writes: list[FixtureWrite] = []
    seen: set[str] = set()
    for value in values:
        path, separator, content = value.partition("=")
        if not separator:
            raise ValueError("fixture writes must use PATH=CONTENT")
        try:
            canonical_path = normalize_changed_path(path)
        except ScopeValidationError as exc:
            raise ValueError(f"fixture write path is unsafe: {exc}") from exc
        if canonical_path in seen:
            raise ValueError("fixture writes must not target the same path twice")
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_FIXTURE_CONTENT_BYTES:
            raise ValueError(
                f"fixture write content exceeds {_MAX_FIXTURE_CONTENT_BYTES} bytes"
            )
        seen.add(canonical_path)
        writes.append(FixtureWrite(path=canonical_path, content=content))
    return tuple(writes)


class TaskExecutionRunner:
    """Run one driver through the fenced claim, worktree, and publication path."""

    def __init__(
        self,
        store: ControlStore,
        coordinator: RepositoryCoordinator,
        *,
        managed_worktree_root: Path,
        boot_id: str,
        limits: ExecutionLimits = DEFAULT_LIMITS,
        renewal_interval_seconds: float = 30.0,
    ) -> None:
        if not boot_id:
            raise ValueError("a runner must identify the daemon boot that owns it")
        if renewal_interval_seconds <= 0:
            raise ValueError("the lease renewal interval must be positive")
        self.store = store
        self.coordinator = coordinator
        self.managed_worktree_root = managed_worktree_root
        self.boot_id = boot_id
        self.limits = limits
        self.renewal_interval_seconds = renewal_interval_seconds
        # Read-only web_fetch policy; see Settings.agent_web_fetch.
        self.web_fetch = "off"
        self.web_domains: tuple[str, ...] = ()

    def web_access(self) -> WebAccess | None:
        """A fresh web policy for one task, or None when web_fetch is off."""

        if self.web_fetch == "off":
            return None
        return WebAccess(self.web_fetch, self.web_domains)

    def execute(
        self,
        *,
        repository: RepositoryRecord,
        task: TaskRecord,
        claim: ClaimRecord,
        driver: AgentDriver,
        instructions: str = "",
        asker: Callable[[str], str] | None = None,
        conversation_state: Mapping[str, object] | None = None,
        coordination_context: str | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ExecutionResult:
        """Execute, validate, branch-publish, and confirm one active claim.

        Failure before ``begin_publication`` releases the active claim and
        wakes eligible waiters.  Once a publication intent exists, the claim
        intentionally stays blocking: a crash or Git-side-effect ambiguity is
        never treated as a safe release.
        """

        if claim.scheduling_mode != "exclusive":
            raise ClaimConflict("isolated execution requires an exclusive claim")
        if claim.state is not ClaimState.ACTIVE_WORK or claim.fencing_token is None:
            raise ClaimConflict("execution requires an active work claim")
        if claim.task_id != task.task_id or claim.task_attempt != task.attempt:
            raise ClaimConflict("claim does not belong to this task attempt")

        repo_path = Path(repository.main_worktree_path)
        info = inspect_repository(
            repo_path,
            profile_id=repository.profile_id,
            target=repository.target_ref,
            coordinate_by_remote=repository.coordinate_by_remote,
        )
        if (
            info.repo_key != repository.repo_key
            or info.target_ref != repository.target_ref
        ):
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                "registered repository identity no longer matches its Git target",
            )

        existing = self.store.get_execution(task.task_id, task.attempt)
        resuming = existing is not None and existing.state == "running"
        if existing is not None and existing.state not in {"preparing", "running"}:
            raise ClaimConflict(
                "task attempt already has a durable execution; recovery is required"
            )
        if existing is not None and existing.driver != driver.name:
            raise ClaimConflict("task attempt belongs to a different execution driver")
        if resuming and not driver.capabilities.resumable:
            raise ClaimConflict("this execution driver cannot resume a prior turn")
        planned_path = (
            Path(existing.worktree_path)
            if existing is not None
            else self.managed_worktree_root / task.task_id
        )
        base_oid = existing.base_oid if existing is not None else info.base_oid
        execution = self.store.create_execution(
            task_id=task.task_id,
            attempt=task.attempt,
            claim_id=claim.claim_id,
            driver=driver.name,
            worktree_path=str(planned_path.absolute()),
            base_oid=base_oid,
            boot_id=self.boot_id,
        )

        managed: ManagedWorktree | None = None
        publication_started = False
        retain_unpublished = False
        validation: WorktreeValidation | None = None
        confirmed_intent: PublicationIntentRecord | None = None
        publication: TaskRefPublication | None = None
        try:
            # Convert the short launch lease to a worker lease before any
            # filesystem preparation. Future long-running drivers renew on a
            # timer in addition to these boundary renewals.
            self.coordinator.renew_claim(
                task_id=task.task_id,
                claim_id=claim.claim_id,
                fencing_token=claim.fencing_token,
                attempt=task.attempt,
            )
            if resuming:
                managed = resume_managed_worktree(
                    repo_path,
                    managed_root=self.managed_worktree_root,
                    registered_path=planned_path,
                    task_id=task.task_id,
                    base_oid=base_oid,
                )
            else:
                managed = create_managed_worktree(
                    repo_path,
                    managed_root=self.managed_worktree_root,
                    task_id=task.task_id,
                    base_oid=base_oid,
                    worktree_path=planned_path,
                )
                execution = self.store.update_execution(
                    execution.execution_id, state="worktree_ready"
                )
                execution = self.store.update_execution(
                    execution.execution_id, state="running"
                )
            checkpoint = self.store.get_execution_checkpoint(execution.execution_id)
            if resuming and checkpoint is None:
                raise ClaimConflict("resumable execution has no driver checkpoint")
            broker = ToolBroker(
                worktree=managed.path,
                scopes=claim.scopes,
                case_insensitive_filesystem=repository.path_case_insensitive,
                base_oid=base_oid,
                limits=self.limits,
                on_event=self._event_recorder(task, claim),
                asker=asker,
                cancelled=cancelled,
                web=self.web_access(),
            )
            request = RunRequest(
                task_id=task.task_id,
                attempt=task.attempt,
                instructions=instructions or task.title,
                scopes=claim.scopes,
                worktree=managed.path,
                base_oid=base_oid,
                conversation_state=conversation_state,
                coordination_context=coordination_context,
                resume_state=(
                    checkpoint.checkpoint if checkpoint is not None else None
                ),
                checkpoint=functools.partial(
                    self._checkpoint_execution,
                    execution_id=execution.execution_id,
                    driver=driver.name,
                ),
            )
            # A driver turn can outlast the work lease many times over, so the
            # lease is renewed on a timer for as long as the driver runs rather
            # than only at these lifecycle boundaries.
            with self._renewing_lease(task=task, claim=claim):
                run = driver.run(request, broker)

            self.coordinator.renew_claim(
                task_id=task.task_id,
                claim_id=claim.claim_id,
                fencing_token=claim.fencing_token,
                attempt=task.attempt,
            )
            self.store.record_task_event(
                task_id=task.task_id,
                claim_id=claim.claim_id,
                event_type="driver.finished",
                payload={
                    "driver": driver.name,
                    "tool_calls": run.tool_calls,
                    "enforces_scope": driver.capabilities.enforces_scope,
                },
            )
            if run.outcome != "completed":
                retain_unpublished = True
                raise LlmCoordError(
                    ErrorCode.PROVIDER_AMBIGUOUS,
                    "the agent ended without a complete result; its isolated "
                    "worktree was retained for inspection",
                    details={"outcome": run.outcome, "answer_saved": bool(run.answer)},
                )
            broker.check_cancelled()
            validation = validate_worktree(
                managed,
                scopes=claim.scopes,
                case_insensitive_filesystem=repository.path_case_insensitive,
            )
            if not validation.changed_paths:
                self.coordinator.settle_read_only_execution(
                    execution.execution_id,
                    fencing_token=claim.fencing_token,
                    result_tree_id=validation.result_tree,
                    patch_hash=validation.patch_sha256,
                    summary=run.summary,
                    tool_calls=run.tool_calls,
                    usage=run.usage,
                )
                completed_execution = self.store.get_execution(
                    task.task_id, task.attempt
                )
                assert completed_execution is not None
                execution = completed_execution
            else:
                result_commit_id = create_result_commit(
                    managed.path,
                    validation,
                    task_id=task.task_id,
                )
                execution = self.store.update_execution(
                    execution.execution_id,
                    state="validated",
                    result_tree_id=validation.result_tree,
                    result_commit_id=result_commit_id,
                    patch_hash=validation.patch_sha256,
                    summary=run.summary,
                    tool_calls=run.tool_calls,
                    usage=run.usage,
                )
                # Read the predecessor before the intent is persisted so the
                # durable record names exactly what the swap expects to replace.
                # A later attempt of this task advances its ref; demanding absence
                # would make every attempt after the first fail to publish.
                expected_old_oid = read_task_ref(repo_path, task.task_id)
                intent = self.coordinator.begin_publication(
                    task_id=task.task_id,
                    claim_id=claim.claim_id,
                    fencing_token=claim.fencing_token,
                    attempt=task.attempt,
                    changed_paths=validation.changed_paths,
                    patch_hash=validation.patch_sha256,
                    result_tree_id=validation.result_tree,
                    result_commit_id=result_commit_id,
                    expected_old_ref=expected_old_oid,
                )
                publication_started = True
                execution = self.store.update_execution(
                    execution.execution_id, state="publishing"
                )
                publication = publish_task_ref(
                    repo_path,
                    task_id=task.task_id,
                    new_commit_oid=result_commit_id,
                    expected_old_oid=expected_old_oid,
                )
                confirmed_intent = self.coordinator.confirm_publication(
                    intent.intent_id,
                    result_commit_id=publication.commit_oid,
                    task_ref=publication.ref,
                )
                execution = self.store.update_execution(
                    execution.execution_id,
                    state="published",
                    result_commit_id=publication.commit_oid,
                )
        except Exception as exc:
            failure_code = _failure_code(exc)
            self._record_failure(
                execution,
                failure_code,
                publication_started=publication_started,
            )
            if not publication_started:
                self._release_after_safe_failure(claim, failure_code)
            if not isinstance(exc, TaskCancelled) and not retain_unpublished:
                self._remove_after_safe_failure(repo_path, managed)
            raise

        try:
            remove_managed_worktree(
                repo_path,
                managed_root=self.managed_worktree_root,
                registered_path=managed.path,
                force=True,
            )
        except LlmCoordError as cleanup_error:
            execution = self.store.update_execution(
                execution.execution_id,
                state="cleanup_failed",
                failure_code=cleanup_error.code.value,
            )
            return ExecutionResult(
                task=self._fresh_task(task.task_id),
                claim=self._fresh_claim(claim.claim_id),
                execution=execution,
                validation=validation,
                publication_intent=confirmed_intent,
                publication=publication,
                worktree_cleaned=False,
                run=run,
            )

        return ExecutionResult(
            task=self._fresh_task(task.task_id),
            claim=self._fresh_claim(claim.claim_id),
            execution=execution,
            validation=validation,
            publication_intent=confirmed_intent,
            publication=publication,
            worktree_cleaned=True,
            run=run,
        )

    def _event_recorder(
        self, task: TaskRecord, claim: ClaimRecord
    ) -> Callable[[str, dict[str, object]], None]:
        """Publish driver progress to the stream every session can read.

        Telemetry must never be able to fail an execution, so a store error
        here is dropped: the work is authoritative, the commentary is not.
        """

        def record(event_type: str, payload: dict[str, object]) -> None:
            with suppress(sqlite3.Error, KeyError, ValueError):
                self.store.record_task_event(
                    task_id=task.task_id,
                    claim_id=claim.claim_id,
                    event_type=event_type,
                    payload=payload,
                )

        return record

    def _checkpoint_execution(
        self,
        state: Mapping[str, object],
        *,
        execution_id: str,
        driver: str,
    ) -> bool:
        """Persist opaque harness/provider state for exact execution resumption."""

        checkpoint = self.store.save_execution_checkpoint(
            execution_id=execution_id,
            driver=driver,
            checkpoint=state,
        )
        return checkpoint.terminal_event_persisted

    @contextmanager
    def _renewing_lease(
        self, *, task: TaskRecord, claim: ClaimRecord
    ) -> Iterator[None]:
        """Hold the work lease alive for as long as the driver is running.

        Renewal happens on a daemon thread because the driver owns the worker
        thread and may block in one provider call for minutes.  A renewal that
        fails is not raised at the driver -- there is no safe way to interrupt
        it mid-write -- but it is raised the moment the driver returns, so a
        lost lease can never reach publication.
        """

        fencing_token = claim.fencing_token
        assert fencing_token is not None
        stop = threading.Event()
        lost: list[BaseException] = []

        def renew() -> None:
            while not stop.wait(self.renewal_interval_seconds):
                try:
                    self.coordinator.renew_claim(
                        task_id=task.task_id,
                        claim_id=claim.claim_id,
                        fencing_token=fencing_token,
                        attempt=task.attempt,
                    )
                except CoordinationError as exc:
                    lost.append(exc)
                    return

        ticker = threading.Thread(
            target=renew,
            name=f"lease-renewal:{task.task_id}:{task.attempt}",
            daemon=True,
        )
        ticker.start()
        try:
            yield
        finally:
            stop.set()
            ticker.join(timeout=10)
        if lost:
            raise ClaimAuthorityError(
                f"the work lease was lost while the driver ran: {lost[0]}"
            )

    def _record_failure(
        self,
        execution: ExecutionRecord,
        failure_code: str,
        *,
        publication_started: bool,
    ) -> None:
        # The primary failure is more actionable, and publication authority
        # must never be weakened because telemetry persistence also failed.
        with suppress(KeyError, ValueError):
            self.store.update_execution(
                execution.execution_id,
                state="operator_attention" if publication_started else "failed",
                failure_code=failure_code,
            )

    def _release_after_safe_failure(
        self, claim: ClaimRecord, failure_code: str
    ) -> None:
        # A concurrent reconciler or an impossible state transition must not
        # mask the original failure. Any live reservation remains safe.
        with suppress(CoordinationError):
            self.coordinator.release_claim(
                claim.claim_id,
                reason="driver_execution_failed",
                expected_fencing_token=claim.fencing_token,
                task_state="cancelled" if failure_code == "CANCELLED" else "failed",
                failure_code=failure_code,
            )

    def _remove_after_safe_failure(
        self, repository: Path, managed: ManagedWorktree | None
    ) -> None:
        if managed is None:
            return
        # A dirty or unavailable worktree remains exactly where the durable
        # record says it is; no broad filesystem cleanup is attempted.
        with suppress(LlmCoordError):
            remove_managed_worktree(
                repository,
                managed_root=self.managed_worktree_root,
                registered_path=managed.path,
                force=True,
            )

    def _fresh_task(self, task_id: str) -> TaskRecord:
        task = self.store.get_task(task_id)
        if task is None:
            raise RuntimeError("task disappeared during its authoritative execution")
        return task

    def _fresh_claim(self, claim_id: str) -> ClaimRecord:
        claim = self.store.get_claim(claim_id)
        if claim is None:
            raise RuntimeError("claim disappeared during its authoritative execution")
        return claim


class FixtureWriteDriver:
    """A deterministic driver that writes explicitly supplied file contents.

    It writes straight to the worktree rather than through the tool broker, and
    says so by declaring ``enforces_scope=False``.  That is the point: it stands
    in for a cooperative external driver, so every run of it proves that trusted
    validation -- not the tool surface -- is what actually stops an out-of-scope
    change from being published.
    """

    name = "fixture_write"
    capabilities = DriverCapabilities(
        tool_calling=False,
        enforces_scope=False,
        transmits_repository_contents=False,
    )

    def __init__(self, writes: tuple[FixtureWrite, ...]) -> None:
        if not writes:
            raise ValueError("fixture execution requires at least one write")
        self.writes = writes

    def run(self, request: RunRequest, tools: ToolBroker) -> RunResult:
        del tools
        for write in self.writes:
            _write_fixture_file(request.worktree, write)
        paths = ", ".join(write.path for write in self.writes)
        return RunResult(
            summary=f"wrote {paths}",
            answer=f"Wrote {paths}.",
            tool_calls=len(self.writes),
        )


class FixtureWriteRunner:
    """Run the fixture driver through the shared execution lifecycle."""

    def __init__(
        self,
        store: ControlStore,
        coordinator: RepositoryCoordinator,
        *,
        managed_worktree_root: Path,
        boot_id: str,
    ) -> None:
        self.runner = TaskExecutionRunner(
            store,
            coordinator,
            managed_worktree_root=managed_worktree_root,
            boot_id=boot_id,
        )

    @property
    def store(self) -> ControlStore:
        return self.runner.store

    @property
    def coordinator(self) -> RepositoryCoordinator:
        return self.runner.coordinator

    @property
    def managed_worktree_root(self) -> Path:
        return self.runner.managed_worktree_root

    def execute(
        self,
        *,
        repository: RepositoryRecord,
        task: TaskRecord,
        claim: ClaimRecord,
        writes: tuple[FixtureWrite, ...],
    ) -> ExecutionResult:
        return self.runner.execute(
            repository=repository,
            task=task,
            claim=claim,
            driver=FixtureWriteDriver(writes),
        )


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, TaskCancelled):
        return "CANCELLED"
    return exc.code.value if isinstance(exc, LlmCoordError) else "EXECUTION_FAILED"


def _write_fixture_file(worktree: Path, write: FixtureWrite) -> None:
    """Write only through non-symlink components beneath the linked worktree."""

    current = worktree
    components = write.path.split("/")
    for component in components[:-1]:
        candidate = current / component
        if candidate.is_symlink():
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                "fixture write would traverse a repository symlink",
            )
        if candidate.exists():
            if not candidate.is_dir():
                raise LlmCoordError(
                    ErrorCode.REPOSITORY_UNSAFE,
                    "fixture write parent is not a directory",
                )
        else:
            candidate.mkdir(mode=0o700)
        current = candidate
    target = current / components[-1]
    if target.is_symlink() or target.is_dir():
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "fixture write target is not a regular repository file",
        )
    target.write_text(write.content, encoding="utf-8")


__all__ = [
    "ExecutionResult",
    "FixtureWrite",
    "FixtureWriteDriver",
    "FixtureWriteRunner",
    "TaskExecutionRunner",
    "parse_fixture_writes",
]
