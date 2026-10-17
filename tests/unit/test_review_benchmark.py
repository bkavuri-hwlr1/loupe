"""The planted-review benchmark requires observed findings and catches its bug."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_live_benchmark import benchmark


def test_planted_review_task_requires_one_observed_successful_review() -> None:
    task = next(
        t
        for t in benchmark.load_suite().tasks
        if t.id == "independent-review-planted-bug"
    )
    assert task.min_review_findings == 1
    # Piped chat treats physical input lines as separate submitted prompts.
    assert "\n" not in task.prompt
    assert not benchmark.checks(task, "completed", benchmark.Metrics())[
        "independent_review"
    ]
    metrics = benchmark.summarize_events(
        [
            ("review.started", {}),
            ("review.finished", {"state": "completed", "findings": 1}),
        ]
    )
    assert benchmark.checks(task, "completed", metrics)["independent_review"]
    assert not benchmark.checks(task, "completed", replace(metrics, review_attempts=2))[
        "independent_review"
    ]
    metrics.reviews[0]["state"] = "failed"
    assert not benchmark.checks(task, "completed", metrics)["independent_review"]


def test_review_metrics_drop_untrusted_details_and_bound_count() -> None:
    metrics = benchmark.summarize_events(
        [
            (
                "review.finished",
                {
                    "state": "completed",
                    "findings": 100_000,
                    "reason": "private provider data",
                },
            ),
        ]
        * 20
    )
    assert len(metrics.reviews) == 8
    assert all(
        item == {"state": "completed", "findings": 0} for item in metrics.reviews
    )
    assert "private" not in str(metrics.reviews)


def test_hidden_acceptance_catches_seed_and_accepts_repair(tmp_path: Path) -> None:
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    root = tmp_path / "fixture"
    shutil.copytree(scripts / "benchmark_fixtures/review-pagination", root)
    target = root / "src/pager.py"
    original = target.read_text()
    hidden = scripts / "benchmark_acceptance/review-pagination/test_reviewed_pager.py"

    def check(path: Path) -> int:
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(path)],
            cwd=root,
            env={
                **os.environ,
                "BENCHMARK_REPOSITORY": str(root),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            capture_output=True,
            timeout=30,
            check=False,
        ).returncode

    assert check(hidden) == 0
    target.write_text(
        original[: original.rfind("    return")]
        + "    return list(items)[start : start + size - 1]\n"
    )
    # Configured smoke checks pass even with the planted defect.
    assert check(root / "tests") == 0
    assert check(hidden) == 1
    target.write_text(target.read_text().replace("start + size - 1", "start + size"))
    assert check(hidden) == 0


def _corpus():
    return [task for task in benchmark.load_suite().tasks if task.review_kind]


def test_corpus_has_eight_defects_and_four_correct_controls() -> None:
    corpus = _corpus()
    assert len([t for t in corpus if t.review_kind == "bug"]) == 8
    assert len([t for t in corpus if t.review_kind == "clean"]) == 4
    assert {t.id for t in corpus if t.review_kind == "bug"} == {
        "review-boundary",
        "review-inverted",
        "review-error-path",
        "review-reset",
        "review-default",
        "review-resource",
        "review-path",
        "review-ordering",
    }
    assert all("\n" not in task.prompt for task in corpus)


def test_all_corpus_seeds_and_reference_fixes_have_independent_oracles(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import runpy

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    for task in _corpus():
        directory = tmp_path / task.id
        shutil.copytree(scripts / "benchmark_fixtures" / task.fixture, directory)
        monkeypatch.setenv("BENCHMARK_REPOSITORY", str(directory))
        hidden = (
            scripts / "benchmark_acceptance" / task.acceptance / "test_acceptance.py"
        )
        oracle = runpy.run_path(str(hidden))["test_contract"]
        target = directory / task.seed_path
        original = target.read_text()
        assert original.count(task.seed_before) == 1
        case_temporary = directory / "temporary"
        case_temporary.mkdir()
        oracle(case_temporary)
        shutil.rmtree(case_temporary)
        target.write_text(original.replace(task.seed_before, task.seed_after, 1))
        public = {"subject": runpy.run_path(str(hidden))["load_subject"]()}
        smoke_path = directory / "tests/test_smoke.py"
        source = smoke_path.read_text().replace("from src import subject\n", "", 1)
        exec(compile(source, str(smoke_path), "exec"), public)
        case_temporary.mkdir()
        public["test_smoke"](case_temporary)
        shutil.rmtree(case_temporary)
        case_temporary.mkdir()
        if task.review_kind == "bug":
            import pytest

            with pytest.raises((AssertionError, ValueError)):
                oracle(case_temporary)
        else:
            oracle(case_temporary)
        shutil.rmtree(case_temporary)
        target.write_text(original)
        case_temporary.mkdir()
        oracle(case_temporary)


def test_pair_order_alternates_without_dropping_repetitions() -> None:
    tasks = _corpus()[:2]
    order = benchmark.execution_order(tasks, 3, True)
    assert len(order) == 12
    assert [(r, t.id, arm) for r, t, arm in order[:2]] == [
        (1, tasks[0].id, "off"),
        (1, tasks[0].id, "on"),
    ]
    assert [arm for r, t, arm in order if r == 2 and t == tasks[0]] == ["on", "off"]
    assert len(benchmark.execution_order(tasks, 3, False)) == 6


def _seed_edit(task, content: str, call_id: str = "seed"):
    original = (benchmark.FIXTURES / task.fixture / task.seed_path).read_text()
    content = original.replace(task.seed_before, content, 1)
    return [
        (
            "model.tool_call",
            {
                "call_id": call_id,
                "tool": "write_file",
                "arguments": {"path": task.seed_path, "content": content},
            },
        ),
        ("model.tool_result", {"call_id": call_id, "tool": "write_file"}),
    ]


def test_seed_requires_success_and_must_still_exist_when_review_starts() -> None:
    task = _corpus()[0]
    seeded = _seed_edit(task, task.seed_after)
    start = [("review.started", {})]
    assert benchmark.seed_observed(task, [*seeded, *start])
    assert not benchmark.seed_observed(task, [*start, *seeded])
    assert not benchmark.seed_observed(task, [*seeded[:-1], *start])
    repaired = _seed_edit(task, task.seed_before, "repair")
    assert not benchmark.seed_observed(task, [*seeded, *repaired, *start])
    assert benchmark.seed_observed(task, [*seeded, *start, *repaired])
    failed = [seeded[0], ("model.tool_result", {"call_id": "seed", "is_error": True})]
    assert not benchmark.seed_observed(task, [*failed, *start])


def test_review_duration_repairs_cache_and_timeout_are_measured_without_details() -> (
    None
):
    metrics = benchmark.summarize_events(
        [
            ("review.started", {"_benchmark_created_at": 10_000}),
            (
                "review.finished",
                {"_benchmark_created_at": 12_500, "state": "completed", "findings": 1},
            ),
            ("model.tool_result", {"call_id": "repair", "tool": "write_file"}),
            (
                "model.finished",
                {
                    "answer": "done",
                    "usage": {
                        "prompt_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 70,
                        "review_prompt_tokens": 30,
                        "review_output_tokens": 5,
                        "review_cache_read_input_tokens": 20,
                    },
                },
            ),
        ]
    )
    assert metrics.review_seconds == 2.5
    assert metrics.review_repair_edits == 1
    result = benchmark.task_result(_corpus()[0], "completed", None, metrics, 10, 0)
    assert result["prompt_tokens"] == 100
    assert result["review_prompt_tokens"] == 30
    assert result["cached_tokens"] == 70
    assert result["review_cached_tokens"] == 20
    timeout = benchmark.summarize_events(
        [
            ("review.started", {}),
            (
                "review.finished",
                {
                    "state": "failed",
                    "findings": 0,
                    "reason": "the reviewer timed out; private details",
                },
            ),
        ]
    )
    assert timeout.review_timeouts == timeout.review_incomplete == 1
    assert "private" not in str(timeout.reviews)


def test_paired_summary_distinguishes_harm_and_incomplete_runs() -> None:
    base = {
        "review_arm": "on",
        "seed_observed": True,
        "passed": True,
        "state": "completed",
        "wall_seconds": 12,
        "prompt_tokens": 100,
        "output_tokens": 20,
        "review_prompt_tokens": 30,
        "review_output_tokens": 5,
        "reviews": [{"findings": 1}],
        "review_repair_edits": 1,
    }
    rows = [
        {
            **base,
            "id": "bug",
            "review_kind": "bug",
            "hidden_acceptance": {"ran": True, "passed": True},
        },
        {
            **base,
            "id": "clean",
            "review_kind": "clean",
            "hidden_acceptance": {"ran": True, "passed": False},
        },
        {
            **base,
            "id": "incomplete",
            "review_kind": "bug",
            "state": "failed",
            "hidden_acceptance": {"ran": True, "passed": True},
        },
    ]
    summary = benchmark.review_summary({"tasks": rows})["on"]
    assert summary["measured_runs"] == 2
    assert summary["invalid_or_incomplete_runs"] == 1
    assert summary["review_caught_and_fixed"] == 1
    assert summary["harmful_review_repairs"] == 1
    assert summary["prompt_tokens"] == 300
    assert summary["review_prompt_tokens"] == 90
    assert summary["review_token_share"] == 105 / 360
    assert summary["cost_usd"] is None
    assert summary["per_case"]["incomplete"]["hidden_passes"] == 0
    assert summary["per_case"]["incomplete"]["median_wall_seconds"] is None
    assert summary["per_case"]["incomplete"]["measured_runs"] == 0


def test_checkpoint_usage_retains_failed_review_cost_without_double_counting(
    tmp_path: Path,
) -> None:
    import json
    import sqlite3

    database = tmp_path / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE task_executions (execution_id TEXT, task_id TEXT, "
            "task_attempt INTEGER);"
            "CREATE TABLE execution_checkpoints (execution_id TEXT, "
            "checkpoint_json TEXT);"
            "INSERT INTO task_executions VALUES ('e', 't', 1);"
        )
        connection.execute(
            "INSERT INTO execution_checkpoints VALUES (?, ?)",
            (
                "e",
                json.dumps(
                    {
                        "usage_total": {
                            "prompt_tokens": 100,
                            "output_tokens": 20,
                            "review_prompt_tokens": 30,
                            "review_output_tokens": 5,
                        }
                    }
                ),
            ),
        )
    usage = benchmark.durable_usage(database, "t")
    assert usage["prompt_tokens"] + usage["output_tokens"] == 120
    assert usage["review_prompt_tokens"] + usage["review_output_tokens"] == 35
    assert benchmark.durable_usage(database, "missing") == {}


def test_event_timestamps_are_taken_from_database_not_provider_payload(
    tmp_path: Path,
) -> None:
    import json
    import sqlite3

    database = tmp_path / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE task_events (sequence INTEGER, task_id TEXT, "
            "event_type TEXT, payload_json TEXT, created_at INTEGER);"
        )
        connection.execute(
            "INSERT INTO task_events VALUES (1, 't', 'review.started', ?, 1000)",
            (json.dumps({"_benchmark_created_at": 999999999}),),
        )
        connection.execute(
            "INSERT INTO task_events VALUES (2, 't', 'review.finished', ?, 3500)",
            (json.dumps({"state": "completed", "findings": 0}),),
        )
    assert (
        benchmark.summarize_events(benchmark._events(database, "t")).review_seconds
        == 2.5
    )


def test_structured_patch_seed_and_noop_repair_are_measured() -> None:
    task = _corpus()[0]

    def patch(old, new, call_id):
        return [
            (
                "model.tool_call",
                {
                    "call_id": call_id,
                    "tool": "apply_patch",
                    "arguments": {
                        "path": task.seed_path,
                        "old_text": old,
                        "new_text": new,
                    },
                },
            ),
            ("model.tool_result", {"call_id": call_id, "tool": "apply_patch"}),
        ]

    seeded = patch(task.seed_before.strip(), task.seed_after.strip(), "seed")
    repaired = patch(task.seed_after, task.seed_before, "repair")
    assert benchmark.seed_observed(task, [*seeded, ("review.started", {}), *repaired])
    assert not benchmark.seed_observed(
        task, [*seeded, *repaired, ("review.started", {})]
    )
    # A repair equal to the original has no final diff; core correctly reports
    # verification not_required even though its public smoke checks ran.
    metrics = benchmark.Metrics(
        edits=2, outcome="completed", verification="not_required"
    )
    assert benchmark.checks(task, "completed", metrics)["state"]
    assert "verification" not in benchmark.checks(task, "completed", metrics)


def test_unavailable_provider_cannot_make_valid_latency_look_faster() -> None:
    good = {
        "id": "bug",
        "review_kind": "bug",
        "review_arm": "on",
        "seed_observed": True,
        "state": "completed",
        "passed": True,
        "hidden_acceptance": {"ran": True, "passed": True},
        "wall_seconds": 50,
        "prompt_tokens": 100,
        "output_tokens": 20,
    }
    unavailable = {
        **good,
        "state": "failed",
        "seed_observed": False,
        "wall_seconds": 1,
        "prompt_tokens": 0,
        "output_tokens": 0,
    }
    arm = benchmark.review_summary({"tasks": [good, unavailable, unavailable]})["on"]
    assert arm["median_wall_seconds"] == 50
    assert arm["per_case"]["bug"] == {
        "runs": 3,
        "measured_runs": 1,
        "invalid_or_incomplete_runs": 2,
        "hidden_passes": 1,
        "median_wall_seconds": 50,
        "median_tokens": 120,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        ("apply_patch", {"old_text": "size - 1", "new_text": "size"}),
        ("delete_file", {}),
        ("rename_file", {"source": "src/subject.py", "destination": "src/moved.py"}),
    ],
)
def test_seed_witness_replays_partial_repairs_delete_and_rename(mutation) -> None:
    task = _corpus()[0]
    tool, arguments = mutation
    events = [
        *_seed_edit(task, task.seed_after),
        (
            "model.tool_call",
            {
                "call_id": "repair",
                "tool": tool,
                "arguments": {"path": task.seed_path, **arguments},
            },
        ),
        ("model.tool_result", {"call_id": "repair"}),
        ("review.started", {}),
    ]
    assert not benchmark.seed_observed(task, events)
    events[-2] = ("model.tool_result", {"call_id": "repair", "is_error": True})
    assert benchmark.seed_observed(task, events)


def test_hidden_oracle_ignores_candidate_collection_hooks_and_skip_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task = _corpus()[0]
    root = tmp_path / "repo"
    shutil.copytree(benchmark.FIXTURES / task.fixture, root)
    (root / "conftest.py").write_text(
        "import pytest\ndef pytest_collection_modifyitems(items):\n"
        "    for item in items: item.add_marker(pytest.mark.skip())\n"
    )
    (root / "pytest.ini").write_text("[pytest]\naddopts = --ignore=hidden_acceptance\n")
    monkeypatch.setattr(benchmark, "available_sandbox", lambda: "test")
    monkeypatch.setattr(benchmark, "wrap", lambda kind, policy, argv, **kwargs: argv)
    target = root / task.seed_path
    original = target.read_text()
    target.write_text(original.replace(task.seed_before, task.seed_after, 1))
    assert benchmark.hidden_acceptance(task, root, ()) == {
        "ran": True,
        "passed": False,
        "returncode": 1,
        "reason": None,
    }
    target.write_text(original)
    assert benchmark.hidden_acceptance(task, root, ())["passed"] is True
    # An early exit must never be accepted as a passed hidden test.
    target.write_text("import os; os._exit(0)\n")
    result = benchmark.hidden_acceptance(task, root, ())
    assert result["ran"] is False and result["passed"] is False


@pytest.mark.parametrize("cleanup", ["finally", "context-manager"])
def test_resource_oracle_accepts_equivalent_cleanup_protocols(
    tmp_path: Path, monkeypatch, cleanup: str
) -> None:
    import runpy

    task = next(task for task in _corpus() if task.id == "review-resource")
    root = tmp_path / "repo"
    shutil.copytree(benchmark.FIXTURES / task.fixture, root)
    if cleanup == "context-manager":
        (root / task.seed_path).write_text(
            "def read_label(path):\n"
            "    with path.open() as handle:\n"
            "        return handle.read().strip()\n"
        )
    monkeypatch.setenv("BENCHMARK_REPOSITORY", str(root))
    hidden = benchmark.ROOT / "scripts/benchmark_acceptance" / task.acceptance
    oracle = runpy.run_path(str(hidden / "test_acceptance.py"))["test_contract"]
    oracle(tmp_path)


def test_resource_oracle_requires_cleanup_when_reading_raises(
    tmp_path: Path, monkeypatch
) -> None:
    import runpy

    task = next(task for task in _corpus() if task.id == "review-resource")
    root = tmp_path / "repo"
    shutil.copytree(benchmark.FIXTURES / task.fixture, root)
    (root / task.seed_path).write_text(
        "def read_label(path):\n"
        "    handle = path.open()\n"
        "    result = handle.read().strip()\n"
        "    handle.close()\n"
        "    return result\n"
    )
    monkeypatch.setenv("BENCHMARK_REPOSITORY", str(root))
    hidden = benchmark.ROOT / "scripts/benchmark_acceptance" / task.acceptance
    oracle = runpy.run_path(str(hidden / "test_acceptance.py"))["test_contract"]
    with pytest.raises(AssertionError):
        oracle(tmp_path)


def test_clean_inverted_seed_preserves_evaluation_order_and_exceptions() -> None:
    task = next(task for task in _corpus() if task.id == "review-clean-inverted")
    original = (benchmark.FIXTURES / task.fixture / task.seed_path).read_text()
    reference, candidate = {}, {}
    exec(original, reference)
    exec(original.replace(task.seed_before, task.seed_after, 1), candidate)

    def outcome(function, age, blocked):
        try:
            return ("value", function(age, blocked))
        except (TypeError, ValueError) as error:
            return (type(error), str(error))

    for age in (12, 18, 19, None, "19"):
        for blocked in (False, True):
            assert outcome(reference["eligible"], age, blocked) == outcome(
                candidate["eligible"], age, blocked
            )
