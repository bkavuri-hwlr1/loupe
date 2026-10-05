"""The update_plan checklist is validated, durable, and shown as progress."""

from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from llm_cli.agent.driver import RunRequest
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import ToolBroker, tool_schemas
from llm_cli.cli.render import EventRenderer, render_event
from llm_cli.providers.base import ModelTurn, ToolCallResult

_STEPS = [
    {"step": "Read the search walk", "status": "completed"},
    {"step": "Batch the exclusion checks", "status": "in_progress"},
    {"step": "Add tests", "status": "pending"},
]


def _broker(
    tmp_path: Path, **kwargs: Any
) -> tuple[ToolBroker, list[tuple[str, dict[str, object]]]]:
    events: list[tuple[str, dict[str, object]]] = []
    broker = ToolBroker(
        tmp_path,
        ("*",),
        on_event=lambda kind, payload: events.append((kind, payload)),
        **kwargs,
    )
    return broker, events


def test_plan_is_recorded_announced_and_acknowledged(tmp_path: Path) -> None:
    broker, events = _broker(tmp_path)

    result = broker.invoke("update_plan", {"steps": _STEPS})

    assert not result.is_error
    assert result.content == "Plan updated: 1 of 3 steps completed."
    assert broker.usage.plan == (
        ("Read the search walk", "completed"),
        ("Batch the exclusion checks", "in_progress"),
        ("Add tests", "pending"),
    )
    assert ("plan.updated", {"steps": _STEPS, "completed": 1, "total": 3}) in events


def test_step_text_is_collapsed_to_one_line(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    steps = [{"step": "  Run\n the\ttests  ", "status": "pending"}]

    assert not broker.invoke("update_plan", {"steps": steps}).is_error
    assert broker.usage.plan == (("Run the tests", "pending"),)


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        (None, "steps is required"),
        ([], "at least one step"),
        ("Read files", "at most 12 items"),
        ([{"step": f"Step {n}", "status": "pending"} for n in range(13)], "at most 12"),
        ([{"step": "Read", "status": "started"}], "pending, in_progress, or completed"),
        ([{"step": "  ", "status": "pending"}], "nonblank text"),
        ([{"step": "x" * 201, "status": "pending"}], "at most 200 characters"),
        ([{"step": "Read"}], "exactly a step and a status"),
        ([{"step": "Read", "status": "pending", "note": "x"}], "exactly a step"),
        (
            [
                {"step": "Read", "status": "in_progress"},
                {"step": "Edit", "status": "in_progress"},
            ],
            "at most one step can be in_progress",
        ),
    ],
)
def test_malformed_plans_are_refused_without_changing_the_plan(
    tmp_path: Path, steps: object, message: str
) -> None:
    broker, events = _broker(tmp_path)
    assert not broker.invoke("update_plan", {"steps": _STEPS}).is_error
    arguments = {} if steps is None else {"steps": steps}

    result = broker.invoke("update_plan", arguments)

    assert result.is_error and message in result.content
    assert len(broker.usage.plan) == 3
    assert [kind for kind, _ in events].count("plan.updated") == 1


def test_secret_material_is_kept_out_of_the_plan(tmp_path: Path) -> None:
    broker, events = _broker(tmp_path)
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    steps = [{"step": f"Rotate {secret}", "status": "pending"}]

    result = broker.invoke("update_plan", {"steps": steps})

    assert result.is_error and "secret material" in result.content
    assert broker.usage.plan == ()
    assert not any(kind == "plan.updated" for kind, _ in events)


def test_plan_survives_a_checkpoint_round_trip(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)
    broker.invoke("update_plan", {"steps": _STEPS})
    saved = broker.usage_snapshot()
    assert saved["plan"] == _STEPS

    restored, _ = _broker(tmp_path)
    restored.restore_usage(saved)
    assert restored.usage.plan == broker.usage.plan

    older = {key: value for key, value in saved.items() if key != "plan"}
    restored.restore_usage(older)
    assert restored.usage.plan == ()

    with pytest.raises(ValueError, match="in_progress"):
        restored.restore_usage(
            {**saved, "plan": [{"step": "a", "status": "in_progress"}] * 2}
        )


