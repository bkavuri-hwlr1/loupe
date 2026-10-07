"""Exploration helpers answer read-only questions without widening authority."""

from __future__ import annotations

import copy
import io
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from llm_cli.agent.driver import RunRequest
from llm_cli.agent.explorer import EXPLORER_TOOLS, Explorer
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.limits import ExecutionLimits
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import TaskCancelled, ToolBroker
from llm_cli.cli.render import EventRenderer, render_event
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.providers.base import (
    ModelTurn,
    ToolCallRequest,
    ToolCallResult,
    context_overflow_error,
)

_USAGE = {"input_tokens": 10, "output_tokens": 2}
Step = ModelTurn | Exception | Callable[[], ModelTurn]


def _calls(*calls: tuple[str, dict[str, object]]) -> ModelTurn:
    return ModelTurn(
        text="",
        tool_calls=tuple(
            ToolCallRequest(f"call-{index}", name, arguments)
            for index, (name, arguments) in enumerate(calls)
        ),
        stop_reason="tool_use",
        usage=_USAGE,
    )


def _text(text: str) -> ModelTurn:
    return ModelTurn(text=text, usage=_USAGE)


class Session:
    def __init__(self, provider: Provider, system: str, tools: object) -> None:
        self.provider = provider
        self.system = system
        self.tools = [str(tool["name"]) for tool in tools]  # type: ignore[attr-defined]
        self.helper = system.startswith("You are a read-only exploration helper")
        self.steps: Any = None
        self.sent: list[object] = []

    def snapshot(self) -> dict[str, object]:
        return {"messages": []}

    def _next(self) -> ModelTurn:
        step = next(self.steps)
        if isinstance(step, Exception):
            raise step
        return step() if callable(step) else step

    def send_user(self, text: str) -> ModelTurn:
        self.sent.append(text)
        if self.helper:
            self.steps = iter(self.provider.helpers[text])
        else:
            self.steps = self.provider.main
        return self._next()

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        self.sent.append(tuple(results))
        if self.steps is None:
            self.steps = self.provider.main
        return self._next()

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        self.sent.append(tuple(results))


class Provider:
    name = "script"
    model = "script"

    def __init__(
        self, main: Sequence[Step], helpers: Mapping[str, Sequence[Step]] | None = None
    ) -> None:
        self.main = iter(main)
        self.helpers = dict(helpers or {})
        self.sessions: list[Session] = []
        self._lock = threading.Lock()

    def session(self, *, system: str, tools: object, state: object = None) -> Session:
        session = Session(self, system, tools)
        with self._lock:
            self.sessions.append(session)
        return session

    def helper_sessions(self) -> list[Session]:
        return [session for session in self.sessions if session.helper]

    def main_session(self) -> Session:
        return next(session for session in self.sessions if not session.helper)


class CappedProvider(Provider):
    """A provider that can open helper sessions at a lower effort."""

    def __init__(self, *args: Any, capped: str | None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.capped = capped
        self.ceilings: list[str] = []
        self.efforts: list[str | None] = []

    def capped_effort(self, ceiling: str) -> str | None:
        self.ceilings.append(ceiling)
        return self.capped

    def session(  # type: ignore[override]
        self,
        *,
        system: str,
        tools: object,
        state: object = None,
        effort: str | None = None,
    ) -> Session:
        self.efforts.append(effort)
        return super().session(system=system, tools=tools, state=state)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "docs/guide.md").write_text("original guide\n")
    (root / "src/app.py").write_text("print('hi')\n")
    run_git(root, ["init", "-b", "main"])
    run_git(root, ["add", "."])
    return root


def _shared(checkout: Path, **kwargs: Any) -> tuple[SharedToolBroker, list[tuple]]:
    events: list[tuple[str, dict[str, object]]] = []
    broker = SharedToolBroker(
        worktree=checkout,
        scopes=("docs/",),
        on_event=lambda kind, payload: events.append((kind, payload)),
        **kwargs,
    )
    return broker, events


