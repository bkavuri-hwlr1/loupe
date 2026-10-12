from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from llm_cli.execution.sandbox import available_sandbox

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their annotations through the module's registered
    # entry, and live_sessions imports live_benchmark by name.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


benchmark = sys.modules.get("live_benchmark") or _load("live_benchmark")
sessions = _load("live_sessions")


def test_the_shipped_experiments_are_valid() -> None:
    version, experiments = sessions.load_experiments()

    assert version >= 1
    for experiment in experiments:
        tests = sessions.acceptance_tests(sessions.ACCEPTANCE / experiment.acceptance)
        # Every part, their combination, and the operator's edit are checked.
        groups = {sessions.group(name) for name in tests}
        assert groups >= {part.name for part in experiment.parts}
        assert {"both", "operator"} <= groups
        # The single arm asks for every part in one line.
        [(name, prompt)] = experiment.lanes("single")
        assert name == "all" and "\n" not in prompt
        assert all(part.prompt in prompt for part in experiment.parts)
        assert [lane for lane, _ in experiment.lanes("pair")] == [
            part.name for part in experiment.parts
        ]


def _experiment_file(tmp_path: Path, **changes: object) -> Path:
    fields = {
        "id": '"x"',
        "fixture": '"shop"',
        "acceptance": '"shop"',
        "mode": '"auto"',
        "follow_up": '"Again ({reason}): {prompt}"',
        "max_follow_ups": "1",
    }
    fields.update({key: str(value) for key, value in changes.items()})
    lines = ["version = 1", "[[experiments]]"]
    lines += [f"{key} = {value}" for key, value in fields.items()]
    lines += [
        "[[experiments.parts]]",
        'name = "a"',
        'prompt = "Do a."',
        "[[experiments.parts]]",
        'name = "b"',
        'prompt = "Do b."',
        "[experiments.disruption]",
        'edit_path = "src/shop/cart.py"',
        'edit_append = "\\n# hand edit\\n"',
    ]
    path = tmp_path / "experiments.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_a_valid_experiment_file_loads(tmp_path: Path) -> None:
    _, (experiment,) = sessions.load_experiments(_experiment_file(tmp_path))

    assert experiment.lanes("single") == [
        ("all", "Make these 2 changes. Change 1: Do a. Change 2: Do b.")
    ]
    assert experiment.disruption.crash


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"fixture": '"nope"'}, "no fixture"),
        ({"acceptance": '"nope"'}, "no acceptance tests"),
        ({"mode": '"plan"'}, "mode must be"),
        ({"follow_up": '"Again."'}, "must contain {prompt}"),
        ({"follow_up": '"{prompt} {why}"'}, "only takes"),
        ({"follow_up": '"""\nAgain:\n{prompt}"""'}, "single lines"),
        ({"max_follow_ups": "-1"}, "must be a count"),
    ],
)
def test_experiment_mistakes_are_refused(
    tmp_path: Path, changes: dict[str, str], problem: str
) -> None:
    with pytest.raises(ValueError, match=problem):
        sessions.load_experiments(_experiment_file(tmp_path, **changes))


def test_edited_paths_come_from_successful_edits() -> None:
    events = [
        (
            "model.tool_call",
            {"call_id": "1", "tool": "apply_patch", "arguments": {"path": "./a.py"}},
        ),
        ("model.tool_result", {"call_id": "1", "tool": "apply_patch"}),
        (
            "model.tool_call",
            {"call_id": "2", "tool": "write_file", "arguments": {"path": "b.py"}},
        ),
        (
            "model.tool_result",
            {"call_id": "2", "tool": "write_file", "is_error": True},
        ),
        (
            "model.tool_call",
            {
                "call_id": "3",
                "tool": "rename_file",
                "arguments": {"source": "c.py", "destination": "d/c.py"},
            },
        ),
        ("model.tool_result", {"call_id": "3", "tool": "rename_file"}),
        (
            "model.tool_call",
            {"call_id": "4", "tool": "read_file", "arguments": {"path": "e.py"}},
        ),
        ("model.tool_result", {"call_id": "4", "tool": "read_file"}),
    ]

    assert sessions.edited_paths(events) == {"a.py", "c.py", "d/c.py"}


def test_tasks_are_matched_to_the_prompt_that_started_them() -> None:
    lane = sessions.Lane("stock", "Add stock.")
    lane.texts.append("Add   stock limits " + "x" * 600)

    # Titles keep 500 characters and the chat may normalize spacing.
    assert lane.claim("Add stock limits " + "x" * 483)
    assert not lane.claim("Add discount codes")
    lane.task_ids.append("chat_1")
    assert not lane.claim("Add stock limits " + "x" * 483)


def test_only_work_that_was_not_applied_is_asked_again() -> None:
    assert sessions.not_applied_reason("completed", None) is None
    assert sessions.not_applied_reason("failed", "PATH_BASE_MISMATCH") == (
        "the files changed before it could be applied"
    )
    assert sessions.not_applied_reason("reviewing", "REVIEW_REQUIRED") == (
        "it was held for review"
    )
    assert sessions.not_applied_reason("operator_attention", None) == (
        "it ended operator attention"
    )


def test_acceptance_is_scored_by_group() -> None:
    report = (
        "<testsuites><testsuite>"
        '<testcase name="test_discount_a"/>'
        '<testcase name="test_discount_b"><failure message="no"/></testcase>'
        '<testcase name="test_stock_a"><error message="no"/></testcase>'
        "</testsuite></testsuites>"
    )
    outcomes = sessions.junit_outcomes(report)
    expected = ["test_discount_a", "test_discount_b", "test_stock_a", "test_both_a"]

    # A test missing from the report, such as after an import error, failed.
    assert sessions.score(expected, outcomes) == {
        "passed": 1,
        "total": 4,
        "groups": {"discount": [1, 2], "stock": [0, 1], "both": [0, 1]},
        "failed": ["test_discount_b", "test_stock_a", "test_both_a"],
    }


@pytest.mark.skipif(available_sandbox() is None, reason="needs a working sandbox")
def test_hidden_tests_need_the_requested_changes(tmp_path: Path) -> None:
    _, experiments = sessions.load_experiments()
    experiment = next(e for e in experiments if e.id == "shop")
    repository = benchmark._fixture(tmp_path / "shop", experiment.fixture)
    tests = sessions.ACCEPTANCE / experiment.acceptance

    untouched = sessions.acceptance(repository, tests, sys.executable, ())
    unedited = sessions.acceptance(
        repository, tests, sys.executable, (), skip=(sessions.OPERATOR_GROUP,)
    )
    with open(repository / experiment.disruption.edit_path, "a") as handle:
        handle.write(experiment.disruption.edit_append)
    edited = sessions.acceptance(repository, tests, sys.executable, ())

    assert untouched["ran"] and untouched["own_tests_passed"]
    # Only the guard that a cart without a code is unchanged passes.
    assert untouched["passed"] == 1
    assert untouched["groups"]["operator"] == [0, 1]
    # Without a hand edit, its test is not scored.
    assert "operator" not in unedited["groups"]
    assert unedited["total"] == untouched["total"] - 1
    # The operator's edit is valid on its own and is what its test checks.
    assert edited["own_tests_passed"]
    assert edited["groups"]["operator"] == [1, 1]
    assert edited["passed"] == 2
