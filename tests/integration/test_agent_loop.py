"""The supervised agent loop, driven by a scripted model.

No network and no API key: the provider seam is a protocol, so a fake session
returning a fixed script exercises exactly the code paths a real model would.
What is being tested is the loop's behavior -- when it stops, what it refuses,
what it records -- not the model's judgement.
"""

from __future__ import annotations

import copy
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from llm_cli.agent.driver import RunRequest
from llm_cli.agent.harness import (
    CodingAgentHarness,
    _answer_message_id,
    _result_to_checkpoint,
    _turn_to_checkpoint,
)
from llm_cli.agent.limits import ExecutionLimits
from llm_cli.agent.tools import ToolBroker, ToolOutcome
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.worktrees import create_managed_worktree
from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )
    return result.stdout.strip()


def _call(name: str, **arguments: object) -> ToolCallRequest:
    return ToolCallRequest(call_id=f"call_{name}", name=name, arguments=arguments)


def _tools(*calls: ToolCallRequest) -> ModelTurn:
    return ModelTurn(text="", tool_calls=calls, stop_reason="tool_use")


class ScriptedProvider:
    """Return a fixed sequence of turns, recording what it was asked."""

    name = "scripted"
    model = "scripted-model"

    def __init__(self, turns: Sequence[ModelTurn]) -> None:
        self.turns = list(turns)
        self.sent_results: list[tuple[ToolCallResult, ...]] = []
        self.recorded_results: list[tuple[ToolCallResult, ...]] = []
        self.user_messages: list[str] = []
        self.offered_tools: tuple[str, ...] = ()
        self.system: str = ""
        self.restored_state: Mapping[str, object] | None = None
        self.history: list[dict[str, object]] = []

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> ScriptedProvider:
        self.system = system
        self.offered_tools = tuple(str(tool["name"]) for tool in tools)
        self.restored_state = state
        if state is not None:
            raw_history = state.get("messages", [])
            assert isinstance(raw_history, list)
            self.history = copy.deepcopy(raw_history)
        return self

    def snapshot(self) -> Mapping[str, object]:
        return {"messages": copy.deepcopy(self.history)}

    def _next(self) -> ModelTurn:
        if not self.turns:
            return ModelTurn(text="(script exhausted)", stop_reason="end_turn")
        return self.turns.pop(0)

    def send_user(self, text: str) -> ModelTurn:
        self.user_messages.append(text)
        self.history.append({"role": "user", "content": text})
        return self._next()

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        self.sent_results.append(tuple(results))
        self.history.append({"role": "tool", "results": len(results)})
        return self._next()

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        self.recorded_results.append(tuple(results))
        self.history.append({"role": "tool", "results": len(results)})


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    (repository / "docs").mkdir(parents=True)
    (repository / "src").mkdir()
    (repository / "docs" / "guide.md").write_text("old\n", encoding="utf-8")
    (repository / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "base")
    managed = create_managed_worktree(
        repository,
        managed_root=tmp_path / "managed",
        task_id="task-agent",
        base_oid=_git(repository, "rev-parse", "HEAD"),
    )
    return managed.path


def _broker(
    worktree: Path, *, events: list[tuple[str, dict[str, object]]]
) -> ToolBroker:
    return ToolBroker(
        worktree=worktree,
        scopes=("docs/",),
        on_event=lambda kind, payload: events.append((kind, payload)),
    )


def _request(worktree: Path) -> RunRequest:
    return RunRequest(
        task_id="task-agent",
        attempt=1,
        instructions="update the guide",
        scopes=("docs/",),
        worktree=worktree,
        base_oid="0" * 40,
    )


