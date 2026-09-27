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
    usage: Mapping[str, int] = field(default_factory=dict)
    refusal_category: str | None = None
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


__all__ = [
    "ChatProvider",
    "ModelSession",
    "ModelTurn",
    "StreamCallback",
    "StreamingSession",
    "ToolCallRequest",
    "ToolCallResult",
    "ToolResultRecorder",
]
