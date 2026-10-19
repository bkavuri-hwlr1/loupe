"""Optional discovery has ordinary budgets and no editing/coordination authority."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_harness_coordination import Script, _finish, _request, _turn

from llm_cli.agent.driver import CoordinationUpdate
from llm_cli.agent.harness import CodingAgentHarness, _results_with_coordination
from llm_cli.agent.limits import ExecutionLimits
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import ToolBroker, ToolBudgetExhausted
from llm_cli.knowledge.retrieval import CLOSE, OPEN, render_retrieval
from llm_cli.providers.base import ToolCallResult


@pytest.mark.parametrize("broker_type", [ToolBroker, SharedToolBroker])
@pytest.mark.parametrize("mode", ["auto", "normal", "plan"])
@pytest.mark.parametrize("enabled", [False, True])
def test_visibility_and_read_only_helper_inheritance(
    tmp_path: Path, broker_type: type[ToolBroker], mode: str, enabled: bool
) -> None:
    broker = broker_type(tmp_path, ("*",), agent_mode=mode)
    if enabled:
        broker.project_search = lambda query, limit: {"state": "ready", "results": []}
    view = broker.read_only_view(ExecutionLimits())
    for surface in (broker, view):
        assert ("search_project" in surface.tool_names()) is enabled
        assert (
            any(
                schema["name"] == "search_project"
                for schema in surface.tool_schemas(surface.tool_names())
            )
            is enabled
        )
        result = surface.invoke("search_project", {"query": "publish"})
        assert result.is_error is not enabled
        if enabled:
            assert result.content.startswith(OPEN) and result.content.endswith(CLOSE)
    assert "write_file" not in view.tool_names()


def test_helper_retrieval_uses_its_reserved_budget_and_emits_only_safe_metrics(
    tmp_path: Path,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    broker = ToolBroker(
        tmp_path, ("*",), on_event=lambda name, payload: events.append((name, payload))
    )
    broker.project_search = lambda query, limit: {
        "state": "ready",
        "results": [{"text": "private source"}],
    }
    view = broker.read_only_view(ExecutionLimits(max_tool_calls=1))
    assert not view.invoke("search_project", {"query": "publish"}).is_error
    assert view.usage.calls == 1 and broker.usage.calls == 0
    assert view.usage.bytes_read > 0
    assert events == [
        (
            "retrieval.completed",
            {"helper": True, "state": "ready", "bytes": view.usage.bytes_read},
        )
    ]
    with pytest.raises(ToolBudgetExhausted):
        view.invoke("search_project", {"query": "publish"})
    assert "private source" not in json.dumps(events)


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": ""},
        {"query": "x" * 501},
        {"query": "ok", "limit": 7},
        {"query": "ok", "limit": True},
    ],
)
def test_bad_retrieval_queries_do_not_reach_index(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    broker = ToolBroker(
        tmp_path,
        ("*",),
        project_search=lambda *_: pytest.fail("invalid query reached index"),
    )
    assert broker.invoke("search_project", arguments).is_error
    assert broker.usage.calls == 1


def test_render_is_bounded_with_valid_labels_and_does_not_modify_results() -> None:
    result = {
        "state": "ready",
        "results": [{"text": "😀" * 30000}],
        "graph": {"edges": [{}] * 300, "nodes": [{}] * 200},
    }
    saved = copy.deepcopy(result)
    text = render_retrieval(result, 500)
    assert len(text.encode()) <= 500 and text.startswith(OPEN) and text.endswith(CLOSE)
    assert result == saved
    assert render_retrieval(result, 1) == ""


def test_coordination_precedes_optional_data_and_results_keep_native_order() -> None:
    results = (
        ToolCallResult("search", OPEN + "\ndata\n" + CLOSE),
        ToolCallResult("read", "source"),
    )
    decorated = _results_with_coordination(
        results, CoordinationUpdate(4, "mandatory facts"), 1000
    )
    assert [row.call_id for row in decorated] == ["search", "read"]
    assert decorated[0].content.index("mandatory facts") < decorated[0].content.index(
        OPEN
    )
    assert decorated[1].content == "source"
    assert results[0].content.startswith(OPEN)


@pytest.mark.parametrize("enabled", [False, True])
def test_scripted_harness_keeps_exact_coordination_cursor_and_checkpoints_separate(
    tmp_path: Path, enabled: bool
) -> None:
    provider = Script(
        [_turn(("search", "search_project", {"query": "publish"})), _finish()]
    )
    broker = ToolBroker(tmp_path, ("*",))
    if enabled:
        broker.project_search = lambda query, limit: {"state": "ready", "results": []}
    cursors: list[int | None] = []
    checkpoints: list[dict[str, object]] = []

    def refresh(after: int | None) -> CoordinationUpdate:
        cursors.append(after)
        return CoordinationUpdate(len(cursors), f"mandatory-{len(cursors)}")

    CodingAgentHarness(provider, explorations=False).run(
        replace(
            _request(tmp_path),
            refresh_coordination=refresh,
            checkpoint=lambda state: checkpoints.append(copy.deepcopy(dict(state))),
        ),
        broker,
    )
    assert cursors == [None, 1]
    assert checkpoints[-1]["coordination_sequence"] == 2
    assert broker.usage.calls == 2
    assert broker.usage.writes == 0
    if enabled:
        result = provider.calls[1][0]
        assert result.content.index("mandatory-2") < result.content.index(OPEN)
        assert all(
            "mandatory-" not in row["content"]
            for state in checkpoints
            for row in state["tool_results"]
        )


@pytest.mark.parametrize("allowance", [0, 2, 65, 100, 500])
def test_optional_context_obeys_exact_utf8_output_boundary(allowance: int) -> None:
    mandatory = CoordinationUpdate(4, "mandatory 😀 facts")
    baseline = _results_with_coordination((ToolCallResult("x", ""),), mandatory, 1000)[
        0
    ].content
    limit = len(baseline.encode()) + allowance
    result = _results_with_coordination(
        (ToolCallResult("x", OPEN + "\n" + "😀" * 200 + "\n" + CLOSE),),
        mandatory,
        limit,
    )[0].content
    assert len(result.encode()) <= limit
    assert "mandatory 😀 facts" in result
    if OPEN in result:
        assert result.endswith(CLOSE)
        assert result.index("mandatory 😀 facts") < result.index(OPEN)
