"""Unanswered clarifications survive harness checkpoints and daemon restarts."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from test_agent_loop import ScriptedProvider, _call, _tools

from llm_cli.agent.driver import RunRequest
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.limits import ExecutionLimits
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import ToolBroker


class InterruptedQuestion(BaseException):
    """Stop a worker without running its normal error-settlement path."""


def _request(root: Path, *, shared: bool) -> RunRequest:
    return RunRequest(
        task_id="question-task",
        attempt=1,
        instructions="Update the guide after clarifying the preferred wording.",
        scopes=("guide.md",),
        worktree=root,
        base_oid="0" * 40,
        workspace_mode="shared" if shared else "isolated",
    )


@pytest.mark.parametrize("shared", [False, True], ids=["isolated", "shared"])
def test_unanswered_question_replays_once_per_restart_without_extra_budget(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    shared: bool,
) -> None:
    root = repository_factory(tmp_path, {"guide.md": "old\n"})
    checkpoints: list[dict[str, object]] = []
    asked: list[str] = []
    broker_type = SharedToolBroker if shared else ToolBroker
    limits = ExecutionLimits(max_tool_calls=4)

    def save(state: Mapping[str, object]) -> None:
        checkpoints.append(copy.deepcopy(dict(state)))

    def interrupted_ask(question: str) -> str:
        asked.append(question)
        raise InterruptedQuestion

    def broker(asker: Callable[[str], str]) -> ToolBroker:
        return broker_type(
            worktree=root, scopes=("guide.md",), asker=asker, limits=limits
        )

    first = ScriptedProvider(
        [
            _tools(
                _call("read_file", path="guide.md"),
                _call("write_file", path="guide.md", content="proposed\n"),
                _call("ask_user", question="Use concise or detailed wording?"),
            )
        ]
    )
    request = replace(_request(root, shared=shared), checkpoint=save)
    with pytest.raises(InterruptedQuestion):
        CodingAgentHarness(first, limits=limits).run(request, broker(interrupted_ask))
    assert checkpoints[-1]["in_flight_call"] == 2
    assert len(asked) == 1
    before_restart = copy.deepcopy(checkpoints[-1])

    # A second daemon also stops while the operator is answering. Each restart
    # must present the pending question exactly once, preserving its call ID.
    with pytest.raises(InterruptedQuestion):
        CodingAgentHarness(ScriptedProvider([]), limits=limits).run(
            replace(request, resume_state=before_restart), broker(interrupted_ask)
        )
    assert asked == ["Use concise or detailed wording?"] * 2
    assert checkpoints[-1]["tool_results"] == before_restart["tool_results"]
    assert checkpoints[-1]["tool_usage"] == before_restart["tool_usage"]

    def answer(question: str) -> str:
        asked.append(question)
        return "Use concise wording."

    restored_broker = broker(answer)
    restored = ScriptedProvider(
        [_tools(_call("finish_task", answer="clarified", summary="clarified"))]
    )
    result = CodingAgentHarness(restored, limits=limits).run(
        replace(request, resume_state=checkpoints[-1]), restored_broker
    )
    assert asked == ["Use concise or detailed wording?"] * 3
    assert result.summary == "clarified"
    assert result.tool_calls == 4  # Read, write, question, finish; no restart penalty.
    assert restored_broker.usage.files_read == 1
    assert len(restored.sent_results) == 1
    answer_result = restored.sent_results[0][2]
    assert answer_result.call_id == "call_ask_user"
    assert (
        not answer_result.is_error and answer_result.content == "Use concise wording."
    )
    assert checkpoints[-1]["phase"] == "finished"
    assert (root / "guide.md").read_text() == ("old\n" if shared else "proposed\n")
    if shared:
        assert isinstance(restored_broker, SharedToolBroker)
        assert restored_broker.candidates()[0].content == b"proposed\n"


@pytest.mark.parametrize("shared", [False, True], ids=["isolated", "shared"])
def test_checkpointed_answer_is_delivered_without_reasking(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    shared: bool,
) -> None:
    root = repository_factory(tmp_path, {"guide.md": "old\n"})
    checkpoints: list[dict[str, object]] = []
    asked: list[str] = []
    broker_type = SharedToolBroker if shared else ToolBroker

    def save_and_interrupt(state: Mapping[str, object]) -> None:
        checkpoints.append(copy.deepcopy(dict(state)))
        if state["phase"] == "tools" and state["tool_results"]:
            raise InterruptedQuestion

    def answer(question: str) -> str:
        asked.append(question)
        return "Keep the existing wording."

    first = ScriptedProvider(
        [_tools(_call("ask_user", question="Change the wording?"))]
    )
    request = replace(_request(root, shared=shared), checkpoint=save_and_interrupt)
    with pytest.raises(InterruptedQuestion):
        CodingAgentHarness(first).run(
            request, broker_type(worktree=root, scopes=("guide.md",), asker=answer)
        )
    assert checkpoints[-1]["in_flight_call"] is None

    def unexpected_question(question: str) -> str:
        pytest.fail(f"A durably answered question was reasked: {question}")

    resumed = ScriptedProvider(
        [_tools(_call("finish_task", answer="kept wording", summary="kept wording"))]
    )
    result = CodingAgentHarness(resumed).run(
        replace(request, resume_state=checkpoints[-1], checkpoint=None),
        broker_type(worktree=root, scopes=("guide.md",), asker=unexpected_question),
    )
    assert asked == ["Change the wording?"]
    assert result.tool_calls == 2
    assert resumed.sent_results[0][0].content == "Keep the existing wording."
    assert not resumed.sent_results[0][0].is_error
    assert (root / "guide.md").read_text() == "old\n"


@pytest.mark.parametrize("corruption", ["wrong-index", "past-end", "wrong-call-id"])
def test_question_replay_rejects_inconsistent_checkpoint(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    corruption: str,
) -> None:
    root = repository_factory(tmp_path, {"guide.md": "old\n"})
    checkpoints: list[dict[str, object]] = []

    def save(state: Mapping[str, object]) -> None:
        checkpoints.append(copy.deepcopy(dict(state)))

    def interrupt(question: str) -> str:
        raise InterruptedQuestion

    provider = ScriptedProvider(
        [
            _tools(
                _call("read_file", path="guide.md"), _call("ask_user", question="Why?")
            )
        ]
    )
    request = replace(_request(root, shared=True), checkpoint=save)
    with pytest.raises(InterruptedQuestion):
        CodingAgentHarness(provider).run(
            request, SharedToolBroker(root, ("guide.md",), asker=interrupt)
        )
    saved = checkpoints[-1]
    if corruption == "wrong-index":
        saved["in_flight_call"] = 0
    elif corruption == "past-end":
        saved["in_flight_call"] = 2
    else:
        completed = saved["tool_results"]
        assert isinstance(completed, list)
        completed[0]["call_id"] = "not-the-original-call"

    def unexpected_question(question: str) -> str:
        pytest.fail(f"Malformed checkpoint replayed a question: {question}")

    with pytest.raises(ValueError, match=r"saved (in-flight tool call|tool result)"):
        CodingAgentHarness(ScriptedProvider([])).run(
            replace(request, resume_state=saved, checkpoint=None),
            SharedToolBroker(root, ("guide.md",), asker=unexpected_question),
        )