def _request(checkout: Path, **kwargs: Any) -> RunRequest:
    return RunRequest(
        "task-explore", 1, "inspect", ("docs/",), checkout, "base", **kwargs
    )


def test_helper_reads_pending_edits_and_its_report_returns_to_the_agent(
    checkout: Path,
) -> None:
    broker, events = _shared(checkout)
    assert not broker.invoke("read_file", {"path": "docs/guide.md"}).is_error
    staged = broker.invoke(
        "apply_patch",
        {"path": "docs/guide.md", "old_text": "original", "new_text": "edited"},
    )
    assert not staged.is_error
    task = "What does docs/guide.md say, and what prints in src/app.py?"
    label = "Guide and app output"
    provider = Provider(
        [
            _calls(("explore", {"description": label, "task": task})),
            _text("The guide was edited."),
        ],
        {
            task: [
                _calls(
                    ("read_file", {"path": "docs/guide.md"}),
                    ("search_text", {"pattern": "print"}),
                ),
                _text("The guide says 'edited guide'. src/app.py:1 prints hi."),
            ]
        },
    )

    result = CodingAgentHarness(provider).run(_request(checkout), broker)

    assert result.answer == "The guide was edited."
    (helper,) = provider.helper_sessions()
    assert helper.tools == list(EXPLORER_TOOLS)
    assert helper.sent[0] == task
    read, search = helper.sent[1]  # type: ignore[misc]
    assert read.content == "edited guide\n"
    assert "src/app.py:1: print('hi')" in search.content
    report = provider.main_session().sent[1][0]  # type: ignore[index]
    assert not report.is_error
    assert report.content.startswith("Exploration report (2 tool calls).")
    assert "src/app.py:1 prints hi." in report.content
    # Main turns, helper turns: two each. The helpers' share is kept separately.
    assert result.usage == {
        "input_tokens": 40,
        "output_tokens": 8,
        "explore_output_tokens": 4,
    }
    # explore itself plus the helper's two calls.
    assert result.tool_calls == broker.usage.calls == 5
    kinds = [kind for kind, _ in events]
    assert kinds.index("explore.started") < kinds.index("explore.finished")
    finished = next(payload for kind, payload in events if kind == "explore.finished")
    assert finished["state"] == "completed"
    assert finished["tool_calls"] == 2
    assert finished["task"] == task
    assert finished["label"] == label
    # The helper's view never reaches the parent's own tool events.
    assert [p["tool"] for k, p in events if k == "tool.called"] == [
        "read_file",
        "apply_patch",
        "explore",
    ]


def test_helper_reads_do_not_authorize_the_agents_writes(checkout: Path) -> None:
    (checkout / "docs/other.md").write_text("other\n")
    broker, _ = _shared(checkout)
    task = "Read docs/other.md"
    provider = Provider(
        [_calls(("explore", {"task": task})), _text("done")],
        {task: [_calls(("read_file", {"path": "docs/other.md"})), _text("other")]},
    )
    CodingAgentHarness(provider).run(_request(checkout), broker)

    denied = broker.invoke("write_file", {"path": "docs/other.md", "content": "new\n"})

    assert denied.is_error
    assert broker.candidates() == ()


def test_helpers_cannot_use_tools_beyond_reading(checkout: Path) -> None:
    broker, _ = _shared(checkout)
    task = "Try to edit"
    provider = Provider(
        [_calls(("explore", {"task": task})), _text("done")],
        {
            task: [
                _calls(
                    ("write_file", {"path": "docs/guide.md", "content": "x"}),
                    ("explore", {"task": "recurse"}),
                ),
                _text("I could not edit."),
            ]
        },
    )
    CodingAgentHarness(provider).run(_request(checkout), broker)

    (helper,) = provider.helper_sessions()
    write, recurse = helper.sent[1]  # type: ignore[misc]
    assert write.is_error and "unknown tool 'write_file'" in write.content
    assert recurse.is_error and "unknown tool 'explore'" in recurse.content
    assert (checkout / "docs/guide.md").read_text() == "original guide\n"
    assert broker.candidates() == ()


