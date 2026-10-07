"""Human rendering of the durable event stream.

The conversation shows assistant messages and concise activity, while raw
reasoning and tool payloads remain in durable events for explicit inspection.
Assistant answers are preserved; terminal controls are stripped before display.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any, TextIO

from llm_cli.cli.partial_json import AnswerFieldDecoder
from llm_cli.cli.streaming_markdown import MarkdownStream
from llm_cli.cli.terminal import TerminalUI, TextSanitizer, safe_text

_MAX_DETAIL = 200
# Explorations are named by the model's short label, or failing that a clipped task.
_MAX_EXPLORATION_NAME = 60

_TOOL_ACTIVITIES = {
    "list_files": "Reviewing project files…",
    "read_file": "Reviewing project files…",
    "search_text": "Searching the codebase…",
    "write_file": "Making changes…",
    "apply_patch": "Making changes…",
    "create_directory": "Making changes…",
    "delete_file": "Making changes…",
    "rename_file": "Making changes…",
    "read_diff": "Reviewing changes…",
    "validate_changes": "Checking changes…",
    "run_check": "Running checks…",
    "finish_task": "Preparing the result…",
    "ask_user": "Waiting for your input…",
    "update_plan": "Updating the plan…",
    "explore": "Exploring…",
}
_PLAN_MARKS = {"completed": "✓", "in_progress": "▸", "pending": "○"}
# A task that called any of these may still fail checks or publication, so its
# answer draft is only previewed until the model.finished event accepts it.
_WRITE_TOOLS = frozenset(
    tool for tool, activity in _TOOL_ACTIVITIES.items() if activity == "Making changes…"
)

# These records remain in the durable/JSON stream. In the conversation they
# are bookkeeping within a task, not separate accomplishments for the user.
_QUIET_EVENTS = frozenset(
    {
        "task.created",
        "claim.granted",
        "claim.released",
        "claim.cancelled",
        "execution.scheduled",
        "execution.worktree_ready",
        "execution.running",
        "driver.finished",
        "publication.prepared",
        "publication.confirmed",
        "model.coordination_updated",
        "check.output",
        "command.output",
    }
)

_CONVERSATION_PHASES = {
    "claim.queued": "Waiting for other work to finish…",
    "execution.preparing": "Getting ready…",
    "execution.schedule_observed": "Still working on this…",
    "execution.validated": "Finalizing changes…",
    "execution.publishing": "Wrapping things up…",
    "execution.cleanup_failed": (
        "Your changes are ready; temporary files still need cleanup."
    ),
}

# Human output is an explicit projection, not a dump of every durable event.
# New internal event types must opt in before they can affect the conversation.
_PUBLIC_EVENTS = frozenset(
    {
        *_CONVERSATION_PHASES,
        "execution.resuming",
        "execution.published",
        "execution.failed",
        "execution.operator_attention",
        "execution.interrupted",
        "execution.abandoned",
        "execution.background_failed",
        "publication.failed_safe",
        "publication.operator_attention",
        "integration.discarded",
        "model.started",
        "model.resumed",
        "model.failed",
        "model.refused",
        "model.stalled",
        "model.instructions.loaded",
        "model.context.compacted",
        "model.context.compaction_failed",
        "model.retrying",
        "model.tool_interrupted",
        "mcp.server.started",
        "mcp.server.failed",
        "model.turn.started",
        "model.turn.completed",
        "model.reasoning",
        "model.reasoning.delta",
        "model.text.delta",
        "model.answer.preview",
        "model.said",
        "model.partial",
        "model.finished",
        "model.tool.delta",
        "model.tool_call",
        "model.tool_result",
        "tool.called",
        "tool.result",
        "tool.completed",
        "check.started",
        "check.finished",
        "command.started",
        "command.finished",
        "plan.updated",
        "explore.started",
        "explore.finished",
        "question.asked",
        "workflow.awaiting_review",
        "workflow.cancelled",
        "workflow.stopping",
        "workflow.verification",
    }
)

_LABELS: dict[str, str] = {
    "task.created": "task created",
    "claim.granted": "claim granted",
    "claim.queued": "queued behind overlapping work",
    "claim.released": "claim released",
    "claim.cancelled": "claim cancelled",
    "execution.scheduled": "scheduled",
    "execution.schedule_observed": "already running",
    "execution.preparing": "preparing",
    "execution.worktree_ready": "worktree ready",
    "execution.running": "running",
    "execution.resuming": "resuming after daemon restart",
    "execution.validated": "changes validated",
    "execution.publishing": "publishing",
    "execution.published": "published",
    "execution.failed": "failed",
    "execution.cleanup_failed": "published, but the worktree remains",
    "execution.operator_attention": "needs an operator decision",
    "execution.interrupted": "interrupted",
    "execution.abandoned": "abandoned at shutdown",
    "execution.background_failed": "run failed",
    "publication.prepared": "publication reserved",
    "publication.confirmed": "publication confirmed",
    "publication.failed_safe": "publication safely abandoned",
    "publication.operator_attention": "publication outcome unknown",
    "integration.discarded": "result discarded",
    "driver.finished": "driver finished",
    "model.started": "thinking",
    "model.failed": "model request failed",
    "model.resumed": "resuming the model",
    "model.coordination_updated": "refreshed checkout context",
    "model.tool_interrupted": "tool outcome was interrupted; the model will inspect",
    "model.stalled": "the model stopped without finishing",
    "model.instructions.loaded": "using repository instructions",
    "model.context.compacted": "summarized earlier conversation to stay in context",
    "model.context.compaction_failed": "could not summarize earlier conversation",
    "model.retrying": "retrying the model request",
    "model.refused": "the model declined this task",
    "model.finished": "done",
    "mcp.server.started": "using MCP server",
    "mcp.server.failed": "an MCP server is unavailable",
}

_MARKERS: dict[str, str] = {
    "tool.called": "→",
    "execution.failed": "✗",
    "execution.background_failed": "✗",
    "model.refused": "✗",
    "execution.published": "✓",
    "publication.confirmed": "✓",
    "model.finished": "✓",
    "execution.operator_attention": "!",
    "publication.operator_attention": "!",
    "model.stalled": "!",
    "model.context.compaction_failed": "!",
    "mcp.server.failed": "!",
    "model.retrying": "!",
}

_STREAMED_OUTCOMES = {
    "blocked": "Loupe marked this answer as blocked.",
    "partial": "Loupe marked this answer as incomplete.",
}

_PROVIDER_FAILURE_EVENTS = frozenset(
    {"execution.failed", "execution.background_failed"}
)

_QUIET_SETTLEMENT_FAILURES = frozenset({"REVIEW_REQUIRED", "CANCELLED"})
_SETTLEMENT_FAILURE_FALLBACKS = {
    "REVIEW_REQUIRED": (
        "Changes were retained for review. Use /diff and /checks before /apply."
    ),
    "CANCELLED": "Stopped. Use /diff to inspect any retained edits.",
}
_INCOMPLETE_FAILURES = {
    "MODEL_BLOCKED": "The task is blocked. No changes were applied.",
    "RESPONSE_INCOMPLETE": (
        "The response ended before the task was complete. No changes were applied."
    ),
}

_PROVIDER_ERROR_HINTS: dict[str, tuple[str, str]] = {
    "connection": (
        "The connection to your provider failed.",
        "Check your connection and try again. Use /diff to inspect retained edits.",
    ),
    "timeout": (
        "Your provider timed out while responding.",
        "Try again. Use /diff to inspect any retained edits before continuing.",
    ),
    "incomplete_response": (
        "Your provider's response ended before it completed.",
        "Try again. Use /diff to inspect any retained edits before continuing.",
    ),
    "invalid_response": (
        "Your provider returned a response Loupe could not use.",
        "Try again, or choose another model with /model.",
    ),
    "unsupported_model": (
        "This model is unavailable for your account.",
        "Run /model --refresh to choose an available model, then try again.",
    ),
    "unsupported_effort": (
        "This model does not support the selected effort level.",
        "Run /effort to choose a supported level, then try again.",
    ),
    "unsupported_parameter": (
        "The provider rejected Loupe's request settings.",
        "Try /model or /effort. If this persists, Loupe may need an update.",
    ),
    "authentication": (
        "Your provider could not authenticate this request.",
        "Run /login to reconnect your account, then try again.",
    ),
    "rate_limit": (
        "Your provider's usage limit has been reached.",
        "Wait before retrying, or use /provider to switch accounts.",
    ),
    "context_overflow": (
        "The conversation is too long for this model.",
        "Run /compact to summarize it, then try again.",
    ),
    "request_rejected": (
        "The provider rejected this request.",
        "Try /model or /effort. If this persists, Loupe may need an update.",
    ),
}


def render_event(event: dict[str, Any]) -> str | None:
    """Return one display line for a durable event, or ``None`` to skip it."""

    kind = str(event.get("event_type", ""))
    if kind not in _PUBLIC_EVENTS:
        return None
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    marker = _MARKERS.get(kind, " ")

    if kind == "command.started":
        return f"  Running: {_command_text(payload)}"
    if kind == "command.finished":
        return f"  Command result: {_command_outcome(payload)}"
    if kind == "explore.started":
        return f"  Exploring: {_exploration_name(payload)}"
    if kind == "explore.finished":
        return f"  {_exploration_outcome(payload)}"
    if kind == "plan.updated":
        steps = _plan_steps(payload)
        if not steps:
            return None
        return "\n".join(
            [f"  {_plan_heading(steps)}"]
            + [f"    {_PLAN_MARKS[status]} {step}" for step, status in steps]
        )
    if kind == "check.started":
        return f"  Running check: {safe_text(payload.get('name', ''))}"
    if kind == "check.finished":
        name = safe_text(payload.get("name", ""))
        state = safe_text(payload.get("state", ""))
        return f"  Check {name}: {state} (exit {payload.get('exit_code')})"
    if kind == "workflow.awaiting_review":
        return _review_notice(payload)
    if kind == "workflow.cancelled":
        return "  Stopped. Pending edits retained; checkout unchanged."
    if kind == "workflow.stopping":
        return "  Stopping at the next safe boundary…"
    if kind == "workflow.verification":
        # The stateful renderer waits for the outcome to tell whether checks
        # apply to any edits. A read-only answer needs no verification banner.
        return None

    if kind == "execution.published" and payload.get("outcome") == "no_changes":
        return None
    if kind == "execution.failed":
        failure = payload.get("failure_code")
        if failure in _QUIET_SETTLEMENT_FAILURES:
            assert isinstance(failure, str)
            return f"  ! {_SETTLEMENT_FAILURE_FALLBACKS[failure]}"
        if isinstance(failure, str) and failure in _INCOMPLETE_FAILURES:
            return f"  ! {_INCOMPLETE_FAILURES[failure]}"
    if kind == "tool.called":
        return f"  {marker} {safe_text(payload.get('tool', 'tool'))}"
    if kind in {
        "model.said",
        "model.text.delta",
        "model.partial",
    }:
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            return None
        prefix = "Partial response\n" if kind == "model.partial" else ""
        return prefix + safe_text(text)
    if kind in {
        "model.turn.started",
        "model.turn.completed",
        "model.tool.delta",
        "model.answer.preview",
        "model.reasoning",
        "model.reasoning.delta",
        "model.tool_result",
    }:
        return None
    if kind == "model.finished":
        if payload.get("answer"):
            return safe_text(payload["answer"])
        if payload.get("summary"):
            return "Task summary\n" + safe_text(payload["summary"])
        return None
    if kind == "question.asked":
        return None

    label = _LABELS.get(kind)
    if label is None:
        return None
    detail = _detail(kind, payload)
    return f"  {marker} {label}{detail}"


def _plan_steps(payload: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    raw = payload.get("steps")
    if not isinstance(raw, list):
        return ()
    steps: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            return ()
        step, status = item.get("step"), item.get("status")
        if not isinstance(step, str) or status not in _PLAN_MARKS:
            return ()
        steps.append((_clip(step), status))
    return tuple(steps)


def _plan_heading(steps: tuple[tuple[str, str], ...]) -> str:
    completed = sum(status == "completed" for _, status in steps)
    return f"Plan · {completed} of {len(steps)} done"


def _plan_progress(steps: tuple[tuple[str, str], ...]) -> str:
    """The footer's one-line view: the step in progress, or the plan's tally."""

    for number, (step, status) in enumerate(steps, 1):
        if status == "in_progress":
            return f"Step {number} of {len(steps)} · {step}"
    return _plan_heading(steps)


def _tool_activity(tool: str) -> str:
    if tool.startswith("mcp__"):
        server = tool.removeprefix("mcp__").split("__", 1)[0]
        return f"Using MCP server {_clip(server)}…"
    return _TOOL_ACTIVITIES.get(tool, "Working…")


def _exploration_name(payload: dict[str, Any]) -> str:
    name = _clip(payload.get("label") or payload.get("task", ""))
    if len(name) > _MAX_EXPLORATION_NAME:
        name = name[: _MAX_EXPLORATION_NAME - 1].rstrip() + "…"
    return name


def _exploration_outcome(payload: dict[str, Any]) -> str:
    task = _exploration_name(payload)
    calls = payload.get("tool_calls")
    seconds = payload.get("seconds")
    details = []
    if isinstance(calls, int) and not isinstance(calls, bool):
        details.append(f"{calls} tool call{'' if calls == 1 else 's'}")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        details.append(f"{seconds:.1f}s")
    suffix = f" · {', '.join(details)}" if details else ""
    if payload.get("state") == "completed":
        return f"↳ Explored: {task}{suffix}"
    reason = payload.get("reason")
    if isinstance(reason, str) and reason:
        suffix += f" ({_clip(reason)})"
    return f"! Exploration stopped early: {task}{suffix}"


def _command_text(payload: dict[str, Any]) -> str:
    argv = payload.get("argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        return "command"
    return _clip(shlex.join(argv))


def _command_outcome(payload: dict[str, Any]) -> str:
    state = payload.get("state")
    exit_code = payload.get("exit_code")
    duration = payload.get("duration")
    timed = isinstance(duration, (int, float)) and not isinstance(duration, bool)
    if state == "completed" and type(exit_code) is int:
        return f"exit {exit_code}" + (f" ({duration:.1f}s)" if timed else "")
    label = {"timed_out": "timed out", "cancelled": "stopped"}.get(str(state))
    if label is None:
        return "could not run"
    return label + (f" after {duration:.1f}s" if timed else "")


def _detail(kind: str, payload: dict[str, Any]) -> str:
    if kind in {"execution.scheduled", "driver.finished"} and payload.get("driver"):
        return f" ({_clip(payload['driver'])})"
    if kind == "model.started" and payload.get("model"):
        return f" ({_clip(payload['model'])})"
    if kind == "model.coordination_updated":
        sequence = payload.get("through_sequence")
        return f" (through event {sequence})" if type(sequence) is int else ""
    if kind == "model.instructions.loaded":
        paths = payload.get("paths")
        if isinstance(paths, list) and paths:
            return " (" + ", ".join(_clip(path) for path in paths[:4]) + ")"
        return ""
    if kind == "model.context.compacted":
        tokens = payload.get("context_tokens")
        if type(tokens) is int and tokens > 0:
            return f" (was about {max(1, round(tokens / 1000))}k tokens)"
        return ""
    if kind == "model.finished":
        calls = payload.get("tool_calls")
        return f" ({calls} tool calls)" if isinstance(calls, int) else ""
    if kind in {"mcp.server.started", "mcp.server.failed"}:
        server = _clip(payload.get("server", ""))
        tools = payload.get("tools")
        if kind == "mcp.server.started" and type(tools) is int:
            return f" {server} ({tools} tool{'' if tools == 1 else 's'})"
        reason = payload.get("reason")
        return f" ({server}: {_clip(reason)})" if reason else f" ({server})"
    if kind == "model.retrying":
        retry, retries, delay = (
            payload.get("retry"),
            payload.get("retries"),
            payload.get("delay"),
        )
        parts = [_clip(payload["reason"])] if payload.get("reason") else []
        if type(retry) is int and type(retries) is int:
            when = f" in {delay:g}s" if isinstance(delay, (int, float)) else ""
            parts.append(f"retry {retry} of {retries}{when}")
        return f" ({'; '.join(parts)})" if parts else ""
    failure = payload.get("failure_code")
    if failure:
        status = payload.get("provider_status")
        if type(status) is int and 100 <= status <= 599:
            return f" ({_clip(failure)}; HTTP {status})"
        return f" ({_clip(failure)})"
    reason = payload.get("reason")
    if reason:
        return f" ({_clip(reason)})"
    return ""


def _review_notice(payload: dict[str, Any]) -> str:
    count = payload.get("file_count")
    subject = (
        f"{count} file{'s' if count != 1 else ''}"
        if type(count) is int and count >= 0
        else "Changes"
    )
    outcome = payload.get("completion_outcome", "completed")
    verification = payload.get("verification")
    check = (
        f" · checks {safe_text(verification).replace('_', ' ')}"
        if isinstance(verification, str) and verification not in {"", "not_applicable"}
        else ""
    )
    if outcome == "completed":
        return f"  {subject} ready for review{check}. Use /diff, /checks, then /apply."
    state = {
        "blocked": "a blocked run",
        "partial": "an incomplete run",
        "failed": "a failed run",
    }.get(str(outcome), "an interrupted run")
    return (
        f"  {subject} retained from {state}{check}. "
        "Review /diff and /checks before /apply."
    )


def _clip(value: object) -> str:
    text = safe_text(str(value).replace("\n", " ").replace("\r", " "))
    printable = "".join(character for character in text if character.isprintable())
    return printable[:_MAX_DETAIL]


def question_text(event: dict[str, Any]) -> str | None:
    """Return the model's question if this event is one, else ``None``."""

    if str(event.get("event_type", "")) != "question.asked":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    question = payload.get("question")
    return safe_text(question) if question else None


