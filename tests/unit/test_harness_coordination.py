"""Model-boundary context preserves real tool outcomes and durable replay."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from llm_cli.agent.driver import CoordinationUpdate, RunRequest
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.limits import ExecutionLimits
from llm_cli.agent.tools import ToolBroker
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult


class Script:
    name = "script"
    model = "script"

    def __init__(self, turns: Sequence[ModelTurn]) -> None:
        self.turns = iter(turns)
        self.history: list[object] = []
        self.calls: list[object] = []

    def session(self, *, system: str, tools: object, state: object = None) -> Script:
        if isinstance(state, dict):
            self.history = copy.deepcopy(state["messages"])
        return self

    def snapshot(self) -> dict[str, object]:
        return {"messages": copy.deepcopy(self.history)}

    def send_user(self, text: str) -> ModelTurn:
        self.history.append(text)
        self.calls.append(text)
        return next(self.turns)

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        self.calls.append(tuple(results))
        self.history.append([result.content for result in results])
        return next(self.turns)

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        self.history.append([result.content for result in results])


def _turn(*calls: tuple[str, str, dict[str, object]]) -> ModelTurn:
    return ModelTurn(
        text="",
        tool_calls=tuple(ToolCallRequest(*call) for call in calls),
        stop_reason="tool_use",
    )


def _finish() -> ModelTurn:
    return _turn(("done", "finish_task", {"answer": "done", "summary": "done"}))


def _request(tmp_path: Path) -> RunRequest:
    return RunRequest("task-context", 1, "inspect", ("docs/",), tmp_path, "base")


def test_every_model_boundary_refreshes_and_only_last_result_gets_context(
    tmp_path: Path,
) -> None:
    provider = Script(
        [
            ModelTurn(text="", stop_reason="end_turn"),
            _turn(("a", "unknown", {}), ("b", "unknown", {})),
            _finish(),
        ]
    )
    cursors: list[int | None] = []
    checkpoints: list[dict[str, object]] = []

    def refresh(after: int | None) -> CoordinationUpdate:
        cursors.append(after)
        return CoordinationUpdate(len(cursors), f"context-{len(cursors)}")

    CodingAgentHarness(provider).run(
        replace(
            _request(tmp_path),
            refresh_coordination=refresh,
            checkpoint=lambda state: checkpoints.append(copy.deepcopy(dict(state))),
        ),
        ToolBroker(tmp_path, ("docs/",)),
    )

    assert cursors == [None, 1, 2]
    assert "context-1" in provider.calls[0]
    assert "context-2" in provider.calls[1]
    results = provider.calls[2]
    assert isinstance(results, tuple)
    assert [result.call_id for result in results] == ["a", "b"]
    assert all(result.is_error for result in results)
    assert "context-3" not in results[0].content
    assert "context-3" in results[1].content
    assert checkpoints[-1]["coordination_sequence"] == 3
    assert all(
        "context-" not in result["content"]
        for checkpoint in checkpoints
        for result in checkpoint["tool_results"]
    )


def test_context_overflow_does_not_send_or_ack_and_resume_refreshes_original_results(
    tmp_path: Path,
) -> None:
    checkpoints: list[dict[str, object]] = []
    first = Script([_turn(("a", "unknown", {}))])

    def overflow(after: int | None) -> CoordinationUpdate:
        if after is None:
            return CoordinationUpdate(4, "initial")
        raise LlmCoordError(ErrorCode.CONTEXT_TOO_LARGE, "too many changes")

    request = replace(
        _request(tmp_path),
        refresh_coordination=overflow,
        checkpoint=lambda state: checkpoints.append(copy.deepcopy(dict(state))),
    )
    with pytest.raises(LlmCoordError, match="too many changes"):
        CodingAgentHarness(first).run(request, ToolBroker(tmp_path, ("docs/",)))

    saved = checkpoints[-1]
    assert saved["phase"] == "tool_results"
    assert saved["coordination_sequence"] == 4
    assert len(first.calls) == 1
    retried_cursors: list[int | None] = []

    def current(after: int | None) -> CoordinationUpdate:
        retried_cursors.append(after)
        return CoordinationUpdate(6, "intervening changes")

    resumed = Script([_finish()])
    CodingAgentHarness(resumed).run(
        replace(request, resume_state=saved, refresh_coordination=current),
        ToolBroker(tmp_path, ("docs/",)),
    )
    assert retried_cursors == [4]
    assert len(resumed.calls) == 1
    sent = resumed.calls[0]
    assert isinstance(sent, tuple)
    assert len(sent) == 1
    assert sent[0].call_id == "a"
    assert sent[0].is_error
    assert sent[0].content.count("[Coordination update:") == 1
    assert "intervening changes" in sent[0].content
    assert "[Coordination update:" not in saved["tool_results"][0]["content"]
    assert checkpoints[-1]["coordination_sequence"] == 6


def test_context_and_multibyte_tool_output_share_the_existing_byte_budget(
    tmp_path: Path,
    git_run: Callable[..., str],
) -> None:
    git_run(tmp_path, "init", "-q")
    (tmp_path / "large.txt").write_text("é" * 512, encoding="utf-8")
    limits = ExecutionLimits(max_tool_output_bytes=512)
    provider = Script([_turn(("read", "read_file", {"path": "large.txt"})), _finish()])
    checkpoints: list[dict[str, object]] = []
    CodingAgentHarness(provider, limits=limits).run(
        replace(
            _request(tmp_path),
            refresh_coordination=lambda after: CoordinationUpdate(
                (after or 0) + 1, "new facts"
            ),
            checkpoint=lambda state: checkpoints.append(copy.deepcopy(dict(state))),
        ),
        ToolBroker(tmp_path, ("docs/",), limits=limits),
    )
    sent = provider.calls[-1]
    assert isinstance(sent, tuple)
    assert not sent[0].is_error
    assert len(sent[0].content.encode("utf-8")) <= 512
    assert "truncated to fit coordination update" in sent[0].content
    assert sent[0].content.endswith("new facts\n[End coordination update]")
    original = next(
        saved["tool_results"][0]["content"]
        for saved in checkpoints
        if saved["phase"] == "tool_results"
    )
    assert "new facts" not in original
    visible, metadata = original.split("\n[output truncated; metadata=", 1)
    assert visible and set(visible) == {"é"}
    assert len(original.encode("utf-8")) <= 512
    assert '"next_start_column":' in metadata


@pytest.mark.parametrize(
    "update",
    [
        CoordinationUpdate(-1, "bad"),
        CoordinationUpdate(True, "bad"),
        CoordinationUpdate(1, None),
        CoordinationUpdate(1, "x" * (16 * 1024 + 1)),
    ],
)
def test_invalid_or_oversized_context_is_refused_before_provider_mutation(
    tmp_path: Path, update: CoordinationUpdate
) -> None:
    provider = Script([_finish()])
    with pytest.raises((ValueError, LlmCoordError)):
        CodingAgentHarness(provider).run(
            replace(_request(tmp_path), refresh_coordination=lambda _: update),
            ToolBroker(tmp_path, ("docs/",)),
        )
    assert provider.history == []
    assert provider.calls == []


def test_prior_conversation_cursor_is_reused_for_the_next_prompt(
    tmp_path: Path,
) -> None:
    provider = Script([_finish()])
    cursors: list[int | None] = []

    def refresh(after: int | None) -> CoordinationUpdate:
        cursors.append(after)
        return CoordinationUpdate(9, "new prompt snapshot")

    conversation: Mapping[str, object] = {
        "version": 1,
        "provider": "script",
        "model": "script",
        "session": {"messages": ["prior history"]},
        "coordination_sequence": 7,
    }
    CodingAgentHarness(provider).run(
        replace(
            _request(tmp_path),
            conversation_state=conversation,
            refresh_coordination=refresh,
        ),
        ToolBroker(tmp_path, ("docs/",)),
    )
    assert cursors == [7]
    assert provider.history[0] == "prior history"