def _explorer(
    tmp_path: Path, helper: Sequence[Step], **kwargs: Any
) -> tuple[Explorer, ToolBroker, Provider, list[tuple]]:
    events: list[tuple[str, dict[str, object]]] = []
    broker = ToolBroker(
        tmp_path,
        ("*",),
        on_event=lambda kind, payload: events.append((kind, payload)),
        **kwargs,
    )
    provider = Provider([], {"question": helper})
    explorer = Explorer(provider, broker)
    broker.explorer = explorer
    return explorer, broker, provider, events


def test_a_spent_budget_asks_for_the_report_and_then_stops(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\n")
    explorer, broker, provider, _ = _explorer(
        tmp_path,
        [_calls(("list_files", {}))] * 41 + [_text("Partial findings.")],
    )

    outcome = explorer("question")

    assert not outcome.is_error
    assert outcome.content.startswith("Exploration report (40 tool calls).")
    (helper,) = provider.helper_sessions()
    assert "tool budget is used up" in helper.sent[-1][0].content  # type: ignore[index]
    assert broker.usage.calls == 40

    stubborn, broker, _, events = _explorer(tmp_path, [_calls(("list_files", {}))] * 42)
    outcome = stubborn("question")
    assert outcome.is_error
    assert "kept calling tools past its budget" in outcome.content
    assert events[-1][1]["state"] == "incomplete"


def test_no_helper_starts_without_budget_for_the_agent_to_continue(
    tmp_path: Path,
) -> None:
    explorer, broker, provider, events = _explorer(
        tmp_path,
        [_text("unused")],
        limits=ExecutionLimits(max_tool_calls=10),
    )
    broker.usage.calls = 5

    outcome = explorer("question")

    assert outcome.is_error and "Too little" in outcome.content
    assert provider.sessions == []
    assert events == []
    assert broker.usage.calls == 5


def test_a_small_budget_limits_the_helper_and_unused_calls_return(
    tmp_path: Path,
) -> None:
    explorer, broker, _, events = _explorer(
        tmp_path,
        [_calls(("list_files", {})), _text("Only one call needed.")],
        limits=ExecutionLimits(max_tool_calls=20),
    )
    broker.usage.calls = 10

    assert not explorer("question").is_error
    assert broker.usage.calls == 11
    assert events[-1][1]["tool_calls"] == 1


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (context_overflow_error(400), "ran out of context; ask a narrower question"),
        (
            LlmCoordError(ErrorCode.PROVIDER_UNAVAILABLE, "PRIVATE_PROVIDER_TEXT"),
            "after 0 tool calls: the model request failed.]",
        ),
        # A rejected request would fail the same way again, so it is not retried.
        (
            LlmCoordError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "the model provider returned HTTP 400",
                details={"status_code": 400},
            ),
            "the model provider returned HTTP 400",
        ),
    ],
)
def test_helper_failures_become_tool_errors_not_task_failures(
    tmp_path: Path, failure: Exception, message: str
) -> None:
    explorer, broker, _, events = _explorer(tmp_path, [failure])

    outcome = explorer("question")

    assert outcome.is_error and message in outcome.content
    # Only fixed descriptions, never provider text, reach notes and events.
    assert "PRIVATE" not in outcome.content and "PRIVATE" not in str(events)
    assert events[-1][1]["state"] == "failed"
    assert broker.usage.calls == 0


def test_a_helper_that_fails_midway_is_charged_for_its_calls(tmp_path: Path) -> None:
    explorer, broker, _, events = _explorer(
        tmp_path,
        [
            _calls(("list_files", {}), ("list_files", {})),
            LlmCoordError(ErrorCode.PROVIDER_UNAVAILABLE, "connection reset"),
        ],
    )

    outcome = explorer("question")

    assert outcome.is_error
    assert "stopped early after 2 tool calls" in outcome.content
    assert broker.usage.calls == 2
    assert events[-1][1]["tool_calls"] == 2


