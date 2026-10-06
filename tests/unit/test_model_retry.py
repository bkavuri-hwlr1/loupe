"""The main conversation sends a request again after a failure in transit."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from llm_cli.agent.driver import RunRequest
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.tools import TaskCancelled, ToolBroker
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.providers.base import ModelTurn, ToolCallResult

_DROPPED = LlmCoordError(
    ErrorCode.PROVIDER_UNAVAILABLE,
    "the connection to the model provider was lost",
    details={"provider_error": "connection"},
)


class Streaming:
    """A session that streams a draft, then returns or raises each step."""

    name = "script"
    model = "script"

    def __init__(self, steps: Sequence[ModelTurn | Exception]) -> None:
        self.steps = iter(steps)
        self.sent: list[object] = []

    def session(self, **kwargs: object) -> Streaming:
        return self

    def snapshot(self) -> dict[str, object]:
        return {}

    def set_event_callback(self, callback: Any) -> None:
        self.callback = callback

    def _next(self, payload: object) -> ModelTurn:
        self.sent.append(payload)
        self.callback("model.text.delta", {"text": "Draft ", "block_id": "1"})
        step = next(self.steps)
        if isinstance(step, Exception):
            raise step
        return step

    def send_user(self, text: str) -> ModelTurn:
        return self._next(text)

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        return self._next(tuple(results))

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        pass


def _run(
    tmp_path: Path,
    git_run: Callable[..., str],
    steps: Sequence[ModelTurn | Exception],
    *,
    sleep: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[Streaming, list[float], list[tuple[str, dict[str, object]]], Any]:
    git_run(tmp_path, "init", "-q")
    events: list[tuple[str, dict[str, object]]] = []
    broker = ToolBroker(
        tmp_path,
        ("*",),
        on_event=lambda kind, data: events.append((kind, data)),
        cancelled=cancelled,
    )
    provider = Streaming(steps)
    sleeps: list[float] = []
    harness = CodingAgentHarness(provider, sleep=sleep or sleeps.append)
    request = RunRequest("task", 1, "explain", ("*",), tmp_path, "base")
    try:
        outcome: Any = harness.run(request, broker)
    except BaseException as exc:
        outcome = exc
    return provider, sleeps, events, outcome


def _kinds(events: list[tuple[str, dict[str, object]]], kind: str) -> list[dict]:
    return [data for event, data in events if event == kind]


def test_a_request_lost_in_transit_is_sent_again(
    tmp_path: Path, git_run: Callable[..., str]
) -> None:
    provider, sleeps, events, result = _run(
        tmp_path, git_run, [_DROPPED, ModelTurn("The answer.")]
    )

    assert result.answer == "The answer."
    # The same prompt both times; the session kept its history unchanged.
    assert len(provider.sent) == 2 and provider.sent[0] == provider.sent[1]
    assert sum(sleeps) == pytest.approx(2.0)
    (retrying,) = _kinds(events, "model.retrying")
    assert retrying == {
        "reason": "the connection to the model provider failed",
        "retry": 1,
        "retries": 3,
        "delay": 2.0,
    }
    # The failed attempt's draft was only a preview, never a partial answer,
    # and the retry started a fresh turn.
    assert _kinds(events, "model.partial") == []
    assert len(_kinds(events, "model.turn.started")) == 2


def test_retries_stop_after_three_and_keep_the_last_draft(
    tmp_path: Path, git_run: Callable[..., str]
) -> None:
    provider, sleeps, events, result = _run(tmp_path, git_run, [_DROPPED] * 4)

    assert result is _DROPPED
    assert len(provider.sent) == 4
    assert sum(sleeps) == pytest.approx(2.0 + 6.0 + 15.0)
    assert [data["retry"] for data in _kinds(events, "model.retrying")] == [1, 2, 3]
    # The final failure is reported as before, with its interrupted text.
    assert [data["text"] for data in _kinds(events, "model.partial")] == ["Draft "]


def test_a_rejected_request_is_not_sent_again(
    tmp_path: Path, git_run: Callable[..., str]
) -> None:
    rejected = LlmCoordError(
        ErrorCode.PROVIDER_UNAVAILABLE,
        "the model provider returned HTTP 400",
        details={"status_code": 400},
    )
    provider, sleeps, events, result = _run(tmp_path, git_run, [rejected])

    assert result is rejected
    assert len(provider.sent) == 1
    assert sleeps == []
    assert _kinds(events, "model.retrying") == []


def test_cancelling_during_a_retry_wait_stops_the_task(
    tmp_path: Path, git_run: Callable[..., str]
) -> None:
    cancelled = [False]

    def sleep(seconds: float) -> None:
        cancelled[0] = True

    provider, _, _, result = _run(
        tmp_path,
        git_run,
        [_DROPPED, ModelTurn("never sent")],
        sleep=sleep,
        cancelled=lambda: cancelled[0],
    )

    assert isinstance(result, TaskCancelled)
    assert len(provider.sent) == 1
