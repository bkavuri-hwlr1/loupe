from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
import time
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

    assert suite.version >= 2
    assert len(suite.revision) == 40
    assert {task.explore for task in suite.tasks} <= {"any", "never", "expected"}
    # Every task expects something beyond finishing.
    for task in suite.tasks:
        expectations = benchmark.checks(task, task.state, benchmark.Metrics())
        assert set(expectations) - {"state", "recorded_once", "one_task"}, task.id
    # The behavior matrix covers each kind of scenario.
    assert {t.interrupt for t in suite.tasks} >= {"cancel", "crash"}
    assert any(t.questions for t in suite.tasks)
    assert {t.verification for t in suite.tasks} >= {"passed", "failed"}
    assert {t.mode for t in suite.tasks} == {"plan", "normal", "auto"}


@pytest.mark.parametrize(
    ("tasks", "message"),
    [
        ('[[tasks]]\nid = "a"\nprompt = "q"\nmode = "chat"\n', "unknown mode"),
        ('[[tasks]]\nid = "a"\nprompt = "q"\nfacts = ["x"]\n', "lists of text"),
        ('[[tasks]]\nid = "a"\nprompt = "q"\nmin_facts = 0\n', "min_facts"),
        ('[[tasks]]\nid = "a"\nprompt = "q"\n' * 2, "unique ids"),
        ('[[tasks]]\nid = "a"\nprompt = "q"\nfixture = "../x"\n', "no fixture"),
        ('[[tasks]]\nid = "a"\nprompt = "q"\nfixture = "nope"\n', "no fixture"),
        (
            '[[tasks]]\nid = "a"\nprompt = "q"\n[tasks.checks.t]\nargv = ["x"]\n',
            "checks need a fixture",
        ),
        (
            '[[tasks]]\nid = "a"\nprompt = "q"\nfixture = "pager"\n'
            "[tasks.checks.t]\nargv = []\n",
            "argv list",
        ),
        ('[[tasks]]\nid = "a"\nprompt = "q"\nstate = "done"\n', "unknown state"),
        ('[[tasks]]\nid = "a"\nprompt = "q"\noutcomes = ["ok"]\n', "outcomes"),
        ('[[tasks]]\nid = "a"\nprompt = "q"\nedits = "many"\n', "unknown edits"),
        ('[[tasks]]\nid = "a"\nprompt = "q"\nquestions = -1\n', "questions"),
        ('[[tasks]]\nid = "a"\nprompt = "q"\nanswers = [""]\n', "answers"),
        ('[[tasks]]\nid = "a"\nprompt = "q"\nmax_main_turns = 0\n', "turns"),
        ('[[tasks]]\nid = "a"\nprompt = "q"\ninterrupt = "kill"\n', "interrupt"),
        (
            '[[tasks]]\nid = "a"\nprompt = "q"\nmode = "plan"\ninterrupt = "cancel"\n',
            "plan mode",
        ),
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
    assert result["checks"] == {
        "state": True,
        "recorded_once": True,
        "one_task": True,
        "facts": True,
        "explored": True,
    }
    assert result["facts_missed"] == ["third"]
    assert result["wall_seconds"] == 95.0
    assert result["main_tool_calls"] == 2
    assert result["prompt_tokens"] == 120_000
    assert result["helper_prompt_tokens"] == 100_000

    # A narrow question should not explore, and a failed task never passes.
    narrow = benchmark.Task(id="n", prompt="q", explore="never")
    failed = benchmark.task_result(narrow, "failed", "CLAIM_STALE", metrics, 1, 0)
    assert failed["passed"] is False
    assert failed["checks"] == {
        "state": False,
        "recorded_once": True,
        "one_task": True,
        "no_explore": False,
    }


def _behavior_events() -> list[tuple[str, dict[str, object]]]:
    return [
        ("model.turn.completed", {"usage": {"prompt_tokens": 900}}),
        ("question.asked", {"question_id": "q"}),
        ("model.tool_result", {"call_id": "a", "tool": "read_file"}),
        ("model.tool_result", {"call_id": "b", "tool": "write_file"}),
        ("model.tool_result", {"call_id": "c", "tool": "write_file", "is_error": True}),
        # A long result arrives in numbered parts; only a repeated part counts.
        ("model.tool_result", {"call_id": "d", "tool": "run_check", "part": 0}),
        ("model.tool_result", {"call_id": "d", "tool": "run_check", "part": 1}),
        ("execution.resuming", {"execution_id": "x"}),
        ("model.tool_result", {"call_id": "b", "tool": "write_file"}),
        (
            "workflow.awaiting_review",
            {
                "completion_outcome": "partial",
                "verification": "failed",
                "file_count": 2,
            },
        ),
        ("model.finished", {"message_id": "m", "answer": "Both tests contradict."}),
    ]


def test_behavior_is_measured_from_events() -> None:
    metrics = benchmark.summarize_events(_behavior_events())

    assert metrics.questions == 1
    assert metrics.edits == 2
    # The answer event carries no usage here, so the turns' usage is used.
    assert metrics.usage == {"prompt_tokens": 900}
    assert metrics.repeated_results == 1
    assert metrics.repeated_answers == 0
    assert metrics.resumed is True
    assert metrics.verification == "failed"
    assert metrics.files_kept == 2
    assert benchmark.edit_seen(_behavior_events())
    assert not benchmark.edit_seen(_behavior_events()[:3])
    assert benchmark.awaiting_answer(_behavior_events())
    answered = [*_behavior_events(), ("question.answered", {"question_id": "q"})]
    assert not benchmark.awaiting_answer(answered)


def test_behavior_expectations_are_checked() -> None:
    metrics = benchmark.summarize_events(_behavior_events())
    metrics.outcome = "partial"
    task = benchmark.Task(
        id="fix",
        prompt="q",
        mode="normal",
        state="reviewing",
        outcomes=("partial", "blocked"),
        edits="some",
        verification="failed",
        questions=1,
        min_answer_characters=10,
        max_main_turns=1,
        interrupt="crash",
    )

    assert benchmark.checks(task, "reviewing", metrics, 1, True) == {
        "state": True,
        # The repeated write result after the restart fails the task.
        "recorded_once": False,
        "one_task": True,
        "outcome": True,
        "edited": True,
        "verification": True,
        "questions": True,
        "answer_length": True,
        "turns": True,
        "interrupted": True,
        "resumed": True,
    }
    quiet = benchmark.Task(id="q", prompt="q", edits="none", interrupt="cancel")
    assert benchmark.checks(quiet, "cancelled", metrics, 2, False) == {
        "state": False,
        "recorded_once": False,
        "one_task": False,
        "no_edits": False,
        "interrupted": False,
        "edits_kept": True,
    }


def test_fixture_tasks_get_their_own_repository_and_checks(tmp_path: Path) -> None:
    repository = benchmark._fixture(tmp_path / "run" / "fix", "textutil")

    assert (repository / "src/textutil.py").is_file()
    status = subprocess.run(
        ["git", "-C", str(repository), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""
    checks = {"test": {"argv": ["{python}", "-m", "pytest"], "timeout": 120}}
    assert benchmark.checks_toml(checks, "/venv/bin/python") == (
        '[checks."test"]\nargv = ["/venv/bin/python", "-m", "pytest"]\ntimeout = 120\n'
    )


def test_run_settings_are_checked_before_any_request(tmp_path: Path) -> None:
    path = benchmark.write_config(
        tmp_path, ["explore_effort=low", "explore=false", "web_fetch=allow"], "bench"
    )

    # Unattended runs allow sandboxed commands and fetch no pages by default.
    assert path.read_text() == (
        '[agent]\ncommands = "allow"\nweb_fetch = "allow"\n'
        'explore_effort = "low"\nexplore = false\n'
    )
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


def _store(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.executescript(
        "CREATE TABLE tasks (task_id TEXT, state TEXT, failure_code TEXT);"
        "CREATE TABLE task_events (sequence INTEGER PRIMARY KEY, task_id TEXT,"
        " event_type TEXT, payload_json TEXT DEFAULT '{}');"
        "INSERT INTO tasks VALUES ('t', 'running', NULL);"
    )
    return connection


def _ask(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO task_events (task_id, event_type) VALUES ('t', 'question.asked')"
    )


def test_questions_get_the_next_answer_only_when_asked(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    connection = _store(database)
    _ask(connection)
    replies: list[str] = []

    def reply(answer: str) -> None:
        replies.append(answer)
        connection.execute(
            "INSERT INTO task_events (task_id, event_type) "
            "VALUES ('t', 'question.answered')"
        )
        if len(replies) == 1:
            _ask(connection)
        else:
            connection.execute("UPDATE tasks SET state = 'reviewing'")

    settled = benchmark._settle(
        database,
        "t",
        time.time() + 30,
        answers=("first", "second", "unused"),
        reply=reply,
        cancel=lambda: pytest.fail("an answered task is not cancelled"),
    )

    assert settled == ("reviewing", None, False)
    assert replies == ["first", "second"]


def test_a_question_without_an_answer_cancels_the_task(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    connection = _store(database)
    _ask(connection)

    def cancel() -> None:
        connection.execute("UPDATE tasks SET state = 'cancelled'")

    settled = benchmark._settle(
        database,
        "t",
        time.time() + 30,
        answers=(),
        reply=lambda answer: pytest.fail("there is nothing to answer with"),
        cancel=cancel,
    )

    assert settled == ("cancelled", None, True)