_DROPPED = LlmCoordError(
    ErrorCode.PROVIDER_UNAVAILABLE,
    "the model provider could not be reached",
    details={"provider_error": "connection"},
)


def _retrying(
    tmp_path: Path, helper: Sequence[Step]
) -> tuple[Explorer, list[float], list[tuple]]:
    events: list[tuple[str, dict[str, object]]] = []
    broker = ToolBroker(
        tmp_path,
        ("*",),
        on_event=lambda kind, payload: events.append((kind, payload)),
    )
    sleeps: list[float] = []
    explorer = Explorer(Provider([], {"question": helper}), broker, sleep=sleeps.append)
    broker.explorer = explorer
    return explorer, sleeps, events


def test_helpers_retry_failures_in_transit(tmp_path: Path) -> None:
    overloaded = LlmCoordError(
        ErrorCode.PROVIDER_UNAVAILABLE,
        "the model provider returned HTTP 503",
        details={"status_code": 503},
    )
    explorer, sleeps, events = _retrying(
        tmp_path,
        [_calls(("list_files", {})), _DROPPED, overloaded, _text("The report.")],
    )

    outcome = explorer("question")

    assert not outcome.is_error and "The report." in outcome.content
    assert sleeps == [2.0, 6.0]
    finished = events[-1][1]
    assert finished["state"] == "completed"
    assert finished["retries"] == 2
    assert finished["reason"] is None


def test_a_helper_that_keeps_failing_names_what_it_read(checkout: Path) -> None:
    explorer, sleeps, events = _retrying(
        checkout,
        [
            _calls(
                (
                    "read_file",
                    {"path": "docs/guide.md", "start_line": 1, "end_line": 1},
                ),
                ("search_text", {"pattern": "print"}),
                ("read_file", {"path": "missing.md"}),
            ),
            _DROPPED,
            _DROPPED,
            _DROPPED,
        ],
    )

    outcome = explorer("question")

    assert outcome.is_error
    assert sleeps == [2.0, 6.0]
    reason = "the connection to the model provider failed (after 2 retries)"
    # A failed read is not something it looked at.
    assert outcome.content == (
        f"[The exploration stopped early after 3 tool calls: {reason}. Before "
        "stopping it had looked at: docs/guide.md:1-1, search for 'print'.]"
    )
    assert events[-1][1]["reason"] == reason


def test_incomplete_and_refused_reports_are_marked(tmp_path: Path) -> None:
    explorer, _, _, _ = _explorer(
        tmp_path, [ModelTurn(text="Half a rep", stop_reason="max_tokens")]
    )
    outcome = explorer("question")
    assert not outcome.is_error
    assert "Half a rep" in outcome.content
    assert "its report was cut off. The report may be incomplete." in outcome.content

    refusing, _, _, _ = _explorer(tmp_path, [ModelTurn(text="", stop_reason="refusal")])
    outcome = refusing("question")
    assert outcome.is_error and "declined" in outcome.content


def test_cancellation_stops_the_helper_and_the_task(tmp_path: Path) -> None:
    stopped = threading.Event()

    def stop_after_reading() -> ModelTurn:
        stopped.set()
        return _calls(("list_files", {}))

    explorer, broker, _, events = _explorer(
        tmp_path, [stop_after_reading, _text("never")], cancelled=stopped.is_set
    )

    with pytest.raises(TaskCancelled):
        explorer("question")
    assert events[-1][1]["state"] == "cancelled"
    assert broker.usage.calls == 0


