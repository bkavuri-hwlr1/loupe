"""Recovery of task executions abandoned when a daemon stopped mid-flight.

An execution only ever moves forward inside the process that started it, so
records left in a working state by an earlier boot have no owner.  Recovery
decides each one against ground truth rather than against elapsed time: the
persisted publication intent says exactly which reference swap was attempted
and what it expected to replace, and the repository says what actually
happened.  When those two do not settle the question, the reservation stays
blocking.  Guessing would either discard a published result or hand a second
agent write authority over paths the first one may already have changed.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from llm_cli.agent.driver import DriverCapabilities
from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import (
    ClaimRecord,
    ClaimState,
    CoordinationError,
    ExecutionRecord,
    PublicationIntentRecord,
    RepositoryRecord,
)
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.integrate import read_task_ref
from llm_cli.git.worktrees import prune_managed_worktrees, remove_managed_worktree
from llm_cli.storage.control import ControlStore

DAEMON_INTERRUPTED = "DAEMON_INTERRUPTED"
PUBLICATION_AMBIGUOUS = "PUBLICATION_AMBIGUOUS"
RESERVATION_WITHOUT_INTENT = "RESERVATION_WITHOUT_INTENT"
REPOSITORY_UNREADABLE = "REPOSITORY_UNREADABLE"

# An intent flagged for an operator is still undecided, so recovery re-probes
# it: the reason it could not be read may since have been repaired.
_OPEN_INTENT_STATES = frozenset(
    {"prepared", "side_effect_unknown", "operator_attention"}
)
_IN_FLIGHT_EXECUTION_STATES = frozenset(
    {"preparing", "worktree_ready", "running", "validated", "publishing"}
)
_RESERVED_CLAIM_STATES = frozenset(
    {ClaimState.PUBLISHING, ClaimState.ACTIVE_INTEGRATION}
)


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """What recovery decided about one abandoned task attempt.

    ``resolution`` is one of ``confirmed`` (the reference swap had happened),
    ``failed_safe`` (it provably had not, so the reservation was released),
    ``operator_attention`` (the outcome is unknown and the reservation stays
    blocking), ``released`` (no publication was ever attempted), or ``settled``
    (the coordination outcome was already durable and only local state was
    tidied).
    """

    task_id: str
    task_attempt: int
    resolution: str
    detail: str
    execution_id: str | None = None
    intent_id: str | None = None
    worktree_removed: bool = False

    @property
    def blocking(self) -> bool:
        return self.resolution == "operator_attention"


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Every decision one recovery pass made, in the order it made them."""

    boot_id: str
    outcomes: tuple[RecoveryOutcome, ...]

    @property
    def blocking(self) -> tuple[RecoveryOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.blocking)

    @property
    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.resolution] = counts.get(outcome.resolution, 0) + 1
        return counts