def test_plan_is_offered_outside_plan_mode_only(tmp_path: Path) -> None:
    for mode in ("auto", "normal"):
        offered = ToolBroker(tmp_path, ("*",), agent_mode=mode).tool_names()
        assert "update_plan" in offered
    plan_mode = ToolBroker(tmp_path, ("*",), agent_mode="plan")
    assert "update_plan" not in plan_mode.tool_names()
    result = plan_mode.invoke("update_plan", {"steps": _STEPS})
    assert result.is_error and "Plan mode" in result.content
    (schema,) = tool_schemas(["update_plan"])
    assert schema["input_schema"]["required"] == ["steps"]  # type: ignore[index]


def test_shared_plan_updates_do_not_wait_for_the_read_barrier(tmp_path: Path) -> None:
    lock = threading.RLock()
    broker = SharedToolBroker(worktree=tmp_path, scopes=("*",), publication_lock=lock)
    released = threading.Event()

    def hold() -> None:
        with lock:
            released.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold)
        try:
            result = broker.invoke("update_plan", {"steps": _STEPS})
        finally:
            released.set()
        holder.result(timeout=5)
    assert not result.is_error


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


def test_system_prompt_explains_the_plan_only_when_offered(tmp_path: Path) -> None:
    offered = Script()
    CodingAgentHarness(offered).run(
        RunRequest("task", 1, "fix it", ("*",), tmp_path, "base"),
        ToolBroker(tmp_path, ("*",)),
    )
    plan_mode = Script()
    CodingAgentHarness(plan_mode).run(
        RunRequest("task", 1, "plan it", ("*",), tmp_path, "base", agent_mode="plan"),
        ToolBroker(tmp_path, ("*",), agent_mode="plan"),
    )

    assert "call update_plan early" in offered.systems[0]
    assert "update_plan" not in plan_mode.systems[0]


def _plan_event(task: str, steps: list[dict[str, str]]) -> dict[str, object]:
    completed = sum(step["status"] == "completed" for step in steps)
    return {
        "event_type": "plan.updated",
        "task_id": task,
        "payload": {"steps": steps, "completed": completed, "total": len(steps)},
    }


class ProgressRecorder(EventRenderer):
    def __init__(self, stream: io.StringIO) -> None:
        super().__init__(stream, plain=True)
        self.progress: list[str | None] = []
        self.ui.progress = self.progress.append  # type: ignore[method-assign,assignment]


def test_plan_updates_print_a_checklist_once_and_track_the_current_step() -> None:
    stream = io.StringIO()
    renderer = ProgressRecorder(stream)

    renderer.render(_plan_event("task-1", _STEPS))
    renderer.render(_plan_event("task-1", _STEPS))

    assert stream.getvalue() == (
        "  Plan · 1 of 3 done\n"
        "    ✓ Read the search walk\n"
        "    ▸ Batch the exclusion checks\n"
        "    ○ Add tests\n"
    )
    assert renderer.progress[-1] == "Step 2 of 3 · Batch the exclusion checks"

    finished = [{**step, "status": "completed"} for step in _STEPS]
    renderer.render(_plan_event("task-1", finished))
    assert "Plan · 3 of 3 done" in stream.getvalue()
    assert renderer.progress[-1] == "Plan · 3 of 3 done"

    renderer.render(
        {"event_type": "model.finished", "task_id": "task-1", "payload": {}}
    )
    assert renderer.progress[-1] is None


def test_a_new_task_starts_without_the_previous_plan() -> None:
    stream = io.StringIO()
    renderer = ProgressRecorder(stream)
    renderer.render(_plan_event("task-1", _STEPS))

    renderer.render({"event_type": "model.started", "task_id": "task-2", "payload": {}})
    assert renderer.progress[-1] is None
    renderer.render(_plan_event("task-2", _STEPS))
    assert stream.getvalue().count("Plan · 1 of 3 done") == 2


def test_plan_text_is_sanitized_and_malformed_events_are_ignored() -> None:
    stream = io.StringIO()
    renderer = EventRenderer(stream, plain=True)
    renderer.render(
        _plan_event("t", [{"step": "Read \x1b[31mred\x1b[0m", "status": "pending"}])
    )
    renderer.render(
        {"event_type": "plan.updated", "task_id": "t", "payload": {"steps": "x"}}
    )
    renderer.render(_plan_event("t", [{"step": "Read", "status": "unknown"}]))

    assert "\x1b" not in stream.getvalue()
    assert stream.getvalue().count("Plan ·") == 1
    assert render_event(_plan_event("t", _STEPS)) == (
        "  Plan · 1 of 3 done\n"
        "    ✓ Read the search walk\n"
        "    ▸ Batch the exclusion checks\n"
        "    ○ Add tests"
    )