def test_helpers_use_the_configured_lower_effort(checkout: Path) -> None:
    broker, events = _shared(checkout)
    provider = CappedProvider(
        [_calls(("explore", {"task": "question"})), _text("done")],
        {"question": [_text("report")]},
        capped="low",
    )

    CodingAgentHarness(provider, explore_effort="medium").run(
        _request(checkout), broker
    )

    assert provider.ceilings == ["medium"]
    # The main session keeps the task's effort; the helper uses the cap.
    assert provider.efforts == [None, "low"]
    started = next(payload for kind, payload in events if kind == "explore.started")
    assert started["effort"] == "low"


def test_helpers_keep_the_tasks_effort_without_a_lower_one(tmp_path: Path) -> None:
    unchanged = CappedProvider([], {"question": [_text("report")]}, capped=None)
    broker = ToolBroker(tmp_path, ("*",))
    explorer = Explorer(unchanged, broker, effort_ceiling="low")
    broker.explorer = explorer
    assert not explorer("question").is_error
    assert unchanged.efforts == [None]

    # A provider without the capability, or no setting, uses the task's effort.
    plain, _, _, events = _explorer(tmp_path, [_text("report")])
    assert not plain("question").is_error
    assert events[0][1]["effort"] is None
    capped = CappedProvider([], {"question": [_text("report")]}, capped="low")
    assert not Explorer(capped, ToolBroker(tmp_path, ("*",)))("question").is_error
    assert capped.ceilings == [] and capped.efforts == [None]


def test_explore_validates_its_task(tmp_path: Path) -> None:
    _, broker, provider, events = _explorer(tmp_path, [_text("report")])

    assert broker.invoke("explore", {"task": "  "}).is_error
    assert broker.invoke("explore", {"task": "x" * 4_001}).is_error
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    assert "secret" in broker.invoke("explore", {"task": secret}).content
    refused = broker.invoke("explore", {"task": "question", "description": secret})
    assert "secret" in refused.content
    assert provider.sessions == []

    # The label only names the exploration, so an overlong one is shortened.
    long = broker.invoke("explore", {"task": "question", "description": "y " * 60})
    assert not long.is_error
    started = next(payload for kind, payload in events if kind == "explore.started")
    assert started["label"] == ("y " * 40).rstrip() + "…"


def test_explore_calls_in_one_turn_run_in_parallel(checkout: Path) -> None:
    broker, events = _shared(checkout)
    barrier = threading.Barrier(2, timeout=5)

    def meet(report: str) -> Callable[[], ModelTurn]:
        def wait() -> ModelTurn:
            barrier.wait()
            return _text(report)

        return wait

    provider = Provider(
        [
            _calls(
                ("explore", {"task": "first"}),
                ("explore", {"task": "second"}),
                ("list_files", {}),
            ),
            _text("Both answered."),
        ],
        {"first": [meet("first report")], "second": [meet("second report")]},
    )

    result = CodingAgentHarness(provider).run(_request(checkout), broker)

    assert result.answer == "Both answered."
    first, second, listing = provider.main_session().sent[1]  # type: ignore[misc]
    assert "first report" in first.content
    assert "second report" in second.content
    assert "docs/" in listing.content
    calls = [p["call_id"] for k, p in events if k == "model.tool_call"]
    outputs = [p["call_id"] for k, p in events if k == "model.tool_result"]
    assert calls == outputs == ["call-0", "call-1", "call-2"]


def test_an_interrupted_exploration_runs_again_after_a_restart(
    checkout: Path,
) -> None:
    checkpoints: list[dict[str, object]] = []

    def save_and_interrupt(state: Mapping[str, object]) -> None:
        saved = copy.deepcopy(dict(state))
        checkpoints.append(saved)
        if saved["phase"] == "tools" and saved["in_flight_call"] == 0:
            raise KeyboardInterrupt

    first = Provider([_calls(("explore", {"task": "question"}))])
    with pytest.raises(KeyboardInterrupt):
        CodingAgentHarness(first).run(
            _request(checkout, checkpoint=save_and_interrupt), _shared(checkout)[0]
        )
    assert first.helper_sessions() == []

    resumed = Provider([_text("Answered.")], {"question": [_text("The report.")]})
    broker, _ = _shared(checkout)
    result = CodingAgentHarness(resumed).run(
        _request(
            checkout,
            resume_state=checkpoints[-1],
            checkpoint=lambda state: checkpoints.append(copy.deepcopy(dict(state))),
        ),
        broker,
    )

    assert result.answer == "Answered."
    (report,) = resumed.main_session().sent[0]  # type: ignore[misc]
    assert "The report." in report.content
    assert broker.usage.calls == 1


