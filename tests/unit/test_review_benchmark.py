"""The planted-review benchmark requires observed findings and catches its bug."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from test_live_benchmark import benchmark


def test_planted_review_task_requires_one_observed_successful_review() -> None:
    task = next(
        t
        for t in benchmark.load_suite().tasks
        if t.id == "independent-review-planted-bug"
    )
    assert task.min_review_findings == 1
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