def checkout_event_text(event: dict[str, Any], *, own_session_id: str) -> str | None:
    """Render compact, untrusted-data-safe coordination notices at prompt edges."""

    kind = event.get("event_type")
    if not isinstance(kind, str):
        return None
    caused_by = event.get("caused_by_session_id")
    if caused_by == own_session_id:
        return None
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if kind in {"workspace.batch_published", "workspace.change_published"}:
        paths = (
            payload.get("paths")
            if kind == "workspace.batch_published"
            else [payload.get("path")]
        )
        paths = paths if isinstance(paths, list) else []
        rendered = (
            ", ".join(
                _clip(path) for path in paths[:4] if isinstance(path, str) and path
            )
            or "checkout files"
        )
        suffix = " …" if len(paths) > 4 else ""
        return f"  Another session updated {rendered}{suffix}."
    return None


@dataclass
class _StreamedText:
    fragments: list[str] = field(default_factory=list)
    sanitizers: dict[str, TextSanitizer] = field(default_factory=dict)
    last_block: str | None = None

    def feed(self, block: str, text: str) -> str:
        cleaned = self.sanitizers.setdefault(block, TextSanitizer()).feed(text)
        if cleaned:
            if self.last_block is not None and self.last_block != block:
                self.fragments.append("\n")
            self.fragments.append(cleaned)
            self.last_block = block
        return cleaned

    @property
    def text(self) -> str:
        return "".join(self.fragments)