def test_the_loop_runs_tools_and_stops_when_the_model_finishes(
    worktree: Path,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    provider = ScriptedProvider(
        [
            _tools(_call("read_file", path="docs/guide.md")),
            _tools(_call("write_file", path="docs/guide.md", content="new\n")),
            _tools(_call("validate_changes")),
            _tools(
                _call(
                    "finish_task",
                    answer="rewrote the guide",
                    summary="rewrote the guide",
                )
            ),
        ]
    )
    broker = _broker(worktree, events=events)

    result = CodingAgentHarness(provider).run(_request(worktree), broker)

    assert result.summary == "rewrote the guide"
    assert result.tool_calls == 4
    assert (worktree / "docs" / "guide.md").read_text(encoding="utf-8") == "new\n"
    # The opening message must tell the model what it is allowed to write.
    assert "docs/" in provider.user_messages[0]
    assert "update the guide" in provider.user_messages[0]
    assert [kind for kind, _ in events].count("tool.called") == 4
    finished = [payload for kind, payload in events if kind == "model.finished"]
    assert len(finished) == 1
    assert finished[0]["summary"] == "rewrote the guide"
    assert finished[0]["answer"] == "rewrote the guide"
    assert finished[0]["outcome"] == "completed"
    assert finished[0]["finished_cleanly"] is True
    assert finished[0]["message_id"] == _answer_message_id("task-agent", 1)


@pytest.mark.parametrize("phase", ["finished", "tools"])
def test_version_one_finish_recovers_without_inventing_an_answer(
    worktree: Path, phase: str
) -> None:
    provider = ScriptedProvider([])
    broker = _broker(worktree, events=[])
    legacy_usage = broker.usage_snapshot()
    legacy_usage.update(finished=True, summary="Previous task summary.")
    legacy_usage.pop("answer")
    legacy_usage.pop("outcome")
    legacy_usage.pop("partial_reads")
    saved = {
        "version": 1,
        "provider": provider.name,
        "model": provider.model,
        "session": {"messages": []},
        "deadline_at": 9_999_999_999,
        "phase": phase,
        "turn": (
            _turn_to_checkpoint(
                _tools(_call("finish_task", summary="Previous task summary."))
            )
            if phase == "tools"
            else None
        ),
        "tool_results": (
            [
                _result_to_checkpoint(
                    ToolCallResult(
                        call_id="call_finish_task",
                        content="Previous task summary.",
                        is_error=False,
                    )
                )
            ]
            if phase == "tools"
            else []
        ),
        "in_flight_call": None,
        "tool_usage": legacy_usage,
        "usage_total": {},
        "idle_turns": 0,
        "final_summary": "Previous task summary.",
    }

    result = CodingAgentHarness(provider).run(
        replace(_request(worktree), resume_state=saved), broker
    )

    assert result.summary == "Previous task summary."
    assert result.answer == ""
    assert result.outcome == "partial"
    assert provider.user_messages == []


def test_a_denied_write_is_returned_to_the_model_rather_than_ending_the_run(
    worktree: Path,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    provider = ScriptedProvider(
        [
            _tools(_call("write_file", path="src/app.py", content="clobbered\n")),
            _tools(_call("write_file", path="docs/guide.md", content="corrected\n")),
            _tools(
                _call(
                    "finish_task",
                    answer="stayed inside the claim",
                    summary="stayed inside the claim",
                )
            ),
        ]
    )
    broker = _broker(worktree, events=events)

    result = CodingAgentHarness(provider).run(_request(worktree), broker)

    # The refusal came back as a tool result the model could act on ...
    denial = provider.sent_results[0][0]
    assert denial.is_error
    assert "outside this task's claimed scopes" in denial.content
    # ... the run continued, and the out-of-scope file was never touched.
    assert result.summary == "stayed inside the claim"
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert (worktree / "docs" / "guide.md").read_text(encoding="utf-8") == "corrected\n"


def test_parallel_tool_calls_are_answered_in_one_batch(worktree: Path) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    provider = ScriptedProvider(
        [
            _tools(
                _call("read_file", path="docs/guide.md"),
                _call("read_file", path="src/app.py"),
            ),
            _tools(_call("finish_task", answer="read both", summary="read both")),
        ]
    )

    CodingAgentHarness(provider).run(
        _request(worktree), _broker(worktree, events=events)
    )

    # Splitting a parallel batch across turns teaches a model to stop making
    # parallel calls, so both results must arrive together.
    assert len(provider.sent_results) == 1
    assert len(provider.sent_results[0]) == 2


def test_finish_is_refused_before_dispatch_when_session_cannot_record_result(
    worktree: Path,
) -> None:
    class LegacyProvider:
        name = "legacy"
        model = "legacy-model"

        def session(self, **kwargs: object) -> LegacyProvider:
            return self

        def snapshot(self) -> Mapping[str, object]:
            return {}

        def send_user(self, text: str) -> ModelTurn:
            return _tools(
                _call("write_file", path="docs/guide.md", content="must not write\n"),
                _call("finish_task", answer="done", summary="done"),
            )

        def send_tool_results(
            self, results: Sequence[ToolCallResult]
        ) -> ModelTurn:
            raise AssertionError("the terminal batch must not reach the model")

    broker = _broker(worktree, events=[])
    with pytest.raises(LlmCoordError) as raised:
        CodingAgentHarness(LegacyProvider()).run(_request(worktree), broker)

    assert raised.value.code == ErrorCode.PROTOCOL_MISMATCH
    assert "record terminal tool results" in raised.value.message
    assert broker.usage.calls == 0
    assert not broker.usage.finished
    assert (worktree / "docs/guide.md").read_text(encoding="utf-8") == "old\n"


def test_a_complete_no_tool_answer_needs_no_finish_call_or_extra_request(
    worktree: Path,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    provider = ScriptedProvider(
        [
            ModelTurn(text="I think I am done.", stop_reason="end_turn"),
            ModelTurn(text="Still done.", stop_reason="end_turn"),
        ]
    )

    result = CodingAgentHarness(provider).run(
        _request(worktree), _broker(worktree, events=events)
    )

    assert len(provider.user_messages) == 1
    assert not any(kind == "model.stalled" for kind, _ in events)
    assert result.answer == "I think I am done."
    assert result.summary == ""
    assert result.outcome == "completed"
    assert result.tool_calls == 0


def test_empty_turns_have_a_bounded_nudge_and_partial_outcome(worktree: Path) -> None:
    provider = ScriptedProvider([ModelTurn(text=""), ModelTurn(text="  ")])
    result = CodingAgentHarness(provider).run(
        _request(worktree), _broker(worktree, events=[])
    )
    assert len(provider.user_messages) == 2
    assert result.outcome == "partial"
    assert not result.answer


@pytest.mark.parametrize("finish_with_tool", [False, True])
def test_summary_only_finish_requests_the_missing_answer_before_completion(
    worktree: Path,
    finish_with_tool: bool,
) -> None:
    snapshots: list[Mapping[str, object]] = []
    answer = (
        "## Security finding\n\n"
        "A symlink to Git metadata can expose private repository information.\n\n"
        "Reject resolved metadata paths before opening them, and add a regression test."
    )
    provider = ScriptedProvider(
        [
            _tools(_call("finish_task", summary="Prepared security review.")),
            (
                _tools(_call("finish_task", answer=answer, summary="Reviewed."))
                if finish_with_tool
                else ModelTurn(text=answer)
            ),
        ]
    )
    events: list[tuple[str, dict[str, object]]] = []
    result = CodingAgentHarness(provider).run(
        replace(_request(worktree), checkpoint=lambda saved: snapshots.append(saved)),
        _broker(worktree, events=events),
    )
    assert provider.sent_results[0][0].is_error
    assert "answer" in provider.sent_results[0][0].content
    assert result.answer == answer
    assert result.outcome == "completed"
    accepted = snapshots[-1]["accepted_answer"]
    assert isinstance(accepted, dict) and accepted["text"] == answer
    finished = [payload for kind, payload in events if kind == "model.finished"]
    assert len(finished) == 1 and finished[0]["answer"] == answer


@pytest.mark.parametrize("reason", ["max_tokens", "pause_turn", "unknown"])
def test_nonfinal_stop_reason_never_completes_a_partial_answer(
    worktree: Path, reason: str
) -> None:
    provider = ScriptedProvider(
        [ModelTurn(text="Unfinished response", stop_reason=reason)]
    )
    result = CodingAgentHarness(provider).run(
        _request(worktree), _broker(worktree, events=[])
    )
    assert result.outcome == "partial"
    assert result.answer == "Unfinished response"
    assert len(provider.user_messages) == 1


def test_native_answer_cannot_bypass_required_completion_checks(worktree: Path) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    broker = _broker(worktree, events=events)
    gates: list[bool] = []

    def gate() -> ToolOutcome:
        gates.append(True)
        return ToolOutcome("Required check failed.", is_error=True)

    broker.finish_gate = gate
    provider = ScriptedProvider(
        [ModelTurn(text="Ready."), ModelTurn(text="Still ready.")]
    )
    result = CodingAgentHarness(provider).run(_request(worktree), broker)
    assert len(gates) == 2
    assert result.outcome == "blocked"
    assert not broker.usage.finished
    assert "Required check failed" in provider.user_messages[1]


def test_token_limited_response_cannot_execute_returned_tool_calls(
    worktree: Path,
) -> None:
    broker = _broker(worktree, events=[])
    provider = ScriptedProvider(
        [
            ModelTurn(
                text="Partial response",
                tool_calls=(
                    _call("write_file", path="docs/guide.md", content="unsafe\n"),
                ),
                stop_reason="max_tokens",
            )
        ]
    )
    with pytest.raises(LlmCoordError, match="did not complete"):
        CodingAgentHarness(provider).run(_request(worktree), broker)
    assert (worktree / "docs" / "guide.md").read_text() == "old\n"
    assert broker.usage.calls == 0


def test_completion_denial_can_be_reported_as_blocked_without_check_loop(
    worktree: Path,
) -> None:
    broker = _broker(worktree, events=[])
    broker.finish_gate = lambda: ToolOutcome("Tests failed.", is_error=True)
    provider = ScriptedProvider(
        [
            ModelTurn(text="Ready."),
            _tools(
                _call(
                    "finish_task",
                    answer="Tests are failing; edits retained.",
                    outcome="blocked",
                )
            ),
        ]
    )
    result = CodingAgentHarness(provider).run(_request(worktree), broker)
    assert result.outcome == "blocked"
    assert result.answer == "Tests are failing; edits retained."


def test_full_answer_is_separate_from_report_and_survives_finished_recovery(
    worktree: Path,
) -> None:
    answer = "## Repository overview\n\n" + "Detailed explanation. " * 400
    snapshots: list[Mapping[str, object]] = []
    events: list[tuple[str, dict[str, object]]] = []
    provider = ScriptedProvider(
        [_tools(_call("finish_task", answer=answer, summary="Inspected repository."))]
    )
    request = replace(
        _request(worktree),
        checkpoint=lambda state: snapshots.append(copy.deepcopy(state)),
    )
    result = CodingAgentHarness(provider).run(request, _broker(worktree, events=events))
    assert result.answer == answer
    assert result.summary == "Inspected repository."
    emitted = "".join(
        str(payload["answer"]) for kind, payload in events if kind == "model.finished"
    )
    assert emitted == answer
    saved = snapshots[-1]
    assert saved["version"] == 2
    assert saved["accepted_answer"] == {
        "version": 1,
        "answer_id": _answer_message_id("task-agent", 1),
        "text": answer,
        "summary": "Inspected repository.",
        "outcome": "completed",
    }
    restored_provider = ScriptedProvider([])
    restored = CodingAgentHarness(restored_provider).run(
        replace(_request(worktree), resume_state=saved), _broker(worktree, events=[])
    )
    assert restored.answer == answer and restored.outcome == "completed"
    assert restored_provider.user_messages == []


def test_atomic_final_checkpoint_owns_completion_event(worktree: Path) -> None:
    checkpoints: list[Mapping[str, object]] = []
    events: list[tuple[str, dict[str, object]]] = []

    def persist(state: Mapping[str, object]) -> bool:
        checkpoints.append(copy.deepcopy(state))
        return state["phase"] == "finished"

    result = CodingAgentHarness(
        ScriptedProvider([ModelTurn(text="The actual answer.")])
    ).run(
        replace(_request(worktree), checkpoint=persist),
        _broker(worktree, events=events),
    )
    assert result.answer == "The actual answer."
    assert checkpoints[-1]["phase"] == "finished"
    assert not any(kind == "model.finished" for kind, _ in events)


def test_a_refusal_stops_the_run_without_publishing(worktree: Path) -> None:
    provider = ScriptedProvider(
        [ModelTurn(text="", stop_reason="refusal", refusal_category="cyber")]
    )

    with pytest.raises(LlmCoordError) as failure:
        CodingAgentHarness(provider).run(
            _request(worktree), _broker(worktree, events=[])
        )

    assert failure.value.code is ErrorCode.PROVIDER_AMBIGUOUS
    assert (failure.value.details or {})["refusal_category"] == "cyber"


def test_the_wall_clock_budget_ends_a_run_that_will_not_stop(
    worktree: Path,
) -> None:
    provider = ScriptedProvider(
        [_tools(_call("list_files", path=".")) for _ in range(50)]
    )
    ticks = iter(range(0, 10_000, 60))
    driver = CodingAgentHarness(
        provider,
        limits=ExecutionLimits(wall_clock_seconds=120),
        clock=lambda: float(next(ticks)),
    )

    with pytest.raises(LlmCoordError) as failure:
        driver.run(_request(worktree), _broker(worktree, events=[]))

    assert failure.value.code is ErrorCode.PROVIDER_UNAVAILABLE
    assert "wall-clock" in failure.value.message


def test_the_tool_budget_ends_a_run_that_will_not_stop(worktree: Path) -> None:
    provider = ScriptedProvider(
        [_tools(_call("list_files", path=".")) for _ in range(50)]
    )
    broker = ToolBroker(
        worktree=worktree, scopes=("docs/",), limits=ExecutionLimits(max_tool_calls=3)
    )

    with pytest.raises(LlmCoordError) as failure:
        CodingAgentHarness(provider).run(_request(worktree), broker)

    assert failure.value.code is ErrorCode.PROVIDER_UNAVAILABLE
    assert "tool-call budget" in failure.value.message


def test_ask_user_is_not_offered_to_a_background_run(worktree: Path) -> None:
    provider = ScriptedProvider(
        [_tools(_call("finish_task", answer="done", summary="done"))]
    )

    CodingAgentHarness(provider).run(_request(worktree), _broker(worktree, events=[]))

    assert "ask_user" not in provider.offered_tools
    assert "write_file" in provider.offered_tools
    assert "finish_task" in provider.offered_tools
    assert "ask_user" not in provider.system


@pytest.mark.parametrize("workspace_mode", ["shared", "isolated"])
def test_interactive_model_uses_clarification_and_continues_same_task(
    worktree: Path, workspace_mode: str
) -> None:
    provider = ScriptedProvider(
        [
            _tools(
                _call(
                    "ask_user",
                    question="Who is the guide for?",
                    options=["Users", "Developers"],
                )
            ),
            _tools(_call("write_file", path="docs/guide.md", content="User guide\n")),
            _tools(
                _call(
                    "finish_task",
                    answer="Updated for users",
                    summary="Updated for users",
                )
            ),
        ]
    )
    broker = ToolBroker(worktree=worktree, scopes=("docs/",), asker=lambda _: "1")
    request = replace(_request(worktree), workspace_mode=workspace_mode)

    result = CodingAgentHarness(provider).run(request, broker)

    assert "ask_user" in provider.offered_tools
    assert "call\nask_user before doing work that depends on it" in provider.system
    assert "Do not ask for confirmation" in provider.system
    assert provider.sent_results[0][0].content == "Users"
    assert len(provider.user_messages) == 1
    assert result.summary == "Updated for users"
    assert (worktree / "docs/guide.md").read_text() == "User guide\n"


def test_a_durable_checkpoint_resumes_after_completed_tools_without_replaying_them(
    worktree: Path,
) -> None:
    checkpoints: list[dict[str, object]] = []

    def save_and_interrupt(state: Mapping[str, object]) -> None:
        saved = copy.deepcopy(dict(state))
        checkpoints.append(saved)
        results = saved["tool_results"]
        if (
            saved["phase"] == "tools"
            and isinstance(results, list)
            and len(results) == 1
            and saved["in_flight_call"] is None
        ):
            raise KeyboardInterrupt

    first = ScriptedProvider(
        [_tools(_call("write_file", path="docs/guide.md", content="new\n"))]
    )
    with pytest.raises(KeyboardInterrupt):
        CodingAgentHarness(first).run(
            RunRequest(
                task_id="task-agent",
                attempt=1,
                instructions="update the guide",
                scopes=("docs/",),
                worktree=worktree,
                base_oid="0" * 40,
                checkpoint=save_and_interrupt,
            ),
            _broker(worktree, events=[]),
        )

    # The first tool has already changed the worktree and its result was
    # checkpointed. A new daemon must continue with that result, not invoke the
    # write a second time (which would be unsafe for a non-idempotent patch).
    checkpoint = checkpoints[-1]
    resumed = ScriptedProvider(
        [_tools(_call("finish_task", answer="resumed", summary="resumed"))]
    )
    result = CodingAgentHarness(resumed).run(
        RunRequest(
            task_id="task-agent",
            attempt=1,
            instructions="update the guide",
            scopes=("docs/",),
            worktree=worktree,
            base_oid="0" * 40,
            resume_state=checkpoint,
            checkpoint=lambda state: checkpoints.append(copy.deepcopy(dict(state))),
        ),
        _broker(worktree, events=[]),
    )

    assert result.summary == "resumed"
    assert result.tool_calls == 2
    assert (worktree / "docs" / "guide.md").read_text(encoding="utf-8") == "new\n"
    assert resumed.restored_state is not None
    assert len(resumed.sent_results) == 1
    assert resumed.sent_results[0][0].content.startswith("wrote docs/guide.md")


def test_an_interrupted_tool_is_not_replayed_when_resuming(worktree: Path) -> None:
    checkpoints: list[dict[str, object]] = []

    def save_and_interrupt(state: Mapping[str, object]) -> None:
        saved = copy.deepcopy(dict(state))
        checkpoints.append(saved)
        if saved["phase"] == "tools" and saved["in_flight_call"] == 0:
            raise KeyboardInterrupt

    first = ScriptedProvider(
        [_tools(_call("write_file", path="docs/guide.md", content="new\n"))]
    )
    with pytest.raises(KeyboardInterrupt):
        CodingAgentHarness(first).run(
            RunRequest(
                task_id="task-agent",
                attempt=1,
                instructions="update the guide",
                scopes=("docs/",),
                worktree=worktree,
                base_oid="0" * 40,
                checkpoint=save_and_interrupt,
            ),
            _broker(worktree, events=[]),
        )

    resumed = ScriptedProvider(
        [_tools(_call("finish_task", answer="checked", summary="checked"))]
    )
    result = CodingAgentHarness(resumed).run(
        RunRequest(
            task_id="task-agent",
            attempt=1,
            instructions="update the guide",
            scopes=("docs/",),
            worktree=worktree,
            base_oid="0" * 40,
            resume_state=checkpoints[-1],
            checkpoint=lambda state: checkpoints.append(copy.deepcopy(dict(state))),
        ),
        _broker(worktree, events=[]),
    )

    assert result.summary == "checked"
    assert (worktree / "docs" / "guide.md").read_text(encoding="utf-8") == "old\n"
    assert resumed.sent_results[0][0].is_error
    assert "outcome is unknown" in resumed.sent_results[0][0].content


def test_restart_after_finish_answers_remaining_calls_and_saves_results_once(
    worktree: Path,
) -> None:
    class RecordingProvider(ScriptedProvider):
        def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
            self.sent_results.append(tuple(results))
            self.history.append(
                {
                    "role": "tool",
                    "results": [
                        {"call_id": result.call_id, "is_error": result.is_error}
                        for result in results
                    ],
                }
            )

    checkpoints: list[dict[str, object]] = []

    def interrupt_after_finish(state: Mapping[str, object]) -> None:
        checkpoints.append(copy.deepcopy(dict(state)))
        usage = state["tool_usage"]
        results = state["tool_results"]
        if (
            state["phase"] == "tools"
            and isinstance(usage, dict)
            and usage["finished"]
            and isinstance(results, list)
            and len(results) == 1
        ):
            raise KeyboardInterrupt

    first = RecordingProvider(
        [
            _tools(
                _call("finish_task", answer="done", summary="done"),
                _call("write_file", path="docs/guide.md", content="must not write\n"),
            )
        ]
    )
    with pytest.raises(KeyboardInterrupt):
        CodingAgentHarness(first).run(
            replace(_request(worktree), checkpoint=interrupt_after_finish),
            _broker(worktree, events=[]),
        )

    resumed = RecordingProvider([])
    result = CodingAgentHarness(resumed).run(
        replace(
            _request(worktree),
            resume_state=checkpoints[-1],
            checkpoint=lambda state: checkpoints.append(copy.deepcopy(dict(state))),
        ),
        _broker(worktree, events=[]),
    )

    assert result.summary == "done"
    assert (worktree / "docs/guide.md").read_text(encoding="utf-8") == "old\n"
    assert resumed.user_messages == []
    assert len(resumed.sent_results) == 1
    terminal_results = resumed.sent_results[0]
    assert [(item.call_id, item.is_error) for item in terminal_results] == [
        ("call_finish_task", False),
        ("call_write_file", True),
    ]
    assert "already finished" in terminal_results[1].content
    assert checkpoints[-1]["phase"] == "finished"
    assert checkpoints[-1]["session"] == resumed.snapshot()

    # A second restart from the final checkpoint neither requests a model turn
    # nor appends the terminal batch again.
    finished = RecordingProvider([])
    recovered = CodingAgentHarness(finished).run(
        replace(_request(worktree), resume_state=checkpoints[-1]),
        _broker(worktree, events=[]),
    )
    assert recovered == result
    assert finished.user_messages == []
    assert finished.sent_results == []
    assert finished.snapshot() == resumed.snapshot()
