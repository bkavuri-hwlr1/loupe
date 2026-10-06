"""The provider-independent shape of a tool-calling chat model.

Nothing above this module may name a vendor, a model ID, or an SDK type.  A
session owns its own conversation history because that history is expressed in
provider-native blocks -- thinking blocks, cache markers, tool-use blocks -- and
copying them through a neutral representation would either lose them or leak
the vendor's schema into the agent loop.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from llm_cli.errors import ErrorCode, LlmCoordError

# The fixed classification every adapter uses when a prompt exceeds the model's
# context window, so the harness can summarize history and retry. Adapters must
# classify only rejections they can identify precisely: a false match replaces
# history with a lossy summary and still fails.
CONTEXT_OVERFLOW = "context_overflow"


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """One tool the model asked to run."""

    call_id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """One tool outcome being returned to the model."""

    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ModelTurn:
    """What the model produced in one turn."""

    text: str
    tool_calls: tuple[ToolCallRequest, ...] = ()
    stop_reason: str = "end_turn"
    # Token counts. Adapters report "prompt_tokens" (every prompt token, cached
    # or not), "output_tokens", and when known "cache_read_input_tokens" and
    # "reasoning_tokens" (subsets of those). Other keys are provider-specific.
    usage: Mapping[str, int] = field(default_factory=dict)
    refusal_category: str | None = None
    # Total tokens this request occupied in the model's context window: the
    # whole prompt, cached or not, plus the generated output. None when the
    # provider did not report usage.
    context_tokens: int | None = None
    # Displayable provider summaries only. Opaque/native reasoning is retained
    # exclusively in the adapter's conversation history.
    reasoning: str = ""

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"

    @property
    def answer_complete(self) -> bool:
        """A natural text completion, never truncated or pending tool execution."""

        return (
            self.stop_reason == "end_turn"
            and not self.tool_calls
            and bool(self.text.strip())
        )


class ModelSession(Protocol):
    """One stateful conversation with a tool-calling model."""

    def snapshot(self) -> Mapping[str, object]:
        """Return JSON-safe provider-native history for durable replay."""

    def send_user(self, text: str) -> ModelTurn:
        """Append a user message and return the model's next turn."""

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        """Return every tool result from one turn together, and continue.

        Results are submitted as a batch because a model that made parallel
        tool calls expects them answered in a single turn; splitting them
        teaches it to stop making parallel calls.
        """

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        """Append outcomes to native history without making a model request.

        The harness uses this for a terminal ``finish_task`` batch. Every native
        tool call must have a matching result even when no subsequent model turn
        is needed.
        """


StreamCallback = Callable[[str, dict[str, object]], None]


@runtime_checkable
class StreamingSession(Protocol):
    """Optional live-output hook; synchronous third-party sessions still work."""

    def set_event_callback(self, callback: StreamCallback | None) -> None:
        """Observe public text/summary and argument deltas before completion.

        Deltas are for display only. Tools become executable exclusively when
        send_user/send_tool_results returns a validated, completed ModelTurn.
        """


@runtime_checkable
class ToolResultRecorder(Protocol):
    """Runtime check for the required terminal-result session capability.

    The harness stops after finish_task instead of asking the model again.
    The actual terminal results must still be recorded before the conversation
    snapshot is persisted.
    """

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        """Append outcomes to native history without making a model request."""


@runtime_checkable
class CompactableSession(Protocol):
    """Optional capability to replace long native history with a summary."""

    def summarize(
        self,
        instruction: str,
        *,
        max_tool_text: int | None = None,
        pending_results: Sequence[ToolCallResult] = (),
    ) -> ModelTurn:
        """Request a tools-disabled summary of the current history.

        The instruction is sent after the existing history, but neither it nor
        the reply is appended. Live event callbacks are not invoked.
        ``pending_results`` answer the last turn's tool calls in this request
        only, so a summary can cover them without recording them first. With
        ``max_tool_text``, longer tool arguments and results are shortened in
        this request only, so an oversized history can still be summarized.
        """

    def replace_history(self, summary: str) -> None:
        """Replace all native history with a single user summary message."""


def context_overflow_error(status_code: int | None = None) -> LlmCoordError:
    details: dict[str, object] = {"provider_error": CONTEXT_OVERFLOW}
    if status_code is not None:
        details["status_code"] = status_code
    return LlmCoordError(
        ErrorCode.PROVIDER_UNAVAILABLE,
        "the conversation is too long for the model's context window",
        details=details,
    )


def is_context_overflow(error: BaseException) -> bool:
    return (
        isinstance(error, LlmCoordError)
        and error.details is not None
        and error.details.get("provider_error") == CONTEXT_OVERFLOW
    )


def mentions_any(text: object, phrases: Sequence[str]) -> bool:
    """Match lowercase provider phrases near the start of an error message."""

    return isinstance(text, str) and any(
        phrase in text[:2000].lower() for phrase in phrases
    )


def shorten_tool_text(text: str, limit: int) -> str:
    """Keep the start of a long tool payload for a summary-only request."""

    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} characters omitted ...]"


# Reasoning effort levels from least to most thinking.
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def capped_effort(
    current: str | None, ceiling: str, supported: Sequence[str]
) -> str | None:
    """Return the supported effort at most ``ceiling`` if it lowers ``current``.

    ``current`` is the effort a session would otherwise use, or None when the
    model's default is unknown. Without a supported level at or below the
    ceiling, the lowest supported level is used. None means keep ``current``.
    """

    if not supported:
        return None
    ordered = sorted(supported, key=EFFORT_ORDER.index)
    rank = EFFORT_ORDER.index(ceiling)
    allowed = [effort for effort in ordered if EFFORT_ORDER.index(effort) <= rank]
    chosen = allowed[-1] if allowed else ordered[0]
    if current is not None and EFFORT_ORDER.index(chosen) >= EFFORT_ORDER.index(
        current
    ):
        return None
    return chosen


class ChatProvider(Protocol):
    """Create model sessions bound to one system prompt and tool surface."""

    @property
    def name(self) -> str:
        """Stable adapter identity persisted with durable harness state."""

    @property
    def model(self) -> str:
        """The model identity recorded on the execution for audit."""

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> ModelSession:
        """Open or restore a conversation that can call exactly ``tools``."""


@runtime_checkable
class EffortCappedProvider(Protocol):
    """Optional capability to open sessions that think less than the provider."""

    def capped_effort(self, ceiling: str) -> str | None:
        """The effort a session capped at ``ceiling`` uses, or None if unchanged."""

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
        effort: str | None = None,
    ) -> ModelSession:
        """Like ``ChatProvider.session``; ``effort`` replaces the provider's own."""


__all__ = [
    "CONTEXT_OVERFLOW",
    "EFFORT_ORDER",
    "ChatProvider",
    "CompactableSession",
    "EffortCappedProvider",
    "ModelSession",
    "ModelTurn",
    "StreamCallback",
    "StreamingSession",
    "ToolCallRequest",
    "ToolCallResult",
    "ToolResultRecorder",
    "capped_effort",
    "context_overflow_error",
    "is_context_overflow",
    "mentions_any",
    "shorten_tool_text",
]
