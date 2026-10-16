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
    assert helper.tools == [*EXPLORER_TOOLS, "read_binary", "read_symlink"]
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


def test_calls_to_tools_a_helper_lacks_use_up_its_budget(tmp_path: Path) -> None:
    explorer, broker, provider, events = _explorer(
        tmp_path, [_calls(("write_file", {"path": "a.txt", "content": "x"}))] * 42
    )

    outcome = explorer("question")

    assert outcome.is_error
    assert "kept calling tools past its budget" in outcome.content
    (helper,) = provider.helper_sessions()
    refused = helper.sent[1][0]  # type: ignore[index]
    assert refused.is_error
    assert "can only use list_files, read_file, search_text, read_diff" in (
        refused.content
    )
    # The question, 40 refused calls, and the turn told its budget was spent.
    assert len(helper.sent) == 42
    assert broker.usage.calls == 40
    assert events[-1][1]["tool_calls"] == 40
    assert not (tmp_path / "a.txt").exists()


def test_a_helper_starts_no_request_after_the_deadline(tmp_path: Path) -> None:
    now = [100.0]

    def slow_listing() -> ModelTurn:
        now[0] = 200.0
        return _calls(("list_files", {}))

    events: list[tuple[str, dict[str, object]]] = []
    broker = ToolBroker(
        tmp_path,
        ("*",),
        on_event=lambda kind, payload: events.append((kind, payload)),
    )
    provider = Provider([], {"question": [slow_listing, _text("never sent")]})
    explorer = Explorer(provider, broker, deadline=150.0, clock=lambda: now[0])

    outcome = explorer("question")

    assert outcome.is_error
    assert outcome.content == (
        "[The exploration stopped early after 1 tool call: "
        "the task's time limit was reached.]"
    )
    (helper,) = provider.helper_sessions()
    assert len(helper.sent) == 1
    assert broker.usage.calls == 1
    assert events[-1][1]["state"] == "timed_out"


def test_helpers_share_the_tasks_deadline(checkout: Path) -> None:
    broker, events = _shared(checkout)
    now = [0.0]

    def slow_read() -> ModelTurn:
        now[0] = 1000.0
        return _calls(("read_file", {"path": "docs/guide.md"}))

    provider = Provider(
        [_calls(("explore", {"task": "question"}))],
        {"question": [slow_read, _text("never sent")]},
    )
    harness = CodingAgentHarness(
        provider,
        limits=ExecutionLimits(wall_clock_seconds=600),
        clock=lambda: now[0],
    )

    with pytest.raises(LlmCoordError, match="wall-clock"):
        harness.run(_request(checkout), broker)

    (helper,) = provider.helper_sessions()
    assert len(helper.sent) == 1
    finished = [payload for kind, payload in events if kind == "explore.finished"]
    assert finished[-1]["state"] == "timed_out"


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


