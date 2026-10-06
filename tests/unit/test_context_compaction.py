"""Long histories are summarized at safe boundaries before they overflow."""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from llm_cli.agent.driver import CoordinationUpdate, RunRequest
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.tools import ToolBroker
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.providers.base import (
    CompactableSession,
    ModelTurn,
    ToolCallRequest,
    ToolCallResult,
    context_overflow_error,
)

_BUDGET = 100_000


class Compactable:
    name = "script"
    model = "script"

    def __init__(
        self,
        turns: Sequence[ModelTurn | Exception],
        *,
        summary: ModelTurn | Exception | Sequence[ModelTurn | Exception] | None = None,
        budget: int | None = _BUDGET,
    ) -> None:
        self.turns = iter(turns)
        default = ModelTurn(text="SUMMARY: inspected docs/a.md")
        self.summaries = (
            list(summary) if isinstance(summary, Sequence) else [summary or default]
        )
        self.history: list[object] = []
        self.calls: list[object] = []
        self.summarize_calls: list[str] = []
        self.summary_limits: list[int | None] = []
        self.summary_pending: list[tuple[str, ...]] = []
        if budget is not None:
            self.input_token_budget = budget

    def session(
        self, *, system: str, tools: object, state: object = None
    ) -> Compactable:
        if isinstance(state, dict):
            self.history = copy.deepcopy(state["messages"])
        return self

    def snapshot(self) -> dict[str, object]:
        return {"messages": copy.deepcopy(self.history)}

    def _next(self) -> ModelTurn:
        outcome = next(self.turns)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def send_user(self, text: str) -> ModelTurn:
        self.calls.append(text)
        # Like real adapters, a failed request leaves history unchanged.
        turn = self._next()
        self.history.append(text)
        return turn

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        self.calls.append(tuple(results))
        turn = self._next()
        self.history.append([result.content for result in results])
        return turn

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        raise AssertionError("summaries must not record tool results first")

    def summarize(
        self,
        instruction: str,
        *,
        max_tool_text: int | None = None,
        pending_results: Sequence[ToolCallResult] = (),
    ) -> ModelTurn:
        self.summarize_calls.append(instruction)
        self.summary_limits.append(max_tool_text)
        self.summary_pending.append(tuple(item.call_id for item in pending_results))
        outcome = (
            self.summaries.pop(0) if len(self.summaries) > 1 else self.summaries[0]
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def replace_history(self, summary: str) -> None:
        self.history = [summary]


def _tool_turn(context_tokens: int) -> ModelTurn:
    return ModelTurn(
        text="",
        tool_calls=(ToolCallRequest("read-1", "list_files", {"path": "docs"}),),
        stop_reason="tool_use",
        context_tokens=context_tokens,
    )


def _answer(text: str = "final answer") -> ModelTurn:
    return ModelTurn(text=text, context_tokens=2_000)


def _run(
    provider: Compactable,
    tmp_path: Path,
    *,
    conversation_state: dict[str, object] | None = None,
    refresh: Callable[[int | None], CoordinationUpdate] | None = None,
) -> tuple[list[tuple[str, dict[str, object]]], list[dict[str, object]], str]:
    (tmp_path / "docs").mkdir(exist_ok=True)
    events: list[tuple[str, dict[str, object]]] = []
    checkpoints: list[dict[str, object]] = []
    request = replace(
        RunRequest("task-compact", 1, "inspect", ("docs/",), tmp_path, "base"),
        checkpoint=lambda state: checkpoints.append(copy.deepcopy(dict(state))),
        conversation_state=conversation_state,
        refresh_coordination=refresh,
    )
    result = CodingAgentHarness(provider).run(
        request,
        ToolBroker(
            tmp_path,
            ("docs/",),
            on_event=lambda kind, payload: events.append((kind, payload)),
        ),
    )
    return events, checkpoints, result.answer


def test_session_protocol_detects_compaction_support() -> None:
    assert isinstance(Compactable([]), CompactableSession)


def test_large_tool_turn_is_summarized_before_results_are_sent(
    tmp_path: Path,
) -> None:
    provider = Compactable([_tool_turn(90_000), _answer()])

    events, checkpoints, answer = _run(provider, tmp_path)

    assert answer == "final answer"
    assert len(provider.summarize_calls) == 1
    assert "Do not call tools" in provider.summarize_calls[0]
    # The summary covered the results, so they were never sent on their own.
    assert provider.summary_pending == [("read-1",)]
    assert not any(isinstance(call, tuple) for call in provider.calls)
    assert "summary above" in str(provider.calls[-1])
    handoff = str(provider.history[0])
    assert handoff.startswith("[Loupe summarized")
    assert "SUMMARY: inspected docs/a.md" in handoff
    assert "Read\nfiles again" in handoff
    # Loupe restates the current task instead of relying on the summary.
    assert "restated by Loupe" in handoff
    assert "Task: inspect" in handoff
    assert "- docs/" in handoff
    compacted = [
        payload for kind, payload in events if kind == "model.context.compacted"
    ]
    # The size before includes the results the summary absorbed.
    assert compacted and compacted[0]["context_tokens"] > 90_000
    assert any(state["phase"] == "continue" for state in checkpoints)
    assert checkpoints[-1]["context_tokens"] == 2_000


def test_small_history_is_never_summarized(tmp_path: Path) -> None:
    provider = Compactable([_tool_turn(5_000), _answer()])

    events, _, answer = _run(provider, tmp_path)

    assert answer == "final answer"
    assert provider.summarize_calls == []
    assert isinstance(provider.calls[1], tuple)
    assert not any(kind.startswith("model.context") for kind, _ in events)


def test_provider_without_budget_never_summarizes(tmp_path: Path) -> None:
    provider = Compactable([_tool_turn(900_000), _answer()], budget=None)

    _, _, answer = _run(provider, tmp_path)

    assert answer == "final answer"
    assert provider.summarize_calls == []


def test_failed_summary_is_reported_and_the_task_continues(tmp_path: Path) -> None:
    provider = Compactable(
        [_tool_turn(90_000), _answer()],
        summary=LlmCoordError(ErrorCode.PROVIDER_UNAVAILABLE, "provider offline"),
    )

    events, _, answer = _run(provider, tmp_path)

    assert answer == "final answer"
    failures = [
        payload for kind, payload in events if kind == "model.context.compaction_failed"
    ]
    assert failures == [{"reason": "provider offline"}]
    # Nothing was recorded for the summary, so the results went out as usual.
    assert isinstance(provider.calls[1], tuple)
    assert not any("summary above" in str(call) for call in provider.calls)
    assert not str(provider.history[0]).startswith("[Loupe summarized")


def test_truncated_summary_is_rejected(tmp_path: Path) -> None:
    provider = Compactable(
        [_tool_turn(90_000), _answer()],
        summary=ModelTurn(text="partial", stop_reason="max_tokens"),
    )

    events, _, _ = _run(provider, tmp_path)

    assert ("model.context.compaction_failed") in [kind for kind, _ in events]
    assert not str(provider.history[0]).startswith("[Loupe summarized")


def test_long_prior_conversation_is_summarized_before_the_next_prompt(
    tmp_path: Path,
) -> None:
    provider = Compactable([_answer("second answer")])
    prior: dict[str, object] = {
        "version": 2,
        "provider": "script",
        "model": "script",
        "session": {"messages": ["first prompt", "first answer"]},
        "context_tokens": 95_000,
    }

    events, _, answer = _run(provider, tmp_path, conversation_state=prior)

    assert answer == "second answer"
    assert len(provider.summarize_calls) == 1
    assert str(provider.history[0]).startswith("[Loupe summarized")
    # This task's prompt follows the summary, so nothing is restated.
    assert "restated by Loupe" not in str(provider.history[0])
    assert "Task: inspect" in str(provider.history[1])
    assert [kind for kind, _ in events].count("model.context.compacted") == 1


def test_resume_after_summary_continues_without_resending_results(
    tmp_path: Path,
) -> None:
    first = Compactable([_tool_turn(90_000)])
    checkpoints: list[dict[str, object]] = []
    (tmp_path / "docs").mkdir()
    request = replace(
        RunRequest("task-compact", 1, "inspect", ("docs/",), tmp_path, "base"),
        checkpoint=lambda state: checkpoints.append(copy.deepcopy(dict(state))),
    )
    # The scripted provider runs out of turns after the summary checkpoint.
    with contextlib.suppress(StopIteration):
        CodingAgentHarness(first).run(request, ToolBroker(tmp_path, ("docs/",)))
    saved = next(state for state in checkpoints if state["phase"] == "continue")

    resumed = Compactable([_answer("resumed answer")])
    result = CodingAgentHarness(resumed).run(
        replace(request, resume_state=saved), ToolBroker(tmp_path, ("docs/",))
    )

    assert result.answer == "resumed answer"
    assert resumed.summarize_calls == []
    assert str(resumed.history[0]).startswith("[Loupe summarized")
    assert "summary above" in str(resumed.calls[0])


def test_rejected_tool_results_are_recorded_summarized_and_continued(
    tmp_path: Path,
) -> None:
    # Usage looked small, but the provider still rejected the prompt.
    provider = Compactable([_tool_turn(9_000), context_overflow_error(400), _answer()])

    events, checkpoints, answer = _run(provider, tmp_path)

    assert answer == "final answer"
    assert len(provider.summarize_calls) == 1
    assert provider.summary_pending == [("read-1",)]
    assert str(provider.history[0]).startswith("[Loupe summarized")
    assert "summary above" in str(provider.calls[-1])
    assert [kind for kind, _ in events].count("model.context.compacted") == 1
    assert any(state["phase"] == "continue" for state in checkpoints)


def test_rejected_prompt_is_summarized_and_sent_again(tmp_path: Path) -> None:
    provider = Compactable([context_overflow_error(400), _answer("second answer")])
    prior: dict[str, object] = {
        "version": 2,
        "provider": "script",
        "model": "script",
        "session": {"messages": ["first prompt", "first answer"]},
        "context_tokens": 20_000,
    }

    _, _, answer = _run(provider, tmp_path, conversation_state=prior)

    assert answer == "second answer"
    assert len(provider.summarize_calls) == 1
    assert provider.calls[0] == provider.calls[1]
    assert str(provider.history[0]).startswith("[Loupe summarized")
    assert "Task: inspect" in str(provider.history[1])


def test_second_rejection_in_one_turn_fails_the_task(tmp_path: Path) -> None:
    provider = Compactable(
        [_tool_turn(9_000), context_overflow_error(400), context_overflow_error(400)]
    )

    with pytest.raises(LlmCoordError) as failure:
        _run(provider, tmp_path)

    assert failure.value.details == {
        "provider_error": "context_overflow",
        "status_code": 400,
    }
    assert len(provider.summarize_calls) == 1


def test_rejection_without_a_usable_summary_reports_the_original_error(
    tmp_path: Path,
) -> None:
    provider = Compactable(
        [_tool_turn(9_000), context_overflow_error(400)],
        summary=LlmCoordError(ErrorCode.PROVIDER_UNAVAILABLE, "provider offline"),
    )

    with pytest.raises(LlmCoordError, match="too long for the model"):
        _run(provider, tmp_path)


def test_other_provider_errors_are_not_retried(tmp_path: Path) -> None:
    provider = Compactable(
        [_tool_turn(9_000), LlmCoordError(ErrorCode.PROVIDER_UNAVAILABLE, "HTTP 500")]
    )

    with pytest.raises(LlmCoordError, match="HTTP 500"):
        _run(provider, tmp_path)

    assert provider.summarize_calls == []


def test_oversized_summary_request_retries_with_shorter_tool_text(
    tmp_path: Path,
) -> None:
    provider = Compactable(
        [_tool_turn(90_000), _answer()],
        summary=[
            context_overflow_error(400),
            context_overflow_error(400),
            ModelTurn(text="short summary"),
        ],
    )

    _, _, answer = _run(provider, tmp_path)

    assert answer == "final answer"
    assert provider.summary_limits == [None, 4_000, 500]
    assert "short summary" in str(provider.history[0])


def test_saved_conversation_can_be_summarized_between_tasks(tmp_path: Path) -> None:
    provider = Compactable([])
    conversation: dict[str, object] = {
        "version": 1,
        "provider": "script",
        "model": "script",
        "session": {"messages": ["first prompt", "first answer"]},
        "coordination_sequence": 4,
        "context_tokens": 120_000,
    }

    updated, before, after = CodingAgentHarness(provider).compact_conversation(
        conversation, worktree=tmp_path
    )

    assert before == 120_000
    assert 0 < after < before
    assert updated["coordination_sequence"] == 4
    assert updated["context_tokens"] == after
    messages = updated["session"]["messages"]  # type: ignore[index]
    assert len(messages) == 1
    assert "SUMMARY: inspected docs/a.md" in messages[0]


def test_failed_standalone_summary_raises_and_keeps_history(tmp_path: Path) -> None:
    provider = Compactable(
        [], summary=ModelTurn(text="partial", stop_reason="max_tokens")
    )
    conversation: dict[str, object] = {
        "version": 1,
        "provider": "script",
        "model": "script",
        "session": {"messages": ["first prompt", "first answer"]},
    }

    with pytest.raises(LlmCoordError, match="could not be summarized"):
        CodingAgentHarness(provider).compact_conversation(
            conversation, worktree=tmp_path
        )

    assert provider.history == ["first prompt", "first answer"]


def test_rejected_prompt_without_history_is_not_summarized(tmp_path: Path) -> None:
    provider = Compactable([context_overflow_error(400)])

    with pytest.raises(LlmCoordError, match="too long for the model"):
        _run(provider, tmp_path)

    assert provider.summarize_calls == []
    assert provider.history == []


def test_rejection_after_a_planned_summary_does_not_summarize_again(
    tmp_path: Path,
) -> None:
    provider = Compactable([context_overflow_error(400)])
    prior: dict[str, object] = {
        "version": 2,
        "provider": "script",
        "model": "script",
        "session": {"messages": ["first prompt", "first answer"]},
        "context_tokens": 95_000,
    }

    with pytest.raises(LlmCoordError, match="too long for the model"):
        _run(provider, tmp_path, conversation_state=prior)

    assert len(provider.summarize_calls) == 1


def test_coordination_update_survives_a_summary(tmp_path: Path) -> None:
    provider = Compactable([_tool_turn(90_000), _answer()])

    def refresh(after: int | None) -> CoordinationUpdate:
        if after is None:
            return CoordinationUpdate(1, "initial context")
        if after < 2:
            return CoordinationUpdate(2, "peer edited docs/a.md")
        return CoordinationUpdate(after, None)

    _, checkpoints, answer = _run(provider, tmp_path, refresh=refresh)

    assert answer == "final answer"
    # The update rode on the summarized results, so the continue prompt
    # carries it again and only then marks it consumed.
    assert "peer edited docs/a.md" in str(provider.calls[-1])
    continuing = next(state for state in checkpoints if state["phase"] == "continue")
    assert continuing["coordination_sequence"] == 1
    assert checkpoints[-1]["coordination_sequence"] == 2


def test_recorded_plan_survives_a_summary(tmp_path: Path) -> None:
    steps = [
        {"step": "Inspect docs", "status": "completed"},
        {"step": "Write the answer", "status": "in_progress"},
    ]
    plan_turn = ModelTurn(
        text="",
        tool_calls=(ToolCallRequest("plan-1", "update_plan", {"steps": steps}),),
        stop_reason="tool_use",
        context_tokens=5_000,
    )
    provider = Compactable([plan_turn, _tool_turn(90_000), _answer()])

    _, _, answer = _run(provider, tmp_path)

    assert answer == "final answer"
    handoff = str(provider.history[0])
    assert "last recorded with update_plan" in handoff
    assert handoff.endswith("[x] Inspect docs\n[>] Write the answer")


def test_summary_without_a_plan_adds_no_plan_section(tmp_path: Path) -> None:
    provider = Compactable([_tool_turn(90_000), _answer()])

    _run(provider, tmp_path)

    assert "update_plan" not in str(provider.history[0])


def test_turn_events_report_context_size_against_the_budget(tmp_path: Path) -> None:
    provider = Compactable([_tool_turn(90_000), _answer()])

    events, _, _ = _run(provider, tmp_path)

    turns = [payload for kind, payload in events if kind == "model.turn.completed"]
    assert turns[0]["context_tokens"] == 90_000
    assert turns[0]["context_budget"] == _BUDGET
    compacted = next(
        payload for kind, payload in events if kind == "model.context.compacted"
    )
    assert compacted["context_budget"] == _BUDGET


def test_turn_events_omit_context_size_without_a_budget(tmp_path: Path) -> None:
    provider = Compactable([_answer()], budget=None)

    events, _, _ = _run(provider, tmp_path)

    turns = [payload for kind, payload in events if kind == "model.turn.completed"]
    assert "context_tokens" not in turns[0]
    assert "context_budget" not in turns[0]