@dataclass
class _ToolError:
    tool: str
    text: str = ""
    complete: bool = False
    sanitizer: TextSanitizer = field(default_factory=TextSanitizer)

    def feed(self, content: str) -> None:
        """Keep only a bounded first diagnostic line, even across event chunks."""
        if self.complete:
            return
        for character in self.sanitizer.feed(content):
            if not self.text and character.isspace():
                continue
            if character == "\n" or len(self.text) >= _MAX_DETAIL:
                self.complete = True
                break
            self.text += character


class EventRenderer:
    """Render answers and concise activity from the durable event stream.

    Stable Markdown blocks appear as they arrive. Authoritative completions only
    append missing text, so replaying a stream doesn't print each answer twice.
    The renderer owns no alternate screen or cursor positioning, keeping native
    terminal selection, searching, and scrollback available throughout a task.
    """

    def __init__(self, stream: TextIO, *, plain: bool = False) -> None:
        self.ui = TerminalUI(stream, plain=plain)
        self._streams: dict[tuple[str, str], _StreamedText] = {}
        self._active: tuple[str, str] | None = None
        self._markdown: MarkdownStream | None = None
        self._turn_id = ""
        self._parts: dict[tuple[str, str, str, str], list[str]] = {}
        self._tool_errors: dict[tuple[str, str], _ToolError] = {}
        self._last_assistant = ""
        self._task_id = ""
        self._claim_id = ""
        self._check_failed = False
        self._command: str | None = None
        self._plan: tuple[tuple[str, str], ...] = ()
        # Running explorations by id, with their display task.
        self._explorations: dict[str, str] = {}
        self._completed = False
        self._verification: str | None = None
        self._answer_ids: set[str] = set()
        self._pending_settlement_failure: tuple[str, str] | None = None
        self._pending_provider_failure: dict[str, Any] | None = None
        self._provider_failure_displayed = False
        # Live answers: finish_task arguments stream before the answer is
        # accepted. Read-only drafts are shown as they arrive; drafts from a
        # task with edits, and text whose role is not yet known, are previews.
        self._edited = False
        self._answers: dict[str, AnswerFieldDecoder] = {}
        self._streamed_calls: set[str] = set()
        self._revising = False
        self._preview_text = ""
        self._preview_source: str | None = None

    def render(self, event: dict[str, Any]) -> None:
        kind = str(event.get("event_type", ""))
        if kind not in _PUBLIC_EVENTS:
            return
        task_id = event.get("task_id")
        claim_id = event.get("claim_id")
        raw = event.get("payload")
        payload = dict(raw) if isinstance(raw, dict) else {}
        provider_failure = (
            kind in _PROVIDER_FAILURE_EVENTS
            and payload.get("failure_code") == "PROVIDER_UNAVAILABLE"
        )
        if self._pending_provider_failure is not None and (
            (isinstance(task_id, str) and task_id != self._task_id)
            or (isinstance(claim_id, str) and claim_id != self._claim_id)
            or (not provider_failure and kind not in _QUIET_EVENTS)
        ):
            self._flush_provider_failure()
        if isinstance(task_id, str) and task_id != self._task_id:
            self._flush_settlement_failure()
            self._task_id = task_id
            self._completed = False
            self._verification = None
            self._provider_failure_displayed = False
            self._last_assistant = ""
            self._edited = False
            self._answers.clear()
            self._streamed_calls.clear()
            self._revising = False
            self._clear_preview()
            self._plan = ()
            self._explorations.clear()
            self.ui.progress(None)
        if isinstance(claim_id, str) and claim_id != self._claim_id:
            self._claim_id = claim_id
            self._provider_failure_displayed = False
        if kind in {
            "execution.preparing",
            "model.started",
            "model.resumed",
            "model.turn.started",
        }:
            self._completed = False
            self._provider_failure_displayed = False
        if provider_failure:
            self._end_block()
            self.ui.activity(None)
            if not self._provider_failure_displayed:
                self._pending_provider_failure = {
                    **(self._pending_provider_failure or {}),
                    **payload,
                }
                if kind == "execution.background_failed":
                    self._flush_provider_failure()
            return
        failure = payload.get("failure_code")
        if (
            kind == "execution.failed"
            and isinstance(failure, str)
            and failure in _QUIET_SETTLEMENT_FAILURES
        ):
            self._end_block()
            self.ui.activity(None)
            self._pending_settlement_failure = (
                task_id if isinstance(task_id, str) else self._task_id,
                failure,
            )
            return
        if kind in {"workflow.awaiting_review", "workflow.cancelled"}:
            expected = (
                "REVIEW_REQUIRED" if kind == "workflow.awaiting_review" else "CANCELLED"
            )
            pending = self._pending_settlement_failure
            if pending is not None and pending[1] == expected:
                self._pending_settlement_failure = None
        turn = str(payload.get("turn_id", self._turn_id))
        message_id = payload.get("message_id")
        if (
            kind == "model.finished"
            and isinstance(message_id, str)
            and message_id in self._answer_ids
        ):
            return
        if kind == "model.turn.started":
            self.finish()
            self._streams.clear()
            self._turn_id = turn
            self.ui.activity("Thinking…")
            return
        if kind in {"model.reasoning", "model.reasoning.delta"}:
            # Progress comes from known lifecycle/tool actions, never from
            # quoting or trying to summarize the model's reasoning text.
            if self._active is None:
                self.ui.activity("Thinking…")
            return
        if kind == "model.answer.preview":
            # Text whose role (answer or narration) is known only once its
            # turn completes. It stays transient; model.finished commits it.
            text = payload.get("text")
            if isinstance(text, str) and text:
                self._preview_text += text
                self._preview_source = "text"
                self.ui.preview(self._preview_text)
            return
        if kind == "model.tool.delta" and payload.get("tool") == "finish_task":
            self._stream_answer(turn, payload)
            return
        if kind in {"model.tool.delta", "model.tool_call", "tool.called"}:
            self._end_block()
            tool = str(payload.get("tool", ""))
            self.ui.activity(
                "Thinking…" if kind == "model.tool.delta" else _tool_activity(tool)
            )
            if kind == "model.tool_call":
                if tool in _WRITE_TOOLS:
                    self._edited = True
                elif tool == "finish_task":
                    call = str(payload.get("call_id", ""))
                    decoder = self._answers.get(call)
                    if call in self._streamed_calls and decoder is not None:
                        # Already on screen: model.finished must not repeat it.
                        self._last_assistant = safe_text(decoder.text)
            if tool == "run_check" and kind == "model.tool_call":
                self._check_failed = False
            return
        if kind in {"model.tool_result", "tool.result", "tool.completed"}:
            if not payload.get("is_error"):
                return
            self._end_block()
            tool = safe_text(payload.get("tool") or "tool")
            if tool == "finish_task":
                # Completion corrections are instructions for the model. The
                # user sees its resulting answer or terminal execution failure.
                call = str(payload.get("call_id", ""))
                self._answers.pop(call, None)
                if self._preview_source == "answer":
                    self._clear_preview()
                if call in self._streamed_calls:
                    # The draft is already in scrollback; say it is not final.
                    self._streamed_calls.discard(call)
                    self._revising = True
                    self._last_assistant = ""
                    self._streams.pop((turn, "text"), None)
                    self.ui.notice(
                        "  ! Loupe is revising this answer…", style="warning"
                    )
                self.ui.activity("Preparing response…")
                return
            self.ui.activity(None)
            if tool == "run_check" and self._check_failed:
                return
            key = (kind, str(payload.get("call_id", "")))
            error = self._tool_errors.setdefault(key, _ToolError(tool))
            error.feed(str(payload.get("content", payload.get("output", ""))))
            if "part" not in payload or payload.get("final"):
                self._show_tool_error(self._tool_errors.pop(key))
            return
        completed = self._collect_parts(kind, turn, payload)
        if completed is None:
            return
        payload = completed
        if kind == "model.turn.completed":
            self._end_block()
            if self._preview_source == "text":
                # The turn established the text's role; only model.finished
                # can turn it into the answer.
                self._clear_preview()
            return
        if kind == "model.text.delta":
            text = payload.get("text")
            if isinstance(text, str) and text:
                self._stream_text(turn, str(payload.get("block_id", "")), text)
            return
        if kind == "model.said":
            text = payload.get("text")
            if isinstance(text, str) and text:
                self._complete_text(turn, "text", text)
            return
        if kind == "model.partial":
            text = payload.get("text")
            if isinstance(text, str) and text:
                self._complete_text(turn, "text", text, heading="Partial response")
            return
        if kind in _QUIET_EVENTS:
            return
        if kind != "model.finished":
            self._end_block()
        if kind in {"model.started", "model.resumed"}:
            self.ui.activity("Thinking…")
            return
        if kind == "execution.published":
            self.ui.activity(None)
            if not self._completed:
                if payload.get("outcome") != "no_changes":
                    suffix = (
                        f" · checks {self._verification.lower()}"
                        if self._verification
                        else ""
                    )
                    self.ui.notice(f"\n  ✓ Changes applied{suffix}\n", style="success")
                self._completed = True
            self._verification = None
            return
        if kind == "workflow.verification":
            status = payload.get("status")
            self._verification = _clip(status) if isinstance(status, str) else None
            return
        if kind in _CONVERSATION_PHASES:
            if kind == "execution.cleanup_failed":
                self.ui.activity(None)
                self.ui.notice(f"  {_CONVERSATION_PHASES[kind]}", style="warning")
            else:
                self.ui.activity(_CONVERSATION_PHASES[kind])
            return
        if kind == "explore.started":
            exploration = str(payload.get("exploration_id", ""))
            self._explorations[exploration] = _exploration_name(payload)
            self._show_explorations()
            return
        if kind == "explore.finished":
            self._explorations.pop(str(payload.get("exploration_id", "")), None)
            self.ui.notice(
                f"  {_exploration_outcome(payload)}",
                style="muted" if payload.get("state") == "completed" else "warning",
            )
            self._show_explorations()
            return
        if kind == "plan.updated":
            steps = _plan_steps(payload)
            if not steps or steps == self._plan:
                return
            self._plan = steps
            self.ui.notice(f"  {_plan_heading(steps)}")
            for step, status in steps:
                self.ui.notice(
                    f"    {_PLAN_MARKS[status]} {step}",
                    style={"completed": "muted", "in_progress": "assistant"}.get(
                        status, ""
                    ),
                )
            self.ui.progress(_plan_progress(steps))
            return
        if kind == "command.started":
            self._command = _command_text(payload)
            self.ui.activity(f"Running {self._command}…")
            return
        if kind == "command.finished":
            self.ui.activity(None)
            # Model-chosen commands are shown as they run, so the transcript
            # records exactly what executed. A failing exit is information.
            command = self._command or "command"
            self._command = None
            self.ui.notice(
                f"  $ {command} · {_command_outcome(payload)}",
                style="muted" if payload.get("state") == "completed" else "warning",
            )
            return
        if kind == "check.started":
            self._check_failed = False
            self.ui.activity("Running checks…")
            return
        if kind == "check.finished":
            self._check_failed = payload.get("state") != "passed"
            if self._check_failed:
                self.ui.activity(None)
                self.ui.error(
                    f"Check {_clip(payload.get('name', ''))}: "
                    f"{_clip(payload.get('state', 'failed'))} "
                    f"(exit {payload.get('exit_code')}). Use /checks for details."
                )
            else:
                self.ui.activity("Checks passed.")
            return
        if kind == "question.asked":
            self.ui.activity(None)
            question = question_text(event)
            if question:
                self.ui.message_heading("Loupe needs your input", style="warning")
                self.ui.body(question)
            return
        if kind == "model.finished":
            self.ui.activity(None)
            self.ui.progress(None)
            self._clear_preview()
            answer = payload.get("answer")
            summary = payload.get("summary")
            outcome = str(payload.get("outcome"))
            if isinstance(answer, str) and answer.strip():
                if safe_text(answer).strip() != self._last_assistant.strip():
                    heading = {
                        "blocked": "Loupe — blocked",
                        "partial": "Loupe — incomplete",
                    }.get(
                        outcome, "Loupe — revised answer" if self._revising else "Loupe"
                    )
                    self._complete_text(turn, "text", answer, heading=heading)
                elif outcome in _STREAMED_OUTCOMES:
                    # The answer streamed before its outcome was known.
                    self._end_block()
                    self.ui.notice(
                        f"  ! {_STREAMED_OUTCOMES[outcome]}", style="warning"
                    )
            elif (
                isinstance(summary, str)
                and summary.strip()
                and safe_text(summary).strip() != self._last_assistant.strip()
            ):
                self._complete_text(turn, "text", summary, heading="Task summary")
            if isinstance(message_id, str):
                self._answer_ids.add(message_id)
            self._revising = False
            # The model can finish before validation or saving fails. Only the
            # execution's confirmed success earns the friendly completion line.
            self._end_block()
            return
        if kind == "model.retrying":
            # The failed attempt's draft is not an answer; the retry redraws.
            self._clear_preview()
        line = render_event({**event, "payload": payload})
        if line is not None:
            self.ui.activity(None)
            style = "muted"
            if kind in {
                "execution.failed",
                "execution.background_failed",
                "model.failed",
                "model.refused",
            }:
                style = (
                    "warning"
                    if payload.get("failure_code") in _INCOMPLETE_FAILURES
                    else "error"
                )
            elif kind in {
                "execution.published",
                "publication.confirmed",
                "model.finished",
            }:
                style = "success"
            elif kind.endswith("operator_attention") or kind in {
                "model.stalled",
                "model.context.compaction_failed",
                "mcp.server.failed",
                "model.retrying",
            }:
                style = "warning"
            self.ui.notice(line, style=style)

    def _show_explorations(self) -> None:
        if not self._explorations:
            self.ui.activity("Thinking…")
        elif len(self._explorations) == 1:
            (task,) = self._explorations.values()
            self.ui.activity(f"Exploring: {task.removesuffix('…')}…")
        else:
            self.ui.activity(
                f"Exploring {len(self._explorations)} questions in parallel…"
            )

    def _collect_parts(
        self, kind: str, turn: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        if "part" not in payload:
            return payload
        field_names = {
            "model.said": ("text",),
            "model.partial": ("text",),
            "model.finished": ("answer", "summary"),
        }.get(kind)
        if field_names is None:
            return payload
        for field_name in field_names:
            key = (
                turn,
                kind,
                str(payload.get("message_id", payload.get("call_id", ""))),
                field_name,
            )
            value = payload.get(field_name)
            if isinstance(value, str):
                self._parts.setdefault(key, []).append(value)
            if payload.get("final") and key in self._parts:
                payload[field_name] = "".join(self._parts.pop(key))
        if not payload.get("final"):
            return None
        return payload

    def _stream_text(
        self, turn: str, block: str, text: str, *, heading: str = "Loupe"
    ) -> None:
        state = self._streams.setdefault((turn, "text"), _StreamedText())
        previous_block = state.last_block
        cleaned = state.feed(block, text)
        if cleaned:
            self._begin_block(turn, "text", heading=heading)
            assert self._markdown is not None
            if previous_block is not None and previous_block != block:
                self._markdown.feed("\n")
            self._markdown.feed(cleaned)

    def _stream_answer(self, turn: str, payload: dict[str, Any]) -> None:
        """Show the ``answer`` argument of a finish_task call as it streams."""

        call = str(payload.get("call_id", ""))
        decoder = self._answers.setdefault(call, AnswerFieldDecoder())
        delta = payload.get("arguments_delta")
        text = decoder.feed(delta) if isinstance(delta, str) else ""
        if self._edited:
            # Edits can still fail checks or publication: preview, don't commit.
            self._end_block()
            if decoder.text:
                self._preview_source = "answer"
                self.ui.preview(decoder.text)
            else:
                self.ui.activity("Writing the answer…")
            return
        if text:
            self._streamed_calls.add(call)
            heading = "Loupe — revised answer" if self._revising else "Loupe"
            self._stream_text(turn, "finish_task:" + call, text, heading=heading)
        elif self._active is None:
            self.ui.activity("Writing the answer…")

    def _clear_preview(self) -> None:
        if self._preview_source is not None or self._preview_text:
            self.ui.preview(None)
        self._preview_text = ""
        self._preview_source = None

    def _begin_block(self, turn: str, channel: str, *, heading: str = "Loupe") -> None:
        key = (turn, channel)
        if self._active == key:
            return
        self._end_block()
        self.ui.activity(None)
        self.ui.message_heading(heading, style="assistant")
        if not self.ui.plain:
            self.ui.activity("Writing response…")
        self._active = key
        self._markdown = MarkdownStream(self.ui)

    def _complete_text(
        self, turn: str, channel: str, text: str, *, heading: str = "Loupe"
    ) -> None:
        cleaned = safe_text(text)
        state = self._streams.pop((turn, channel), None)
        streamed = state.text if state else ""
        if streamed and cleaned.startswith(streamed):
            suffix = cleaned[len(streamed) :]
            if suffix:
                # Reuse the active streaming block when it is still open.
                self._begin_block(turn, channel, heading=heading)
                assert self._markdown is not None
                self._markdown.feed(suffix)
            self._end_block()
        elif streamed and cleaned.strip() == streamed.strip():
            self._end_block()
        else:
            self._end_block()
            self.ui.activity(None)
            self.ui.message_heading(heading, style="assistant")
            self.ui.body(cleaned, markdown=True)
        if channel == "text":
            self._last_assistant = cleaned

    def _end_block(self) -> None:
        if self._markdown is not None:
            self._markdown.finish()
            self._markdown = None
            self.ui.activity(None)
        self._active = None

    def _show_tool_error(self, error: _ToolError) -> None:
        self.ui.error(f"{_clip(error.tool)}: {error.text.strip() or 'Tool failed'}")

    def _flush_provider_failure(self) -> None:
        payload = self._pending_provider_failure
        if payload is None:
            return
        self._pending_provider_failure = None
        if self._provider_failure_displayed:
            return
        self._provider_failure_displayed = True
        self._end_block()
        self.ui.activity(None)
        status = payload.get("provider_status")
        status = status if type(status) is int and 100 <= status <= 599 else None
        category = payload.get("provider_error")
        # Older durable records contain only HTTP status. Keep those actionable
        # without rendering any untrusted provider message as a recovery hint.
        if not isinstance(category, str) or category not in _PROVIDER_ERROR_HINTS:
            category = (
                {
                    400: "request_rejected",
                    401: "authentication",
                    403: "authentication",
                    429: "rate_limit",
                }.get(status)
                if status is not None
                else None
            )
        if category in _PROVIDER_ERROR_HINTS:
            summary, hint = _PROVIDER_ERROR_HINTS[category]
            suffix = f" (HTTP {status})" if status is not None else ""
            self.ui.notice(f"  ✗ {summary.rstrip('.')}{suffix}.", style="error")
        else:
            self.ui.notice(
                "  ✗ failed" + _detail("execution.failed", payload), style="error"
            )
            hint = "Try again, or use /provider to connect another account."
        self.ui.notice(f"    {hint}")

    def finish(self) -> None:
        """Flush an interrupted answer, without exposing hidden trace payloads."""

        # An interrupted chunked completion is still useful, even without its
        # final part. Do not silently discard anything already delivered.
        streamed_response_interrupted = False
        for (turn, kind, call, field_name), parts in self._parts.items():
            if parts:
                if field_name == "summary" and any(
                    self._parts.get((turn, kind, call, "answer"), [])
                ):
                    continue
                partial = safe_text("".join(parts))
                state = self._streams.get((turn, "text"))
                streamed = state.text if state else ""
                if (
                    field_name != "summary"
                    and streamed
                    and (streamed.startswith(partial) or partial.startswith(streamed))
                ):
                    streamed_response_interrupted = True
                if streamed and streamed.startswith(partial):
                    continue
                if kind == "model.finished" and self._last_assistant.startswith(
                    partial
                ):
                    streamed_response_interrupted = True
                    continue
                heading = (
                    "Partial task summary"
                    if field_name == "summary"
                    else "Partial response"
                )
                self._complete_text(turn, "text", partial, heading=heading)
        self._parts.clear()
        self._end_block()
        self._clear_preview()
        if streamed_response_interrupted:
            self.ui.notice(
                "  ! Partial response (stream interrupted).", style="warning"
            )
        self._flush_provider_failure()
        self._flush_settlement_failure()
        self.ui.activity(None)
        for error in self._tool_errors.values():
            self._show_tool_error(error)
        self._tool_errors.clear()

    def _flush_settlement_failure(self) -> None:
        pending = self._pending_settlement_failure
        if pending is None:
            return
        self._pending_settlement_failure = None
        self.ui.activity(None)
        self.ui.notice(
            f"  ! {_SETTLEMENT_FAILURE_FALLBACKS[pending[1]]}", style="warning"
        )


__all__ = ["EventRenderer", "checkout_event_text", "question_text", "render_event"]
