"""The provider-independent seam between coordination and whatever writes code.

A driver decides *what* to change; the execution runner around it owns the
claim, the worktree, validation, publication, and recovery.  Nothing in this
module may import a provider SDK, and nothing below it may reach the control
database, a fencing token, or a publication credential -- a driver's only
authority over the repository is the tool broker it is handed.

The protocol is synchronous because a driver runs on the worker thread that
owns its execution record.  That keeps lease renewal, crash recovery, and boot
ownership working exactly as they already do; a driver that needs concurrency
owns it internally rather than forcing the lifecycle to become async.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from llm_cli.agent.finalization import SettlementFacts
from llm_cli.agent.limits import MAX_ANSWER_CHARACTERS
from llm_cli.agent.modes import validate_agent_mode
from llm_cli.agent.tools import ToolBroker


@dataclass(frozen=True, slots=True)
class DriverCapabilities:
    """What a driver can do, so a policy can refuse one that cannot comply.

    ``enforces_scope`` is the load-bearing field: a built-in driver writes only
    through the tool broker and is bounded by the claim, while an
    external-process driver runs as the same user and is merely cooperative.
    """

    tool_calling: bool = False
    streaming: bool = False
    resumable: bool = False
    enforces_scope: bool = True
    transmits_repository_contents: bool = False
    reports_usage: bool = False
    shared_workspace: bool = False


@dataclass(frozen=True, slots=True)
class CoordinationUpdate:
    """Read-only checkout facts through one durable event sequence.

    The harness checkpoints this cursor separately from terminal delivery. The
    callback must refuse an oversized update rather than skip relevant facts.
    """

    sequence: int
    text: str | None


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Everything a driver is told about the work it has been given."""

    task_id: str
    attempt: int
    instructions: str
    scopes: tuple[str, ...]
    worktree: Path
    base_oid: str
    conversation_state: Mapping[str, object] | None = None
    coordination_context: str | None = None
    resume_state: Mapping[str, object] | None = None
    checkpoint: Callable[[Mapping[str, object]], bool | None] | None = None
    workspace_mode: str = "isolated"
    refresh_coordination: Callable[[int | None], CoordinationUpdate] | None = None
    agent_mode: str = "auto"
    publication_aware_finalization: bool = False

    def __post_init__(self) -> None:
        validate_agent_mode(self.agent_mode)
        if type(self.publication_aware_finalization) is not bool:
            raise ValueError("publication-aware finalization flag must be boolean")


@dataclass(frozen=True, slots=True)
class RunResult:
    """Model deliverable and intent, before trusted validation/publication.

    ``summary`` is operational metadata, never a substitute for ``answer``.
    A completed model turn does not assert that its edits were published.
    """

    summary: str
    tool_calls: int = 0
    usage: Mapping[str, int] = field(default_factory=dict)
    answer: str = ""
    outcome: str = "completed"

    def __post_init__(self) -> None:
        if self.outcome not in {"completed", "blocked", "partial"}:
            raise ValueError("driver outcome must be completed, blocked, or partial")
        if not isinstance(self.answer, str) or len(self.answer) > MAX_ANSWER_CHARACTERS:
            raise ValueError("driver answer is invalid or exceeds its character limit")
        if self.outcome == "completed" and not self.answer.strip():
            raise ValueError(
                "a completed driver result requires the user-facing answer"
            )


@dataclass(frozen=True, slots=True)
class FinalizationRequest:
    """Ask a driver to accept or regenerate an answer after trusted settlement."""

    task_id: str
    attempt: int
    instructions: str
    draft: str
    summary: str
    facts: SettlementFacts
    resume_state: Mapping[str, object]
    checkpoint: Callable[[Mapping[str, object]], bool | None]
    use_model: bool = True
    on_event: Callable[[str, dict[str, object]], None] | None = None
    cancelled: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        if not self.task_id or type(self.attempt) is not int or self.attempt <= 0:
            raise ValueError("finalization task identity is invalid")
        if not self.instructions.strip():
            raise ValueError("finalization requires the original request")
        if not isinstance(self.draft, str) or len(self.draft) > MAX_ANSWER_CHARACTERS:
            raise ValueError("finalization draft is invalid or too large")
        if not isinstance(self.resume_state, Mapping):
            raise ValueError("finalization requires a durable driver checkpoint")

    def emit(self, event_type: str, payload: dict[str, object]) -> None:
        if self.on_event is not None:
            self.on_event(event_type, payload)

    def check_cancelled(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise RuntimeError("final response cancelled after task settlement")


@runtime_checkable
class SettlementFinalizer(Protocol):
    """Optional driver capability for one post-settlement response turn."""

    def prepare_finalization(
        self, request: FinalizationRequest
    ) -> Mapping[str, object]:
        """Bind trusted facts before the runner releases file authority."""

    def finalize(self, request: FinalizationRequest) -> RunResult:
        """Persist and return the accepted answer without rerunning task work."""


@runtime_checkable
class AgentDriver(Protocol):
    """Produce changes inside one claimed worktree through bounded tools.

    The request may carry opaque prior runtime state plus a checkpoint callback.
    Drivers that declare ``resumable`` use them to continue a turn; the
    lifecycle persists that state but never interprets harness or provider
    content.
    """

    @property
    def name(self) -> str:
        """Stable identifier persisted on the execution record."""

    @property
    def capabilities(self) -> DriverCapabilities:
        """Declared behavior, checked against task policy before scheduling."""

    def run(self, request: RunRequest, tools: ToolBroker) -> RunResult:
        """Do the work, returning only after the worktree is final."""


__all__ = [
    "AgentDriver",
    "CoordinationUpdate",
    "DriverCapabilities",
    "FinalizationRequest",
    "RunRequest",
    "RunResult",
    "SettlementFinalizer",
]