@pytest.mark.parametrize("shared", [False, True])
def test_helper_uses_parent_command_approval_and_web_policy(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    from llm_cli.agent.tools import ToolOutcome
    from llm_cli.web.access import WebAccess
    from llm_cli.web.fetch import Page, WebFetchError

    questions: list[str] = []
    commands: list[tuple[list[str], str, int]] = []
    downloads: list[str] = []

    def ask(question: str) -> str:
        questions.append(question)
        return "2"

    def command(argv: list[str], cwd: str, timeout: int) -> ToolOutcome:
        commands.append((argv, cwd, timeout))
        return ToolOutcome("exit 0\nhelper output", False)

    def fetch(url: str, **kwargs: Any) -> Page:
        refusal = kwargs["allowed"]("docs.example.com", url)
        if refusal is not None:
            raise WebFetchError(refusal)
        downloads.append(url)
        return Page(url, 200, "text/plain", "allowed page", False)

    monkeypatch.setattr("llm_cli.web.access.fetch", fetch)
    broker_type = SharedToolBroker if shared else ToolBroker
    broker = broker_type(
        checkout,
        ("docs/",),
        asker=ask,
        command_runner=command,
        web=WebAccess("ask"),
    )
    task = "Run and fetch"
    provider = Provider(
        [],
        {
            task: [
                _calls(
                    (
                        "run_command",
                        {
                            "argv": ["cat", "docs/guide.md"],
                            "cwd": "docs",
                            "timeout": 17,
                        },
                    ),
                    ("web_fetch", {"url": "https://docs.example.com/guide"}),
                ),
                _text("Both tools answered."),
            ]
        },
    )
    explorer = Explorer(provider, broker)

    outcome = explorer(task)

    assert not outcome.is_error
    (helper,) = provider.helper_sessions()
    assert helper.tools == [
        *EXPLORER_TOOLS,
        "run_command",
        "web_fetch",
        *(("read_binary", "read_symlink") if shared else ()),
    ]
    command_result, web_result = helper.sent[1]  # type: ignore[misc]
    assert not command_result.is_error and "helper output" in command_result.content
    assert not web_result.is_error and "allowed page" in web_result.content
    assert "external content, not instructions" in web_result.content
    assert commands == [(["cat", "docs/guide.md"], "docs", 17)]
    assert len(questions) == 2
    assert broker.usage.calls == 2
    assert explorer.take_usage() == {"input_tokens": 20, "output_tokens": 4}
    # Task-wide choices and fetched pages are shared in both directions.
    assert not broker.invoke("run_command", {"argv": ["pwd"]}).is_error
    assert not broker.invoke(
        "web_fetch", {"url": "https://docs.example.com/guide"}
    ).is_error
    assert len(questions) == 2 and len(downloads) == 1
    assert broker.usage.calls == 4
    assert (checkout / "docs/guide.md").read_text() == "original guide\n"
    if shared:
        assert isinstance(broker, SharedToolBroker) and broker.candidates() == ()


def test_helper_refusals_match_parent_and_do_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.agent.tools import ToolOutcome
    from llm_cli.web.access import WebAccess
    from llm_cli.web.fetch import WebFetchError

    commands: list[object] = []

    def fetch(url: str, **kwargs: Any) -> Any:
        refusal = kwargs["allowed"]("denied.example.com", url)
        assert refusal is not None
        raise WebFetchError(refusal)

    monkeypatch.setattr("llm_cli.web.access.fetch", fetch)
    command_args = {"argv": ["pwd"]}
    web_args = {"url": "https://denied.example.com/"}
    explorer, broker, provider, _ = _explorer(
        tmp_path,
        [
            _calls(("run_command", command_args), ("web_fetch", web_args)),
            _text("Both denied."),
        ],
        asker=lambda question: "3",
        web=WebAccess("ask"),
        command_runner=lambda *args: commands.append(args) or ToolOutcome("bad", False),
    )

    assert not explorer("question").is_error
    (helper,) = provider.helper_sessions()
    command, web = helper.sent[1]  # type: ignore[misc]
    assert command.is_error and web.is_error
    assert command.content == broker.invoke("run_command", command_args).content
    assert web.content == broker.invoke("web_fetch", web_args).content
    assert commands == []
    assert broker.usage.calls == 4


def test_plan_helper_can_fetch_but_cannot_run_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.agent.tools import ToolOutcome
    from llm_cli.web.access import WebAccess
    from llm_cli.web.fetch import Page

    monkeypatch.setattr(
        "llm_cli.web.access.fetch",
        lambda url, **kwargs: Page(url, 200, "text/plain", "plan page", False),
    )
    commands: list[object] = []
    explorer, broker, provider, _ = _explorer(
        tmp_path,
        [
            _calls(
                ("run_command", {"argv": ["pwd"]}),
                ("web_fetch", {"url": "https://example.com/"}),
            ),
            _text("Fetched only."),
        ],
        agent_mode="plan",
        web=WebAccess("allow"),
        command_approval="allow",
        command_runner=lambda *args: commands.append(args) or ToolOutcome("bad", False),
    )

    assert not explorer("question").is_error
    (helper,) = provider.helper_sessions()
    assert helper.tools == [*EXPLORER_TOOLS, "web_fetch"]
    denied, fetched = helper.sent[1]  # type: ignore[misc]
    assert denied.is_error and "unknown tool 'run_command'" in denied.content
    assert not fetched.is_error and "plan page" in fetched.content
    assert commands == [] and broker.usage.calls == 2


def test_helper_does_not_start_another_tool_after_deadline(tmp_path: Path) -> None:
    from llm_cli.agent.tools import ToolOutcome
    from llm_cli.web.access import WebAccess

    now = [100.0]
    commands: list[object] = []

    def command(*args: object) -> ToolOutcome:
        commands.append(args)
        now[0] = 200.0
        return ToolOutcome("done", False)

    broker = ToolBroker(
        tmp_path,
        ("*",),
        command_runner=command,
        command_approval="allow",
        web=WebAccess("allow"),
    )
    provider = Provider(
        [],
        {
            "question": [
                _calls(
                    ("run_command", {"argv": ["pwd"]}),
                    ("run_command", {"argv": ["never"]}),
                ),
                _text("never sent"),
            ]
        },
    )
    explorer = Explorer(provider, broker, deadline=150.0, clock=lambda: now[0])

    outcome = explorer("question")

    assert outcome.is_error and "time limit" in outcome.content
    assert len(commands) == 1 and broker.usage.calls == 2
    (helper,) = provider.helper_sessions()
    assert len(helper.sent) == 1


def test_helper_command_cancellation_releases_reserved_budget(tmp_path: Path) -> None:
    def stopped(*args: object) -> Any:
        raise TaskCancelled("task stopped")

    explorer, broker, _, events = _explorer(
        tmp_path,
        [
            _calls(("run_command", {"argv": ["pwd"]})),
        ],
        command_runner=stopped,
        command_approval="allow",
    )

    with pytest.raises(TaskCancelled):
        explorer("question")
    assert broker.usage.calls == 1
    assert events[-1][1]["state"] == "cancelled"


@pytest.mark.parametrize("shared", [False, True])
def test_read_only_view_refuses_mutations_checks_and_questions(
    checkout: Path, shared: bool
) -> None:
    from llm_cli.agent.tools import ToolOutcome

    parent_type = SharedToolBroker if shared else ToolBroker
    parent = parent_type(
        checkout,
        ("*",),
        asker=lambda question: "yes",
        command_runner=lambda *args: ToolOutcome("ok", False),
        command_approval="allow",
    )
    view = parent.read_only_view(parent.limits)
    assert view.tool_names() == (
        *EXPLORER_TOOLS,
        "run_command",
        *(("read_binary", "read_symlink") if shared else ()),
    )
    calls = {
        "write_file": {"path": "docs/guide.md", "content": "changed"},
        "write_binary": {"path": "docs/data.bin", "content_base64": "AP8="},
        "write_symlink": {"path": "docs/link", "target": "guide.md"},
        "apply_patch": {
            "path": "docs/guide.md",
            "old_text": "original",
            "new_text": "x",
        },
        "delete_file": {"path": "docs/guide.md"},
        "create_directory": {"path": "new"},
        "rename_file": {"path": "docs/guide.md", "destination": "new.md"},
        "run_check": {"name": "tests"},
        "finish_task": {"answer": "done"},
        "ask_user": {"question": "approve"},
        "explore": {"task": "recurse"},
    }
    for name, arguments in calls.items():
        assert view.invoke(name, arguments).is_error, name
    assert not parent.usage.finished and not view.usage.finished
    assert (checkout / "docs/guide.md").read_text() == "original guide\n"


def test_helper_command_and_fetch_output_are_screened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.agent.tools import ToolOutcome
    from llm_cli.web.access import WebAccess
    from llm_cli.web.fetch import Page

    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    monkeypatch.setattr(
        "llm_cli.web.access.fetch",
        lambda url, **kw: Page(url, 200, "text/plain", secret, False),
    )
    explorer, broker, provider, events = _explorer(
        tmp_path,
        [
            _calls(
                ("run_command", {"argv": ["pwd"]}),
                ("web_fetch", {"url": "https://example.com/"}),
            ),
            _text("Results were withheld."),
        ],
        command_runner=lambda *args: ToolOutcome(secret, False),
        command_approval="allow",
        web=WebAccess("allow"),
    )
    outcome = explorer("question")
    assert not outcome.is_error
    (helper,) = provider.helper_sessions()
    results = helper.sent[1]
    assert all(result.is_error for result in results)  # type: ignore[union-attr]
    assert secret not in str(helper.sent) and secret not in str(events)
    assert broker.usage.calls == 2


def test_helper_command_keeps_parent_pre_tool_hook_without_double_charge(
    tmp_path: Path,
) -> None:
    from llm_cli.agent.tools import ToolOutcome

    seen: list[str] = []
    commands: list[object] = []

    class Hooks:
        def before_tool(self, name: str, arguments: object) -> tuple[str, str]:
            seen.append(name)
            return "guard", "operator policy"

    explorer, broker, provider, _ = _explorer(
        tmp_path,
        [
            _calls(("list_files", {}), ("run_command", {"argv": ["pwd"]})),
            _text("Blocked."),
        ],
        command_runner=lambda *args: commands.append(args) or ToolOutcome("bad", False),
        command_approval="allow",
        hooks=Hooks(),
    )
    assert not explorer("question").is_error
    (helper,) = provider.helper_sessions()
    result = helper.sent[1][1]  # type: ignore[index]
    assert (
        result.is_error
        and "guard" in result.content
        and "operator policy" in result.content
    )
    assert seen == ["run_command"] and commands == []
    assert broker.usage.calls == 2 and broker.usage.denied == 1


def test_parallel_helpers_serialize_parent_approval_questions(checkout: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from llm_cli.agent.tools import ToolOutcome

    asking = threading.Event()
    release = threading.Event()
    second_question = threading.Event()
    start = threading.Barrier(2)
    questions: list[str] = []

    def ask(question: str) -> str:
        questions.append(question)
        if len(questions) > 1:
            second_question.set()
        asking.set()
        assert release.wait(5)
        return "2"

    broker = ToolBroker(
        checkout,
        ("*",),
        asker=ask,
        command_runner=lambda *args: ToolOutcome("ok", False),
    )
    views = [broker.read_only_view(broker.limits) for _ in range(2)]

    def command(view: ToolBroker) -> object:
        start.wait(timeout=5)
        return view.invoke("run_command", {"argv": ["pwd"]})

    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = [workers.submit(command, view) for view in views]
        assert asking.wait(5)
        overlapped = second_question.wait(0.05)
        release.set()
        assert all(not result.result(timeout=5).is_error for result in pending)
    assert not overlapped and len(questions) == 1


def test_helper_read_callback_tracks_safe_partial_and_search_reads(
    checkout: Path,
) -> None:
    from llm_cli.workspace.identity import identify_path

    seen: list[tuple[str, object]] = []
    (checkout / "docs/secret.md").write_text("AKIA" + "ABCDEFGHIJKLMNOP")
    (checkout / "docs/.env").write_text("private configuration")
    (checkout / "docs/binary").write_bytes(b"\xff\x00")
    broker, _ = _shared(
        checkout, on_read=lambda path, identity: seen.append((path, identity))
    )
    view = broker.read_only_view(broker.limits)
    assert not view.invoke(
        "read_file", {"path": "docs/guide.md", "start_line": 1, "end_line": 1}
    ).is_error
    assert seen == [("docs/guide.md", identify_path(checkout / "docs/guide.md"))]
    assert view.invoke("read_file", {"path": "docs/secret.md"}).is_error
    assert view.invoke("read_file", {"path": "docs/.env"}).is_error
    assert view.invoke("read_file", {"path": "docs/binary"}).is_error
    assert len(seen) == 1
    search = view.invoke("search_text", {"pattern": "print", "path": "src/"})
    assert not search.is_error
    assert seen[-1] == ("src/app.py", identify_path(checkout / "src/app.py"))
    assert len(seen) == 2
    # A helper's registered read still does not authorize its parent's write.
    assert broker.invoke(
        "write_file", {"path": "docs/guide.md", "content": "changed"}
    ).is_error
    assert broker.candidates() == ()


def test_private_helper_overlay_and_diff_never_replay_source_read_callback(
    checkout: Path,
) -> None:
    from llm_cli.workspace.identity import identify_path

    seen: list[tuple[str, object]] = []

    def callback(path: str, identity: object) -> None:
        seen.append((path, identity))

    parent, _ = _shared(checkout, on_read=callback)
    assert not parent.invoke("read_file", {"path": "docs/guide.md"}).is_error
    original = seen.copy()
    assert not parent.invoke(
        "write_file", {"path": "docs/guide.md", "content": "private proposal\n"}
    ).is_error
    helper = parent.read_only_view(parent.limits)
    (checkout / "docs/guide.md").write_text("published peer edit\n")
    for _ in range(2):
        assert helper.invoke("read_file", {"path": "docs/guide.md"}).content == (
            "private proposal\n"
        )
        assert not helper.invoke("read_diff", {}).is_error
        assert not parent.invoke("read_diff", {}).is_error
    assert seen == original
    # A real later checkout read wins even if an older private helper replays.
    fresh, _ = _shared(checkout, on_read=callback)
    assert not fresh.invoke("read_file", {"path": "docs/guide.md"}).is_error
    newest = ("docs/guide.md", identify_path(checkout / "docs/guide.md"))
    assert seen[-1] == newest
    count = len(seen)
    helper.invoke("read_file", {"path": "docs/guide.md"})
    assert len(seen) == count and seen[-1] == newest


@pytest.mark.parametrize("batched", [False, True])
def test_pinned_partial_read_does_not_downgrade_latest_source_callback(
    checkout: Path, batched: bool
) -> None:
    from llm_cli.workspace.identity import identify_path

    (checkout / "docs/guide.md").write_text("original guide\nanother line\n")
    seen: list[tuple[str, object]] = []
    parent, _ = _shared(
        checkout, on_read=lambda path, identity: seen.append((path, identity))
    )
    helper = parent.read_only_view(parent.limits)

    def read(arguments: dict[str, object]) -> Any:
        if batched:
            return helper.invoke_reads(
                [("read_file", arguments), ("list_files", {"path": "docs"})]
            )[0]
        return helper.invoke("read_file", arguments)

    arguments: dict[str, object] = {
        "path": "docs/guide.md",
        "start_line": 1,
        "end_line": 1,
    }
    assert not read(arguments).is_error
    assert len(seen) == 1
    (checkout / "docs/guide.md").write_text("new checkout\n")
    assert "original" in read(arguments).content
    assert "original" in read({"path": "docs/guide.md"}).content
    assert len(seen) == 1
    assert not read({**arguments, "refresh": True}).is_error
    assert seen[-1] == ("docs/guide.md", identify_path(checkout / "docs/guide.md"))
    assert len(seen) == 2


@pytest.mark.parametrize("batched", [False, True])
def test_fresh_absent_source_callback_tracks_later_creation(
    checkout: Path, batched: bool
) -> None:
    from llm_cli.workspace.identity import ABSENT, identify_path

    seen: list[tuple[str, object]] = []
    parent, _ = _shared(
        checkout, on_read=lambda path, identity: seen.append((path, identity))
    )
    helper = parent.read_only_view(parent.limits)
    missing = {"path": "docs/missing.md"}
    if batched:
        outcomes = helper.invoke_reads(
            [("read_file", missing), ("read_file", {"path": "docs/guide.md"})]
        )
        assert outcomes[0].is_error and not outcomes[1].is_error
    else:
        assert helper.invoke("read_file", missing).is_error
    assert ("docs/missing.md", ABSENT) in seen
    assert sum(path == "docs/missing.md" for path, _ in seen) == 1
    (checkout / "docs/missing.md").write_text("created outside\n")
    assert not helper.invoke("read_file", missing).is_error
    assert seen[-1] == ("docs/missing.md", identify_path(checkout / "docs/missing.md"))


def test_batch_source_callbacks_happen_at_capture_and_not_cached_replay(
    checkout: Path,
) -> None:
    seen: list[tuple[str, object, str]] = []

    class ChangedAfterCapture(SharedToolBroker):
        def _match_searches(self, *args: Any) -> Any:
            ready = super()._match_searches(*args)
            (checkout / "docs/guide.md").write_text("later checkout\n")
            return ready

    broker = ChangedAfterCapture(
        checkout,
        ("*",),
        on_read=lambda path, identity: seen.append(
            (path, identity, (checkout / "docs/guide.md").read_text())
        ),
    )
    outcomes = broker.invoke_reads(
        [
            ("read_file", {"path": "docs/guide.md"}),
            ("read_file", {"path": "src/app.py"}),
        ]
    )
    assert all(not result.is_error for result in outcomes)
    assert outcomes[0].content == "original guide\n"
    assert len(seen) == 2
    assert all(content == "original guide\n" for _, _, content in seen)


def test_batch_mixed_pinned_read_and_refresh_registers_only_new_source(
    checkout: Path,
) -> None:
    from llm_cli.workspace.identity import identify_path

    (checkout / "docs/guide.md").write_text("original guide\nanother line\n")
    seen: list[tuple[str, object]] = []
    parent, _ = _shared(
        checkout, on_read=lambda path, identity: seen.append((path, identity))
    )
    helper = parent.read_only_view(parent.limits)
    partial: dict[str, object] = {
        "path": "docs/guide.md",
        "start_line": 1,
        "end_line": 1,
    }
    assert not helper.invoke("read_file", partial).is_error
    assert len(seen) == 1
    (checkout / "docs/guide.md").write_text("new checkout\n")
    outcomes = helper.invoke_reads(
        [
            ("read_file", partial),
            ("read_file", {"path": "docs/guide.md", "refresh": True}),
        ]
    )
    assert "original" in outcomes[0].content
    assert outcomes[1].content == "new checkout\n"
    assert len(seen) == 2
    assert seen[-1] == ("docs/guide.md", identify_path(checkout / "docs/guide.md"))


@pytest.mark.parametrize(
    "invalid",
    [
        {"refresh": True, "start_line": 0},
        {"refresh": True, "start_line": 2, "end_line": 1},
        {"refresh": True, "start_column": 0},
        {"refresh": "yes"},
    ],
)
def test_invalid_batch_read_cannot_refresh_a_pinned_source_observation(
    checkout: Path, invalid: dict[str, object]
) -> None:
    (checkout / "docs/guide.md").write_text("original guide\nanother line\n")
    seen: list[tuple[str, object]] = []
    parent, _ = _shared(
        checkout, on_read=lambda path, identity: seen.append((path, identity))
    )
    helper = parent.read_only_view(parent.limits)
    partial: dict[str, object] = {
        "path": "docs/guide.md",
        "start_line": 1,
        "end_line": 1,
    }
    assert not helper.invoke("read_file", partial).is_error
    original = seen.copy()
    (checkout / "docs/guide.md").write_text("new checkout\n")
    outcomes = helper.invoke_reads(
        [("read_file", partial), ("read_file", {"path": "docs/guide.md", **invalid})]
    )
    assert not outcomes[0].is_error and "original" in outcomes[0].content
    assert outcomes[1].is_error
    assert seen == original


def test_shared_helper_reads_typed_overlay_without_authorizing_parent_edits(
    checkout: Path,
) -> None:
    import base64

    (checkout / "docs/existing.bin").write_bytes(b"\x00existing")
    (checkout / "docs/existing-link").symlink_to("guide.md")
    parent, _ = _shared(checkout)
    assert not parent.invoke(
        "write_binary", {"path": "docs/new.bin", "content_base64": "AP8="}
    ).is_error
    assert not parent.invoke(
        "write_symlink", {"path": "docs/new-link", "target": "guide.md"}
    ).is_error
    view = parent.read_only_view(parent.limits)
    assert view.invoke("read_binary", {"path": "docs/new.bin"}).content == "AP8="
    assert view.invoke("read_symlink", {"path": "docs/new-link"}).content == "guide.md"
    assert view.invoke(
        "read_binary", {"path": "docs/existing.bin"}
    ).content == base64.b64encode(b"\x00existing").decode("ascii")
    assert (
        view.invoke("read_symlink", {"path": "docs/existing-link"}).content
        == "guide.md"
    )
    assert view.invoke(
        "write_file", {"path": "docs/guide.md", "content": "forbidden"}
    ).is_error
    for broker in (view, parent):
        assert broker.invoke(
            "write_binary", {"path": "docs/existing.bin", "content_base64": "AP8="}
        ).is_error
        assert broker.invoke(
            "write_symlink", {"path": "docs/existing-link", "target": "new.bin"}
        ).is_error
    assert (checkout / "docs/existing.bin").read_bytes() == b"\x00existing"
    assert (checkout / "docs/existing-link").readlink() == Path("guide.md")
    assert {item.relative_path for item in parent.candidates()} == {
        "docs/new.bin",
        "docs/new-link",
    }