def test_explore_is_offered_in_every_mode_unless_disabled(checkout: Path) -> None:
    for mode in ("auto", "normal", "plan"):
        provider = Provider([_text("answer")])
        CodingAgentHarness(provider).run(
            _request(checkout, agent_mode=mode), _shared(checkout, agent_mode=mode)[0]
        )
        session = provider.main_session()
        assert "explore" in session.tools
        assert "Exploration helpers" in session.system

    disabled = Provider([_text("answer")])
    CodingAgentHarness(disabled, explorations=False).run(
        _request(checkout), _shared(checkout)[0]
    )
    assert "explore" not in disabled.main_session().tools
    assert "Exploration helpers" not in disabled.main_session().system


def _event(kind: str, **payload: object) -> dict[str, object]:
    return {"event_type": kind, "task_id": "task", "payload": payload}


def test_explorations_are_shown_while_running_and_when_finished() -> None:
    stream = io.StringIO()
    renderer = EventRenderer(stream, plain=True)
    activity: list[str | None] = []
    renderer.ui.activity = activity.append  # type: ignore[method-assign,assignment]

    renderer.render(_event("explore.started", exploration_id="a", task="Find callers"))
    assert activity[-1] == "Exploring: Find callers…"
    renderer.render(_event("explore.started", exploration_id="b", task="Trace config"))
    assert activity[-1] == "Exploring 2 questions in parallel…"
    renderer.render(
        _event(
            "explore.finished",
            exploration_id="a",
            task="Find callers",
            state="completed",
            tool_calls=7,
            seconds=12.34,
        )
    )
    assert activity[-1] == "Exploring: Trace config…"
    renderer.render(
        _event(
            "explore.finished",
            exploration_id="b",
            task="Trace config",
            state="incomplete",
            tool_calls=1,
            seconds=3,
        )
    )
    assert activity[-1] == "Thinking…"
    assert stream.getvalue() == (
        "  ↳ Explored: Find callers · 7 tool calls, 12.3s\n"
        "  ! Exploration stopped early: Trace config · 1 tool call, 3.0s\n"
    )
    assert render_event(_event("explore.started", task="Find \x1b[2Jcallers")) == (
        "  Exploring: Find callers"
    )
    assert render_event(
        _event(
            "explore.finished",
            task="Trace config",
            state="failed",
            tool_calls=40,
            seconds=76.3,
            reason="the model request failed: the model provider could not be reached",
        )
    ) == (
        "  ! Exploration stopped early: Trace config · 40 tool calls, 76.3s "
        "(the model request failed: the model provider could not be reached)"
    )


def test_explorations_are_named_by_their_label_or_a_short_task() -> None:
    stream = io.StringIO()
    renderer = EventRenderer(stream, plain=True)
    activity: list[str | None] = []
    renderer.ui.activity = activity.append  # type: ignore[method-assign,assignment]
    task = "Trace how the daemon recovers in-flight tasks after a restart. " * 3

    renderer.render(
        _event("explore.started", exploration_id="a", task=task, label="Task recovery")
    )
    assert activity[-1] == "Exploring: Task recovery…"
    renderer.render(_event("explore.finished", exploration_id="a", task=task, label=""))
    renderer.render(_event("explore.started", exploration_id="b", task=task))
    clipped = task[:59].rstrip() + "…"
    assert activity[-1] == f"Exploring: {clipped}"
    assert len(clipped) == 60
    assert stream.getvalue() == f"  ! Exploration stopped early: {clipped}\n"
