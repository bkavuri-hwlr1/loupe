from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from llm_cli.errors import LlmCoordError

_SPEC = importlib.util.spec_from_file_location(
    "live_benchmark", Path(__file__).resolve().parents[2] / "scripts/live_benchmark.py"
)
assert _SPEC is not None and _SPEC.loader is not None
benchmark = importlib.util.module_from_spec(_SPEC)
# Dataclasses resolve their annotations through the module's registered entry.
sys.modules["live_benchmark"] = benchmark
_SPEC.loader.exec_module(benchmark)


def test_the_shipped_tasks_are_valid() -> None:
    suite = benchmark.load_suite()

    assert suite.version >= 1
    assert len(suite.revision) == 40
    assert all(task.prompt and task.facts for task in suite.tasks)
    assert {task.explore for task in suite.tasks} <= {"any", "never", "expected"}


@pytest.mark.parametrize(
    ("tasks", "message"),
    [
        ('[[tasks]]\nid = "a"\nprompt = "q"\nmode = "chat"\n', "unknown mode"),
        ('[[tasks]]\nid = "a"\nprompt = "q"\nfacts = ["x"]\n', "lists of text"),
        ('[[tasks]]\nid = "a"\nprompt = "q"\nmin_facts = 0\n', "min_facts"),
        ('[[tasks]]\nid = "a"\nprompt = "q"\n' * 2, "unique ids"),
    ],
)
def test_task_file_mistakes_are_refused(
    tmp_path: Path, tasks: str, message: str
) -> None:
    path = tmp_path / "tasks.toml"
    path.write_text(f'version = 1\nrevision = "abc"\n{tasks}', encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        benchmark.load_suite(path)


def test_a_fact_counts_when_any_wording_appears() -> None:
    found, missed = benchmark.fact_coverage(
        "It calls os.replace and marks the batch OPERATOR_ATTENTION.",
        [["os.replace"], ["operator_attention", "operator attention"], ["fsync"]],
    )

    assert found == ["os.replace", "operator_attention"]
    assert missed == ["fsync"]


def _events() -> list[tuple[str, dict[str, object]]]:
    usage = {"prompt_tokens": 9_000, "output_tokens": 300}
    return [
        ("model.turn.completed", {"usage": usage, "context_tokens": 5_000}),
        ("model.tool_call", {"tool": "read_file"}),
        ("model.tool_call", {"tool": "explore"}),
        ("explore.started", {"exploration_id": "x", "effort": "low"}),
        (
            "explore.finished",
            {
                "exploration_id": "x",
                "state": "completed",
                "tool_calls": 12,
                "seconds": 60.5,
                "retries": 1,
                "reason": None,
            },
        ),
        # Older turn events carry no context size; their prompt is used.
        ("model.turn.completed", {"usage": {"input_tokens": 7_000}}),
        (
            "model.finished",
            {
                "part": 1,
                "answer": "second half",
                "outcome": "completed",
                "usage": {
                    "prompt_tokens": 120_000,
                    "output_tokens": 4_000,
                    "explore_prompt_tokens": 100_000,
                },
            },
        ),
        (
            "model.finished",
            {"part": 0, "answer": "First half, ", "outcome": "completed"},
        ),
    ]


def test_events_are_measured() -> None:
    metrics = benchmark.summarize_events(_events())

    assert metrics.answer == "First half, second half"
    assert metrics.outcome == "completed"
    assert metrics.main_turns == 2
    assert metrics.main_tool_calls == {"read_file": 1, "explore": 1}
    assert metrics.peak_context_tokens == 7_000
    assert metrics.explorations == [
        {
            "state": "completed",
            "tool_calls": 12,
            "seconds": 60.5,
            "retries": 1,
            "effort": "low",
            "reason": None,
        }
    ]


def test_results_check_each_expectation() -> None:
    metrics = benchmark.summarize_events(_events())
    task = benchmark.Task(
        id="t",
        prompt="q",
        explore="expected",
        facts=(("first",), ("second",), ("third",)),
        min_facts=0.6,
    )

    result = benchmark.task_result(task, "completed", None, metrics, 95.04, 0.0)

    assert result["passed"] is True
    assert result["checks"] == {"completed": True, "facts": True, "explored": True}
    assert result["facts_missed"] == ["third"]
    assert result["wall_seconds"] == 95.0
    assert result["main_tool_calls"] == 2
    assert result["prompt_tokens"] == 120_000
    assert result["helper_prompt_tokens"] == 100_000

    # A narrow question should not explore, and a failed task never passes.
    narrow = benchmark.Task(id="n", prompt="q", explore="never")
    failed = benchmark.task_result(narrow, "failed", "CLAIM_STALE", metrics, 1, 0)
    assert failed["passed"] is False
    assert failed["checks"] == {"completed": False, "no_explore": False}


def test_run_settings_are_checked_before_any_request(tmp_path: Path) -> None:
    path = benchmark.write_config(
        tmp_path, ["explore_effort=low", "explore=false"], "bench"
    )

    assert path.read_text() == '[agent]\nexplore_effort = "low"\nexplore = false\n'
    with pytest.raises(LlmCoordError):
        benchmark.write_config(tmp_path, ["explore_efort=low"], "bench")
    with pytest.raises(ValueError, match="KEY=VALUE"):
        benchmark.write_config(tmp_path, ["explore"], "bench")


def test_runs_are_compared_task_by_task() -> None:
    def run(passed: bool, seconds: float, found: list[str]) -> dict[str, object]:
        task = {
            "id": "three-questions",
            "passed": passed,
            "wall_seconds": seconds,
            "main_tool_calls": 5,
            "prompt_tokens": 320_000,
            "facts_found": found,
        }
        return {"suite_version": 1, "tasks": [task]}

    lines = benchmark.compare(run(False, 236.0, ["a"]), run(True, 145.0, ["a", "b"]))

    assert "False → True" in lines[1]
    assert "236.0 → 145.0" in lines[1]
    assert "1 → 2" in lines[1]
    different = benchmark.compare({"suite_version": 1}, {"suite_version": 2})
    assert "different task versions" in different[0]


def test_the_default_profile_is_refused() -> None:
    with pytest.raises(SystemExit, match="not the default one"):
        benchmark.main(["--profile", "default"])
    with pytest.raises(SystemExit):
        benchmark.main([])
