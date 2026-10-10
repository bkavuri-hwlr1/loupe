"""An edit task's answer is finished from what settlement actually did."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from test_shared_agent_execution import (
    ScriptedProvider,
    _call,
    _finish,
    _tools,
)
from test_verified_workflow import await_task, edit_provider, setup

from llm_cli.daemon.service import DaemonService
from llm_cli.providers.base import ModelTurn, ToolCallResult

DRAFT = "Changed value to 2 and published it."


def _answer(service: DaemonService) -> dict[str, Any]:
    finished = [
        event.payload
        for event in service.store.list_task_events("task")
        if event.event_type == "model.finished"
    ]
    assert finished, "the task delivered no answer"
    parts = sorted(finished, key=lambda payload: payload.get("part", 0))
    return {
        "text": "".join(str(payload["answer"]) for payload in parts),
        "outcome": parts[-1]["outcome"],
        "count": len({payload.get("message_id") for payload in parts}),
    }


def _finalization(service: DaemonService) -> dict[str, Any]:
    execution = service.store.get_execution("task", 1)
    assert execution is not None
    saved = service.store.get_execution_checkpoint(execution.execution_id)
    assert saved is not None
    state = saved.checkpoint["finalization"]
    assert isinstance(state, dict)
    return state


def _final_turn(text: str) -> Callable[..., ModelTurn]:
    """The settlement turn: a plain answer, with no tool results to read."""

    def step(results: Sequence[ToolCallResult]) -> ModelTurn:
        assert not results
        return ModelTurn(text=text, stop_reason="end_turn")

    return step


class _RecordingProvider(ScriptedProvider):
    """Record each user message, including the settlement request."""

    def __init__(self, model: str, steps: Sequence[Any]) -> None:
        super().__init__(model, steps)
        self.user_messages: list[str] = []
        self.sessions: list[tuple[str, ...]] = []

    def session(self, **kwargs: Any) -> ScriptedProvider:
        self.sessions.append(tuple(str(tool["name"]) for tool in kwargs["tools"]))
        return super().session(**kwargs)

    def send_user(self, text: str) -> ModelTurn:
        self.user_messages.append(text)
        return super().send_user(text)


def _edit(*steps: Any) -> _RecordingProvider:
    return _RecordingProvider(
        "workflow-session",
        [
            _tools(
                _call("read_file", path="a.py"),
                _call("write_file", path="a.py", content="value = 2\n"),
            ),
            *steps,
        ],
    )


def test_an_expected_hold_keeps_the_draft_without_another_model_call(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        provider = edit_provider(_finish(DRAFT))
        creds = await setup(service, root, provider, review=True)
        await await_task(service, root, creds)

        assert service._task_view("task")["state"] == "awaiting_review"
        answer = _answer(service)
        assert answer["text"] == DRAFT and answer["outcome"] == "completed"
        state = _finalization(service)
        assert (state["status"], state["attempts"]) == ("completed", 0)
        assert state["facts"]["publication"] == "held_for_review"
        assert state["facts"]["changed_paths"] == ["a.py"]
        # The settled task joins the conversation the next prompt sees; a new
        # session has none until a finished task is promoted.
        assert service.store.session_conversation("workflow-session") is not None

    asyncio.run(scenario())


def test_an_expected_publication_keeps_the_draft(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        creds = await setup(service, root, edit_provider(_finish(DRAFT)))
        await await_task(service, root, creds)

        assert (root / "a.py").read_text() == "value = 2\n"
        assert _answer(service)["text"] == DRAFT
        state = _finalization(service)
        assert (state["status"], state["attempts"]) == ("completed", 0)
        assert state["facts"]["publication"] == "published"

    asyncio.run(scenario())


def _stale_check(root: Path) -> str:
    # Passes in its snapshot, then changes the real checkout, so the evidence
    # is stale by the time auto mode would publish.
    target = json.dumps(str(root / "dependency.py"))
    return f"import pathlib; pathlib.Path({target}).write_text('changed\\n')"


def test_a_stale_check_in_auto_mode_rewrites_the_answer_once(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(
            tmp_path, {"a.py": "value = 1\n", "dependency.py": "base\n"}
        )
        service = service_factory(tmp_path)
        settled = "The change to a.py is held for review: its check is stale."
        provider = _edit(_finish(DRAFT), _final_turn(settled))
        creds = await setup(service, root, provider, code=_stale_check(root))
        await await_task(service, root, creds)

        assert service._task_view("task")["state"] == "awaiting_review"
        assert (root / "a.py").read_text() == "value = 1\n"
        answer = _answer(service)
        assert answer == {"text": settled, "outcome": "completed", "count": 1}
        # One tools-disabled turn, given the runner's facts as data.
        assert provider.sessions[-1] == ()
        facts = json.loads(provider.user_messages[-1].split("\n\n", 1)[1])
        assert facts["settlement"]["publication"] == "held_for_review"
        assert facts["settlement"]["verification"] == "stale"
        assert facts["provisional_draft"] == DRAFT
        state = _finalization(service)
        assert (state["status"], state["attempts"]) == ("completed", 1)
        events = [e.event_type for e in service.store.list_task_events("task")]
        assert events.index("model.finalizing") < events.index("model.finished")
        assert events.index("model.finished") < events.index("workflow.awaiting_review")

    asyncio.run(scenario())


def test_a_diverged_checkout_rewrites_the_answer_before_the_failure(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)

        def finish(results: Sequence[ToolCallResult]) -> ModelTurn:
            # Someone edits the file after the agent read it.
            (root / "a.py").write_text("value = 9\n")
            return _finish(DRAFT)

        settled = "a.py changed underneath this task, so nothing was published."
        provider = _edit(finish, _final_turn(settled))
        creds = await setup(service, root, provider)
        await await_task(service, root, creds)

        assert (root / "a.py").read_text() == "value = 9\n"
        assert _answer(service)["text"] == settled
        assert _finalization(service)["facts"]["publication"] == "diverged"
        execution = service.store.get_execution("task", 1)
        assert execution is not None
        assert execution.failure_code == "PATH_BASE_MISMATCH"

    asyncio.run(scenario())


def test_a_failed_final_turn_states_the_settled_facts(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(
            tmp_path, {"a.py": "value = 1\n", "dependency.py": "base\n"}
        )
        service = service_factory(tmp_path)

        def broken(results: Sequence[ToolCallResult]) -> ModelTurn:
            raise RuntimeError("the provider dropped the final response")

        provider = _edit(_finish(DRAFT), broken)
        creds = await setup(service, root, provider, code=_stale_check(root))
        await await_task(service, root, creds)

        # Settlement still completes, and the answer claims only its facts.
        assert service._task_view("task")["state"] == "awaiting_review"
        answer = _answer(service)
        assert answer["outcome"] == "partial"
        assert "retained for review and were not published" in answer["text"]
        assert DRAFT not in answer["text"]
        state = _finalization(service)
        assert (state["status"], state["attempts"]) == ("interrupted", 1)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("publication", "publish_mode", "surprising"),
    [
        ("held_for_review", "review", False),
        ("held_for_review", "auto", True),
        ("published", "auto", False),
        ("no_changes", "auto", False),
        ("diverged", "auto", True),
        ("operator_attention", "auto", True),
    ],
)
def test_only_a_surprising_settlement_needs_a_model_turn(
    publication: str, publish_mode: str, surprising: bool
) -> None:
    from llm_cli.agent.finalization import SettlementFacts, settlement_surprises

    facts = SettlementFacts(
        publication=publication, verification="passed", completion="completed"
    )
    assert settlement_surprises(facts, publish_mode=publish_mode) is surprising
    stopped = SettlementFacts(
        publication=publication, verification="passed", completion="cancelled"
    )
    assert settlement_surprises(stopped, publish_mode=publish_mode) is False
