"""The run_command tool validates arguments and is offered only when allowed."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_cli.agent.driver import RunRequest
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.tools import ToolBroker, ToolOutcome, tool_schemas
from llm_cli.cli.render import EventRenderer, render_event
from llm_cli.providers.base import ModelTurn, ToolCallResult


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str, int]] = []

    def __call__(self, argv: list[str], cwd: str, timeout: int) -> ToolOutcome:
        self.calls.append((argv, cwd, timeout))
        return ToolOutcome("exit 0 after 0.1s\nok", False)


def _broker(
    tmp_path: Path,
    *,
    mode: str = "auto",
    approval: str = "allow",
    answers: list[str] | None = None,
) -> tuple[ToolBroker, Recorder]:
    recorder = Recorder()
    questions: list[str] = []

    def asker(question: str) -> str:
        questions.append(question)
        assert answers, "an unexpected approval question was asked"
        return answers.pop(0)

    broker = ToolBroker(
        tmp_path,
        ("*",),
        agent_mode=mode,
        asker=asker if answers is not None else None,
    )
    broker.command_runner = recorder
    broker.command_approval = approval
    recorder.questions = questions  # type: ignore[attr-defined]
    return broker, recorder


def test_run_command_is_offered_only_with_a_runner_outside_plan_mode(
    tmp_path: Path,
) -> None:
    assert "run_command" not in ToolBroker(tmp_path, ("*",)).tool_names()
    assert "run_command" in _broker(tmp_path)[0].tool_names()
    assert "run_command" in _broker(tmp_path, mode="normal")[0].tool_names()
    assert "run_command" not in _broker(tmp_path, mode="plan")[0].tool_names()


def test_schema_requires_argv() -> None:
    (schema,) = tool_schemas(["run_command"])

    assert schema["input_schema"]["required"] == ["argv"]  # type: ignore[index]
    assert "sandboxed" in str(schema["description"])


def test_valid_arguments_reach_the_runner_with_defaults(tmp_path: Path) -> None:
    broker, recorder = _broker(tmp_path)

    result = broker.invoke("run_command", {"argv": ["pytest", "-q"]})
    broker.invoke("run_command", {"argv": ["ls"], "cwd": "src", "timeout": 600})

    assert not result.is_error and "ok" in result.content
    assert recorder.calls == [(["pytest", "-q"], ".", 120), (["ls"], "src", 600)]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"argv": []},
        {"argv": "pytest -q"},
        {"argv": [""]},
        {"argv": ["echo", 3]},
        {"argv": ["echo", "a\0b"]},
        {"argv": ["x"] * 101},
        {"argv": ["echo", "x" * 70_000]},
        {"argv": ["ls"], "timeout": 0},
        {"argv": ["ls"], "timeout": 601},
        {"argv": ["ls"], "timeout": "60"},
        {"argv": ["ls"], "cwd": 3},
    ],
)
def test_invalid_arguments_are_refused_before_running(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    broker, recorder = _broker(tmp_path)

    result = broker.invoke("run_command", arguments)

    assert result.is_error
    assert recorder.calls == []


def test_plan_mode_refuses_a_command_call(tmp_path: Path) -> None:
    broker, recorder = _broker(tmp_path, mode="plan")

    result = broker.invoke("run_command", {"argv": ["ls"]})

    assert result.is_error and "Plan mode" in result.content
    assert recorder.calls == []


class Script:
    name = "script"
    model = "script"

    def __init__(self) -> None:
        self.systems: list[str] = []

    def session(self, *, system: str, tools: object, state: object = None) -> Script:
        self.systems.append(system)
        return self

    def snapshot(self) -> dict[str, object]:
        return {"messages": []}

    def send_user(self, text: str) -> ModelTurn:
        return ModelTurn(text="answer")

    def send_tool_results(self, results: list[ToolCallResult]) -> ModelTurn:
        raise AssertionError("no tools expected")

    def record_tool_results(self, results: list[ToolCallResult]) -> None:
        pass


def test_system_prompt_explains_commands_only_when_offered(tmp_path: Path) -> None:
    request = RunRequest("task", 1, "explain", ("*",), tmp_path, "base")
    with_commands = Script()
    CodingAgentHarness(with_commands).run(request, _broker(tmp_path)[0])
    without = Script()
    CodingAgentHarness(without).run(request, ToolBroker(tmp_path, ("*",)))

    assert "run_command runs argv" in with_commands.systems[0]
    assert "run_command" not in without.systems[0]


def test_commands_are_shown_in_the_conversation(tmp_path: Path) -> None:
    import io

    stream = io.StringIO()
    renderer = EventRenderer(stream, plain=True)
    renderer.render(
        {
            "event_type": "command.started",
            "task_id": "t",
            "payload": {"argv": ["pytest", "-q", "tests/a b.py"]},
        }
    )
    renderer.render(
        {
            "event_type": "command.output",
            "task_id": "t",
            "payload": {"text": "raw output stays out of the conversation\n"},
        }
    )
    renderer.render(
        {
            "event_type": "command.finished",
            "task_id": "t",
            "payload": {"state": "completed", "exit_code": 1, "duration": 3.21},
        }
    )

    text = stream.getvalue()
    assert "$ pytest -q 'tests/a b.py' · exit 1 (3.2s)" in text
    assert "raw output" not in text
    assert (
        render_event(
            {
                "event_type": "command.finished",
                "payload": {"state": "timed_out", "duration": 120.0},
            }
        )
        == "  Command result: timed out after 120.0s"
    )


def test_approval_needs_an_interactive_session(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, approval="ask")

    assert "run_command" not in broker.tool_names()


def test_allow_once_asks_again_for_the_next_command(tmp_path: Path) -> None:
    broker, recorder = _broker(tmp_path, approval="ask", answers=["1", "y"])

    first = broker.invoke("run_command", {"argv": ["pytest", "-q"], "cwd": "src"})
    second = broker.invoke("run_command", {"argv": ["ls"]})

    assert not first.is_error and not second.is_error
    assert [call[0] for call in recorder.calls] == [["pytest", "-q"], ["ls"]]
    questions = recorder.questions  # type: ignore[attr-defined]
    assert len(questions) == 2
    assert "$ pytest -q\nin src (timeout 120s)" in questions[0]
    assert "no network" in questions[0]


def test_allowing_the_task_stops_further_questions(tmp_path: Path) -> None:
    broker, recorder = _broker(tmp_path, approval="ask", answers=["2"])

    broker.invoke("run_command", {"argv": ["pytest"]})
    broker.invoke("run_command", {"argv": ["ls"]})

    assert len(recorder.calls) == 2
    assert len(recorder.questions) == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ("3", "the user declined this command"),
        ("no", "the user declined this command"),
        ("use make test instead", "declined this command: use make test instead"),
    ],
)
def test_a_declined_command_never_runs(
    tmp_path: Path, answer: str, message: str
) -> None:
    broker, recorder = _broker(tmp_path, approval="ask", answers=[answer])

    result = broker.invoke("run_command", {"argv": ["rm", "-rf", "build"]})

    assert result.is_error and message in result.content
    assert recorder.calls == []
    assert broker.usage.denied == 1


def test_approval_shows_hidden_characters_escaped(tmp_path: Path) -> None:
    broker, recorder = _broker(tmp_path, approval="ask", answers=["3"])

    broker.invoke(
        "run_command",
        {"argv": ["echo", "safe\n\n1. Allow once‮"], "cwd": "src"},
    )

    (question,) = recorder.questions  # type: ignore[attr-defined]
    shown = question.split("$ ", 1)[1].split(" (timeout", 1)[0]
    assert "\n" not in shown.split("\nin ", 1)[0]
    assert "\\u000a\\u000a1. Allow once\\u202e" in shown