class ExecutionReconciler:
    """Resolve executions and publication intents left behind by a dead boot."""

    def __init__(
        self,
        store: ControlStore,
        coordinator: RepositoryCoordinator,
        *,
        managed_worktree_root: Path,
        boot_id: str,
        driver_capabilities: Callable[[str], DriverCapabilities | None] | None = None,
    ) -> None:
        if not boot_id:
            raise ValueError("recovery requires the current daemon boot identity")
        self.store = store
        self.coordinator = coordinator
        self.managed_worktree_root = managed_worktree_root
        self.boot_id = boot_id
        self.driver_capabilities = driver_capabilities or (lambda _name: None)

    def reconcile(self) -> RecoveryReport:
        """Decide every abandoned execution and every unresolved intent."""

        outcomes: list[RecoveryOutcome] = []
        handled: set[tuple[str, int]] = set()
        for execution in self.store.list_orphaned_executions(self.boot_id):
            # Shared checkout executions have a filesystem batch journal, not
            # a Git-ref intent. Their own reconciler owns their recovery and
            # must never send the user's checkout through worktree cleanup.
            if execution.workspace_id is not None:
                continue
            handled.add((execution.task_id, execution.task_attempt))
            if self._can_resume(execution):
                outcomes.append(self._adopt_resumable_execution(execution))
            else:
                outcomes.append(self._recover_execution(execution))
        for intent in self.store.list_open_publication_intents():
            key = (intent.task_id, intent.task_attempt)
            if key in handled:
                continue
            paired = self.store.get_execution(intent.task_id, intent.task_attempt)
            if self._in_flight_here(paired):
                continue
            handled.add(key)
            outcomes.append(self._resolve_intent(paired, intent))
        return RecoveryReport(boot_id=self.boot_id, outcomes=tuple(outcomes))

    def _can_resume(self, execution: ExecutionRecord) -> bool:
        """Whether this exact unfinished turn has the evidence to continue.

        The driver registry, durable launch, checkpoint, and live claim must all
        agree. Unknown and non-resumable drivers retain the conservative
        release path; recovery never infers capability from checkpoint data.
        """

        if execution.state != "running":
            return False
        capabilities = self.driver_capabilities(execution.driver)
        if capabilities is None or not capabilities.resumable:
            return False
        launch = self.store.get_execution_launch(
            execution.task_id, execution.task_attempt
        )
        if launch is None or launch.driver != execution.driver:
            return False
        claim = self.store.get_claim(execution.claim_id)
        if claim is None or claim.state is not ClaimState.ACTIVE_WORK:
            return False
        checkpoint = self.store.get_execution_checkpoint(execution.execution_id)
        return checkpoint is not None and checkpoint.driver == execution.driver

    def _adopt_resumable_execution(self, execution: ExecutionRecord) -> RecoveryOutcome:
        """Transfer a checkpointed running turn to this daemon boot."""

        adopted = self.store.adopt_execution(
            execution.execution_id, boot_id=self.boot_id
        )
        self.store.record_task_event(
            task_id=adopted.task_id,
            claim_id=adopted.claim_id,
            event_type="execution.resuming",
            payload={"execution_id": adopted.execution_id, "driver": adopted.driver},
        )
        return RecoveryOutcome(
            task_id=adopted.task_id,
            task_attempt=adopted.task_attempt,
            resolution="resuming",
            detail="a durable coding-agent execution will resume in this daemon boot",
            execution_id=adopted.execution_id,
        )

    def _in_flight_here(self, execution: ExecutionRecord | None) -> bool:
        """Report whether a worker in this process may still be advancing it.

        A settled execution row proves no worker is running whichever boot
        wrote it, so only a working state in this process is protected.
        """

        if execution is None:
            return False
        return (
            execution.boot_id == self.boot_id
            and execution.state in _IN_FLIGHT_EXECUTION_STATES
        )

    def _recover_execution(self, execution: ExecutionRecord) -> RecoveryOutcome:
        intent = self.store.latest_publication_intent(
            execution.task_id, execution.task_attempt
        )
        if intent is None:
            return self._recover_unpublished(execution)
        if intent.operation_state in _OPEN_INTENT_STATES:
            return self._resolve_intent(execution, intent)
        if intent.operation_state == "confirmed":
            return self._settle_confirmed(execution, intent)
        return self._settle_recorded_outcome(execution, intent)

    def _recover_unpublished(self, execution: ExecutionRecord) -> RecoveryOutcome:
        """Close an attempt that died before any shared Git side effect.

        Publication is the only writer of shared references and it never runs
        before its intent is durable, so the absence of an intent is itself the
        proof that the repository was never touched.  The reservation can
        therefore be released and its waiters woken.
        """

        claim = self.store.get_claim(execution.claim_id)
        if claim is not None and claim.state in _RESERVED_CLAIM_STATES:
            # A reservation with no intent behind it cannot be explained, so it
            # is never released: the record that would say what was published
            # is exactly the one that is missing.
            return self._flag_execution_for_operator(
                execution,
                failure_code=RESERVATION_WITHOUT_INTENT,
                detail=(
                    f"claim {claim.claim_id} holds a {claim.state.value} "
                    "reservation but no publication intent explains it"
                ),
            )
        repository = self._repository_for(claim)
        removed, cleanup_detail = self._remove_worktree(repository, execution)
        self._update_execution(
            execution, state="failed", failure_code=DAEMON_INTERRUPTED
        )
        released = self._release_claim(claim)
        detail = "no publication was attempted; the reservation was released"
        if not released:
            detail = "no publication was attempted; the claim was already terminal"
        return RecoveryOutcome(
            task_id=execution.task_id,
            task_attempt=execution.task_attempt,
            resolution="released",
            detail=_join(detail, cleanup_detail),
            execution_id=execution.execution_id,
            worktree_removed=removed,
        )

    def _resolve_intent(
        self,
        execution: ExecutionRecord | None,
        intent: PublicationIntentRecord,
    ) -> RecoveryOutcome:
        """Decide an unresolved intent by reading the reference it targeted."""

        repository = self.store.get_repository_by_key(intent.repo_key)
        if repository is None:
            return self._flag_intent_for_operator(
                execution,
                intent,
                detail=(
                    "the repository this publication targeted is no longer registered"
                ),
                failure_code=REPOSITORY_UNREADABLE,
            )
        repo_path = Path(repository.main_worktree_path)
        try:
            actual = read_task_ref(repo_path, intent.task_id)
        except LlmCoordError as exc:
            return self._flag_intent_for_operator(
                execution,
                intent,
                detail=f"the internal task reference could not be read: {exc.message}",
                failure_code=REPOSITORY_UNREADABLE,
            )

        if intent.result_commit_id is not None and actual == intent.result_commit_id:
            self.coordinator.confirm_publication(
                intent.intent_id,
                result_commit_id=intent.result_commit_id,
                task_ref=intent.task_ref or f"refs/llm-coord/tasks/{intent.task_id}",
            )
            fresh = self.store.get_publication_intent(intent.intent_id) or intent
            return self._settle_confirmed(
                execution,
                fresh,
                detail=(
                    "the reference swap had already succeeded before the daemon stopped"
                ),
            )

        if actual == intent.expected_old_ref:
            # The reference still names exactly what the swap meant to replace,
            # so the swap did not happen. That is proof of absence, not merely
            # an absence of proof, which is what releasing safely requires.
            removed, cleanup_detail = self._remove_worktree(repository, execution)
            self.coordinator.fail_publication_safe(
                intent.intent_id,
                reason="daemon stopped before the reference swap",
                failure_code=DAEMON_INTERRUPTED,
            )
            self._update_execution(
                execution, state="failed", failure_code=DAEMON_INTERRUPTED
            )
            return RecoveryOutcome(
                task_id=intent.task_id,
                task_attempt=intent.task_attempt,
                resolution="failed_safe",
                detail=_join(
                    "the reference never moved, so the reservation was released",
                    cleanup_detail,
                ),
                execution_id=execution.execution_id if execution else None,
                intent_id=intent.intent_id,
                worktree_removed=removed,
            )

        return self._flag_intent_for_operator(
            execution,
            intent,
            detail=(
                "the internal task reference matches neither the published "
                "result nor the commit the publication expected to replace"
            ),
            failure_code=PUBLICATION_AMBIGUOUS,
            details={
                "actual_oid": actual,
                "expected_old_ref": intent.expected_old_ref,
                "result_commit_id": intent.result_commit_id,
            },
        )

    def _settle_confirmed(
        self,
        execution: ExecutionRecord | None,
        intent: PublicationIntentRecord,
        *,
        detail: str = "the publication was already confirmed",
    ) -> RecoveryOutcome:
        """Bring local state in line with a publication that did succeed."""

        repository = self.store.get_repository_by_key(intent.repo_key)
        removed, cleanup_detail = self._remove_worktree(repository, execution)
        if execution is not None:
            self._update_execution(
                execution,
                state="published" if removed else "cleanup_failed",
                result_commit_id=intent.result_commit_id,
            )
        return RecoveryOutcome(
            task_id=intent.task_id,
            task_attempt=intent.task_attempt,
            resolution="confirmed",
            detail=_join(detail, cleanup_detail),
            execution_id=execution.execution_id if execution else None,
            intent_id=intent.intent_id,
            worktree_removed=removed,
        )

    def _settle_recorded_outcome(
        self, execution: ExecutionRecord, intent: PublicationIntentRecord
    ) -> RecoveryOutcome:
        """Tidy local state behind an outcome coordination already recorded."""

        repository = self.store.get_repository_by_key(intent.repo_key)
        removed, cleanup_detail = self._remove_worktree(repository, execution)
        self._update_execution(
            execution, state="failed", failure_code=DAEMON_INTERRUPTED
        )
        return RecoveryOutcome(
            task_id=execution.task_id,
            task_attempt=execution.task_attempt,
            resolution="settled",
            detail=_join(
                "the publication was already recorded as safely failed",
                cleanup_detail,
            ),
            execution_id=execution.execution_id,
            intent_id=intent.intent_id,
            worktree_removed=removed,
        )

    def _flag_intent_for_operator(
        self,
        execution: ExecutionRecord | None,
        intent: PublicationIntentRecord,
        *,
        detail: str,
        failure_code: str,
        details: dict[str, object] | None = None,
    ) -> RecoveryOutcome:
        self.coordinator.flag_publication_for_operator(
            intent.intent_id, reason=detail, details=details
        )
        if execution is not None:
            self._update_execution(
                execution, state="operator_attention", failure_code=failure_code
            )
        # The managed worktree is deliberately left in place: it is the only
        # remaining evidence of what the abandoned attempt produced.
        return RecoveryOutcome(
            task_id=intent.task_id,
            task_attempt=intent.task_attempt,
            resolution="operator_attention",
            detail=detail,
            execution_id=execution.execution_id if execution else None,
            intent_id=intent.intent_id,
        )

    def _flag_execution_for_operator(
        self,
        execution: ExecutionRecord,
        *,
        failure_code: str,
        detail: str,
        intent_id: str | None = None,
    ) -> RecoveryOutcome:
        self._update_execution(
            execution, state="operator_attention", failure_code=failure_code
        )
        return RecoveryOutcome(
            task_id=execution.task_id,
            task_attempt=execution.task_attempt,
            resolution="operator_attention",
            detail=detail,
            execution_id=execution.execution_id,
            intent_id=intent_id,
        )

    def _update_execution(
        self,
        execution: ExecutionRecord | None,
        *,
        state: str,
        failure_code: str | None = None,
        result_commit_id: str | None = None,
    ) -> None:
        if execution is None or execution.state == state:
            return
        # Recovery must record every decision it can, but a telemetry write
        # that fails must never leave the coordination outcome unmade.
        with suppress(KeyError, ValueError):
            self.store.update_execution(
                execution.execution_id,
                state=state,
                failure_code=failure_code,
                result_commit_id=result_commit_id,
            )

    def _release_claim(self, claim: ClaimRecord | None) -> bool:
        if claim is None or claim.state not in {
            ClaimState.QUEUED,
            ClaimState.ACTIVE_WORK,
        }:
            return False
        with suppress(CoordinationError):
            result = self.coordinator.release_claim(
                claim.claim_id,
                reason="daemon_stopped_mid_execution",
                task_state="failed",
                failure_code=DAEMON_INTERRUPTED,
            )
            return result.released
        return False

    def _repository_for(self, claim: ClaimRecord | None) -> RepositoryRecord | None:
        if claim is None:
            return None
        return self.store.get_repository_by_key(claim.repo_key)

    def _remove_worktree(
        self,
        repository: RepositoryRecord | None,
        execution: ExecutionRecord | None,
    ) -> tuple[bool, str]:
        """Remove one abandoned managed worktree, reporting what stopped it."""

        if execution is None:
            return False, ""
        if repository is None:
            return False, "the managed worktree's repository is no longer registered"
        repo_path = Path(repository.main_worktree_path)
        registered = Path(execution.worktree_path)
        try:
            if registered.exists():
                remove_managed_worktree(
                    repo_path,
                    managed_root=self.managed_worktree_root,
                    registered_path=registered,
                    force=True,
                )
                return True, ""
            # The directory is gone but Git may still own the record, and a
            # retained record makes every later attempt of this task fail to
            # create its worktree.
            prune_managed_worktrees(repo_path, managed_root=self.managed_worktree_root)
            return True, ""
        except LlmCoordError as exc:
            return False, f"the managed worktree remains: {exc.message}"
        except OSError as exc:
            return False, f"the managed worktree remains: {exc.strerror or exc}"


def _join(primary: str, secondary: str) -> str:
    return f"{primary}; {secondary}" if secondary else primary


def recovery_error(report: RecoveryReport) -> LlmCoordError | None:
    """Return the error a caller should surface when recovery left work blocked."""

    blocking = report.blocking
    if not blocking:
        return None
    return LlmCoordError(
        ErrorCode.PROVIDER_AMBIGUOUS,
        f"{len(blocking)} abandoned publication(s) need an operator decision",
        details={
            "task_ids": sorted({outcome.task_id for outcome in blocking}),
        },
    )


__all__ = [
    "DAEMON_INTERRUPTED",
    "PUBLICATION_AMBIGUOUS",
    "ExecutionReconciler",
    "RecoveryOutcome",
    "RecoveryReport",
    "recovery_error",
]
