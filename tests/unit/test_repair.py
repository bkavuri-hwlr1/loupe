"""Repair limits: a failing check gets a bounded number of fixes."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from llm_cli.agent.limits import ExecutionLimits
from llm_cli.agent.repair import RepairTracker, failure_signature
from llm_cli.agent.tools import ToolBroker, ToolOutcome
from llm_cli.cli.render import EventRenderer

PYTEST_FAILURE = """\
rootdir: /tmp/check-worktrees/check_01a1192561d63a796a3d072d43a79c05/repo
tests/test_a.py F
E   assert 2 == 3
tmp_path: /private/var/folders/x/pytest-of-me/pytest-{n}/test_a0
object at 0x10{n}fa2b0
==== 1 failed, 4 passed in {n}.2{n}s ===="""


def _record(tracker: RepairTracker, state: str, output: str = "x") -> str | None:
    return tracker.record("tests", state, output, attempts=3)


def test_each_failed_repair_is_counted_until_the_limit() -> None:
    tracker = RepairTracker()

    assert _record(tracker, "failed", "one") == (
        "[tests failed. Fix the cause and run it again; 3 repair attempts left.]"
    )
    assert _record(tracker, "failed", "two") == (
        "[Repair attempt 1 of 3 for tests failed; 2 repair attempts left.]"
    )
    assert _record(tracker, "timed_out", "three") == (
        "[Repair attempt 2 of 3 for tests failed; 1 repair attempt left.]"
    )
    assert tracker.exhausted is None
    stopped = _record(tracker, "failed", "four")
    assert stopped is not None and stopped.startswith(
        "[Repair limit reached: tests failed 4 times in a row, after 3 repair "
        "attempts. Make no more edits or check runs."
    )
    assert tracker.exhausted == "tests"
    # Nothing more is counted once the repair has stopped.
    assert _record(tracker, "failed", "five") is None
    assert _record(tracker, "passed") is None
    assert tracker.exhausted == "tests"


def test_a_pass_resets_the_count_and_unusable_runs_do_not_count() -> None:
    tracker = RepairTracker()
    _record(tracker, "failed", "one")
    _record(tracker, "failed", "two")

    for state in ("error", "cancelled", "uncertain"):
        assert _record(tracker, state, "infrastructure") is None
    assert tracker.failures == {"tests": 2}
    assert _record(tracker, "passed") is None
    assert tracker.failures == {}
    assert _record(tracker, "failed", "one").startswith("[tests failed.")  # type: ignore[union-attr]
    # Each check has its own count.
    assert tracker.record("lint", "failed", "x", attempts=3) is not None
    assert tracker.failures == {"tests": 1, "lint": 1}


def test_the_same_failure_three_times_stops_the_repair_early() -> None:
    tracker = RepairTracker()
    runs = [PYTEST_FAILURE.format(n=n) for n in (1, 7, 9)]

    assert _record(tracker, "failed", runs[0]) is not None
    assert _record(tracker, "failed", runs[1]) == (
        "[Repair attempt 1 of 3 for tests failed with the same failure as before; "
        "2 repair attempts left.]"
    )
    stopped = _record(tracker, "failed", runs[2])
    assert stopped is not None
    assert "tests failed 3 times in a row, with the same failure the last 3 " in (
        stopped
    )
    assert tracker.exhausted == "tests"


def test_signatures_ignore_timings_and_ids_but_not_results() -> None:
    assert failure_signature(PYTEST_FAILURE.format(n=1)) == failure_signature(
        PYTEST_FAILURE.format(n=8)
    )
    assert failure_signature("1 failed, 4 passed") != failure_signature(
        "2 failed, 3 passed"
    )
    assert failure_signature("tests/test_a.py:10") != failure_signature(
        "tests/test_a.py:12"
    )


def test_repair_state_round_trips_and_rejects_malformed_counts() -> None:
    tracker = RepairTracker()
    for output in ("a", "a", "a"):
        _record(tracker, "failed", output)
    saved = tracker.to_dict()

    restored = RepairTracker.from_dict(saved)
    assert restored == tracker
    assert RepairTracker.from_dict(None) == RepairTracker()
    for bad in (
        "nope",
        {**saved, "extra": 1},
        {**saved, "failures": {"tests": 0}},
        {**saved, "failures": {"tests": True}},
        {**saved, "signatures": {"tests": "short"}},
        {**saved, "unchanged": {"tests": 9}},
        {**saved, "exhausted": "lint"},
    ):
        with pytest.raises(ValueError, match="repair state"):
            RepairTracker.from_dict(bad)


def _broker(
    tmp_path: Path, outputs: list[str], *, attempts: int = 3
) -> tuple[ToolBroker, list[tuple[str, dict[str, object]]]]:
    """A broker whose single check fails with each of ``outputs`` in turn."""

    events: list[tuple[str, dict[str, object]]] = []
    broker = ToolBroker(
        tmp_path,
        ("*",),
        limits=ExecutionLimits(max_repair_attempts=attempts),
        on_event=lambda kind, payload: events.append((kind, payload)),
    )
    remaining = iter(outputs)

    def check(name: str) -> ToolOutcome:
        output = next(remaining)
        broker.record_check(name, "failed", output)
        return ToolOutcome(f"{name}: failed (exit 1, 0.1s)\n{output}\n", True)

    def gate() -> ToolOutcome:
        failed = check("tests")
        return ToolOutcome("Required checks did not pass.\n" + failed.content, True)

    broker.check_runner = check
    broker.finish_gate = gate
    return broker, events


def test_the_broker_stops_edits_and_checks_and_finishes_partial(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    broker, events = _broker(tmp_path, ["first", "second"], attempts=1)

    first = broker.invoke("run_check", {"name": "tests"})
    assert first.is_error
    assert first.content == (
        "tests: failed (exit 1, 0.1s)\nfirst\n\n"
        "[tests failed. Fix the cause and run it again; 1 repair attempt left.]"
    )
    # The finish gate's runs count too, and its refusal carries the note.
    refused = broker.complete(answer="fixed")
    assert refused.is_error and "[Repair limit reached: tests failed 2" in (
        refused.content
    )
    assert events[-1] == (
        "repair.exhausted",
        {"check": "tests", "failures": 2, "unchanged": False},
    )

    for name, arguments in (
        ("write_file", {"path": "a.py", "content": "value = 2\n"}),
        ("apply_patch", {"path": "a.py", "old_text": "1", "new_text": "2"}),
        ("delete_file", {"path": "a.py"}),
        ("run_check", {"name": "tests"}),
    ):
        outcome = broker.invoke(name, arguments)
        assert outcome.is_error and "repair limit for tests was reached" in (
            outcome.content
        )
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "value = 1\n"
    # Tools that change nothing stay available for writing the answer.
    steps = [{"step": "Explain what still fails", "status": "in_progress"}]
    assert not broker.invoke("update_plan", {"steps": steps}).is_error

    # Restarting keeps the limit.
    resumed, _ = _broker(tmp_path, [], attempts=1)
    resumed.restore_usage(broker.usage_snapshot())
    assert resumed.invoke("run_check", {"name": "tests"}).is_error

    # A natural answer or finish_task "completed" is recorded as partial.
    finished = broker.complete(answer="value is still wrong")
    assert finished.content == (
        "task marked finished as partial: tests still fails after the repair limit"
    )
    assert broker.usage.finished and broker.usage.outcome == "partial"


def test_an_explicit_blocked_outcome_is_kept(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, ["a", "a", "a"])
    for _ in range(3):
        broker.invoke("run_check", {"name": "tests"})

    assert broker.complete(answer="blocked", outcome="blocked").content == (
        "task marked finished"
    )
    assert broker.usage.outcome == "blocked"


def test_notes_are_never_clipped_from_long_check_output(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path, ["x" * 5_000])
    broker.limits = ExecutionLimits(max_tool_output_bytes=1_000)

    outcome = broker.invoke("run_check", {"name": "tests"})
    assert "output truncated" in outcome.content
    assert outcome.content.endswith("3 repair attempts left.]")


def test_the_terminal_says_why_repair_stopped() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output, plain=True)
    renderer.render(
        {
            "event_type": "repair.exhausted",
            "payload": {"check": "tests", "failures": 3, "unchanged": True},
        }
    )
    renderer.finish()
    assert (
        "! stopped repairing tests after 3 failed runs with no change in the "
        "failure" in output.getvalue()
    )
