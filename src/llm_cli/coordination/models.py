"""Immutable records used by repository coordination."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

EFFORT_LEVELS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)


class ClaimState(StrEnum):
    QUEUED = "queued"
    ACTIVE_WORK = "active_work"
    PUBLISHING = "publishing"
    ACTIVE_INTEGRATION = "active_integration"
    RELEASED = "released"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


ACTIVE_STATES = frozenset(
    {
        ClaimState.ACTIVE_WORK,
        ClaimState.PUBLISHING,
        ClaimState.ACTIVE_INTEGRATION,
    }
)
CONTENDING_STATES = frozenset({ClaimState.QUEUED, *ACTIVE_STATES})
TERMINAL_STATES = frozenset(
    {ClaimState.RELEASED, ClaimState.EXPIRED, ClaimState.CANCELLED}
)


@dataclass(frozen=True, slots=True)
class RepositoryRecord:
    repository_id: str
    repo_key: str
    profile_id: str
    display_name: str
    git_common_dir: str
    main_worktree_path: str
    remote_identity: str | None
    target_ref: str
    object_format: str
    integration_adapter: str
    coordination_mode: str
    path_case_insensitive: bool
    coordinate_by_remote: bool
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class CheckoutRecord:
    """One physical working-tree attachment to a legacy repository record."""

    checkout_id: str
    repository_id: str
    repo_key: str
    canonical_path: str
    git_common_dir: str
    path_case_insensitive: bool
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    """Where one session's edits are physically prepared."""

    workspace_id: str
    checkout_id: str
    kind: str
    mode: str
    state: str
    state_version: int
    canonical_path: str
    git_dir: str | None
    workspace_epoch: int
    workspace_revision: int
    local_generation: int
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class WorkspaceCandidateRecord:
    """A private, complete proposed regular-file replacement."""

    candidate_id: str
    workspace_id: str
    session_id: str
    relative_path: str
    generation: int
    base_kind: str
    base_mode: str
    base_digest: str
    result_kind: str
    result_mode: str
    result_digest: str
    content_hash: str
    byte_count: int
    state: str
    created_at: int
    updated_at: int
    published_at: int | None
    diverged_at: int | None


@dataclass(frozen=True, slots=True)
class WorkspacePublicationRecord:
    """A durable single-file filesystem/SQLite publication journal."""

    publication_id: str
    candidate_id: str
    workspace_id: str
    checkout_id: str
    operation_state: str
    workspace_revision: int | None
    checkout_event_sequence: int | None
    created_at: int
    updated_at: int
    confirmed_at: int | None


@dataclass(frozen=True, slots=True)
class WorkspaceDivergenceRecord:
    """Exact evidence retained when a candidate's base no longer matches."""

    divergence_id: str
    candidate_id: str
    workspace_id: str
    session_id: str
    relative_path: str
    base_kind: str
    base_mode: str
    base_digest: str
    current_kind: str
    current_mode: str
    current_digest: str
    candidate_kind: str
    candidate_mode: str
    candidate_digest: str
    state: str
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """Durable top-level coding-agent session, without its resume secret."""

    session_id: str
    checkout_id: str
    workspace_id: str | None
    workspace_mode: str
    provider: str
    model: str
    state: str
    state_version: int
    conversation_revision: int
    opened_at: int
    last_heartbeat_at: int
    disconnected_at: int | None
    closed_at: int | None
    close_reason: str | None
    updated_at: int
    effort: str | None = None
    agent_mode: str = "auto"


@dataclass(frozen=True, slots=True)
class SessionCursorRecord:
    """Replay and context positions for one session's checkout stream."""

    session_id: str
    checkout_id: str
    transport_received_sequence: int
    context_consumed_sequence: int
    last_delivered_sequence: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class CheckoutEventRecord:
    """One durable checkout-scoped notification replayed to local sessions."""

    checkout_id: str
    sequence: int
    event_id: str
    event_type: str
    caused_by_session_id: str | None
    payload: dict[str, object]
    created_at: int


@dataclass(frozen=True, slots=True)
class SessionIntentRecord:
    """A current or cleared advisory path declaration for one session."""

    intent_id: str
    session_id: str
    generation: int
    summary: str
    state: str
    paths: tuple[str, ...]
    created_at: int
    cleared_at: int | None
    updated_at: int


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    repository_id: str
    repo_key: str
    title: str
    state: str
    coordination_mode: str
    coordination_state: str
    attempt: int
    current_claim_id: str | None
    current_fencing_token: int | None
    session_id: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    claim_id: str
    task_id: str
    task_attempt: int
    repo_key: str
    state: ClaimState
    queue_sequence: int
    fencing_token: int | None
    lease_expires_at: int | None
    scopes: tuple[str, ...]
    blocking_claim_ids: tuple[str, ...]
    created_at: int
    updated_at: int
    release_reason: str | None = None
    scheduling_mode: str = "exclusive"
    workspace_id: str | None = None

    @property
    def granted(self) -> bool:
        return self.state is ClaimState.ACTIVE_WORK

    @property
    def live(self) -> bool:
        return self.state in CONTENDING_STATES


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    released: bool
    claim: ClaimRecord
    activated: tuple[ClaimRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    expired_claim_ids: tuple[str, ...]
    expired_task_ids: tuple[str, ...]
    activated: tuple[ClaimRecord, ...]


@dataclass(frozen=True, slots=True)
class PublicationIntentRecord:
    intent_id: str
    idempotency_key: str
    claim_id: str
    task_id: str
    task_attempt: int
    repo_key: str
    fencing_token: int
    patch_hash: str
    result_tree_id: str
    result_commit_id: str | None
    expected_old_ref: str | None
    target_ref: str
    task_ref: str | None
    operation_state: str
    changed_paths: tuple[str, ...]
    started_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    """Durable local lifecycle record for one task attempt."""

    execution_id: str
    task_id: str
    task_attempt: int
    claim_id: str
    driver: str
    state: str
    boot_id: str | None
    worktree_path: str
    base_oid: str
    result_tree_id: str | None
    result_commit_id: str | None
    patch_hash: str | None
    summary: str | None
    tool_calls: int
    usage: dict[str, int]
    failure_code: str | None
    created_at: int
    updated_at: int
    workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionLaunchRecord:
    """Durable recipe the daemon needs to start one claimed task attempt."""

    task_id: str
    task_attempt: int
    driver: str
    instructions: str
    interactive: bool
    parameters: dict[str, object]
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ExecutionCheckpointRecord:
    """Opaque, JSON-safe runtime state owned by one resumable driver."""

    execution_id: str
    driver: str
    checkpoint: dict[str, object]
    revision: int
    created_at: int
    updated_at: int
    terminal_event_persisted: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionAnswerRecord:
    """One immutable accepted answer and its durable transcript projection."""

    execution_id: str
    answer_id: str
    version: int
    payload: dict[str, object]
    content_sha256: str
    first_event_sequence: int
    last_event_sequence: int
    created_at: int


@dataclass(frozen=True, slots=True)
class TaskEventRecord:
    """Durable task-state change available to every local CLI session."""

    sequence: int
    event_id: str
    task_id: str
    claim_id: str | None
    event_type: str
    payload: dict[str, object]
    created_at: int


class CoordinationError(RuntimeError):
    """Base class for safe coordination failures."""


class ClaimNotFound(CoordinationError):
    pass


class ClaimConflict(CoordinationError):
    pass


class ClaimAuthorityError(CoordinationError):
    pass


class ScopeAuthorityError(ClaimAuthorityError):
    pass


class PublicationError(CoordinationError):
    pass
