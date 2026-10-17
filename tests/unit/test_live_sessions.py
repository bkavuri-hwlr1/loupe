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


@pytest.mark.skipif(available_sandbox() is None, reason="needs a working sandbox")
@pytest.mark.parametrize(
    ("experiment_id", "untouched_passed", "total"),
    [("toolkit", 0, 16), ("toolkit-runtime", 1, 17)],
)
def test_toolkit_hidden_tests_fail_untouched_and_pass_reference(
    tmp_path: Path, experiment_id: str, untouched_passed: int, total: int
) -> None:
    version, experiments = sessions.load_experiments()
    assert version >= 3
    experiment = next(e for e in experiments if e.id == experiment_id)
    repository = benchmark._fixture(tmp_path / experiment_id, experiment.fixture)
    sessions.prepare_runtime(repository, experiment)
    tests = sessions.ACCEPTANCE / experiment.acceptance
    untouched = sessions.acceptance(repository, tests, sys.executable, ())
    assert untouched["ran"] and untouched["own_tests_passed"]
    assert untouched["passed"] == untouched_passed
    assert untouched["total"] == total
    text = repository / "src/toolkit/text.py"
    text.write_text(
        text.read_text()
        + """


def normalize_space(value):
    return " ".join(value.split())


def slug(value):
    import re
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
"""
    )
    numbers = repository / "src/toolkit/numbers.py"
    numbers.write_text(
        numbers.read_text()
        + """


def mean(values):
    items = list(values)
    if not items:
        raise ValueError("empty input")
    return float(sum(items) / len(items))


def clamp(value, lower, upper):
    if lower > upper:
        raise ValueError("reversed bounds")
    return max(lower, min(upper, value))
"""
    )
    registry = repository / experiment.disruption.edit_path
    registry.write_text(
        registry.read_text().replace("False", "True")
        + experiment.disruption.edit_append
    )
    solution = sessions.acceptance(repository, tests, sys.executable, ())
    assert solution["ran"] and solution["own_tests_passed"]
    assert solution["passed"] == solution["total"] == total


def test_toolkit_registry_parts_merge_without_conflict() -> None:
    # A three-way merge treats changes on adjacent lines as one conflict, so
    # the two toolkit parts must flip registry flags on separate lines.
    from llm_cli.workspace.merge import merge_text

    registry = benchmark.FIXTURES / "toolkit/src/toolkit/registry.py"
    base = registry.read_bytes()
    text = base.replace(b'"text": False', b'"text": True')
    numbers = base.replace(b'"numbers": False', b'"numbers": True')
    merged = merge_text(base, text, numbers)
    assert merged.conflicts == 0
    assert b'"text": True' in merged.content and b'"numbers": True' in merged.content


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


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ("shared", ["shared", "shared"]),
        ("isolated", ["isolated", "isolated"]),
        ("mixed", ["shared", "isolated"]),
    ],
)
def test_workspace_mapping_is_explicit(selection: str, expected: list[str]) -> None:
    assert [sessions.lane_workspace(selection, index) for index in range(2)] == expected
    with pytest.raises(ValueError, match="workspace"):
        sessions.lane_workspace("not-a-mode", 0)


def test_runtime_canary_stays_ignored_and_out_of_git_baseline(tmp_path: Path) -> None:
    from llm_cli.agent.source_policy import contains_secret_material

    _, experiments = sessions.load_experiments()
    experiment = next(e for e in experiments if e.id == "toolkit-runtime")
    repository = benchmark._fixture(tmp_path / "runtime", experiment.fixture)
    before = sessions.git_invariants(repository)
    canary = sessions.prepare_runtime(repository, experiment)
    assert contains_secret_material(canary)
    assert not (repository / ".env").stat().st_mode & 0o077
    assert sessions.git_invariants(repository) == before


@pytest.mark.parametrize("workspace", ["shared", "isolated"])
def test_workspace_and_exact_runtime_consent_reach_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: str
) -> None:
    import io
    from types import SimpleNamespace

    from llm_cli.paths import AppPaths

    started = []
    stream = io.StringIO()

    def start(argv, **kwargs):
        started.append((argv, kwargs))
        return SimpleNamespace(stdin=stream)

    monkeypatch.setattr(sessions.subprocess, "Popen", start)
    context = sessions.Context(
        AppPaths.resolve("fixture", home=tmp_path, environ={}),
        ["loupe"],
        ["loupe", "chat", "--plain"],
        {"LLM_COORD_RUNTIME_DIR": "isolated-socket"},
        tmp_path,
        tmp_path,
        10,
    )
    lane = sessions.Lane("text", "Do text.", workspace=workspace)
    sessions._start(context, tmp_path / "repo", "auto", lane, "Do text.", (".env",))
    argv, options = started[0]
    if workspace == "isolated":
        assert argv[-4:] == ["--workspace", "isolated", "--approve-secret", ".env"]
    else:
        assert argv[-2:] == ["--workspace", "shared"]
        assert "--approve-secret" not in argv
    assert options["env"] == context.environment
    assert len(lane.processes) == 1


@pytest.mark.parametrize("leak", [None, "database", "log", "session", "managed"])
def test_isolation_audit_reports_leaks_without_returning_canary(
    tmp_path: Path, leak: str | None
) -> None:
    import sqlite3

    from llm_cli.paths import AppPaths

    _, experiments = sessions.load_experiments()
    experiment = next(e for e in experiments if e.id == "toolkit-runtime")
    repository = benchmark._fixture(tmp_path / "runtime", experiment.fixture)
    before = sessions.git_invariants(repository)
    canary = sessions.prepare_runtime(repository, experiment)
    paths = AppPaths.resolve("fixture", home=tmp_path, environ={})
    paths.ensure()
    with sqlite3.connect(paths.control_db) as con:
        con.execute("CREATE TABLE sessions(session_id,workspace_mode,state)")
        con.execute("INSERT INTO sessions VALUES ('session','isolated','closed')")
        con.execute("CREATE TABLE check_runs(task_id,name,state,output)")
        con.execute(
            "INSERT INTO check_runs VALUES (?,?,?,?)",
            ("task", "runtime", "passed", "C6 runtime read passed"),
        )
        if leak == "database":
            con.execute("CREATE TABLE accidental_content(value)")
            con.execute("INSERT INTO accidental_content VALUES (?)", (canary.decode(),))
    if leak == "log":
        paths.log_file.write_bytes(canary)
    if leak == "session":
        (paths.session_dir / "accidental").write_bytes(canary)
    if leak == "managed":
        target = paths.data_dir / "isolated-worktrees/private/.env"
        target.parent.mkdir(parents=True)
        target.write_bytes(canary)
    lane = sessions.Lane("text", "Do text.", task_ids=["task"], workspace="isolated")
    row = sessions.TaskRow("task", "Do text.", "completed", None, "session", 0, 1)
    audit = sessions.audit_run(
        paths,
        repository,
        before,
        canary,
        [lane],
        {"task": row},
        "clean",
        None,
        None,
        True,
    )
    assert all(audit.values()) == (leak is None)
    if leak is not None:
        key = {
            "database": "database_has_no_canary",
            "log": "daemon_log_has_no_canary",
            "session": "session_files_have_no_canary",
            "managed": "managed_env_copies_absent",
        }[leak]
        assert audit[key] is False
    assert canary.decode() not in repr(audit)


def test_runtime_fixture_requires_exact_consent_and_configured_check(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="canary requires"):
        sessions.load_experiments(_experiment_file(tmp_path, runtime_canary="true"))


def test_sandbox_setup_failure_is_not_scored_as_product_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    _, experiments = sessions.load_experiments()
    experiment = next(e for e in experiments if e.id == "toolkit-runtime")
    repository = benchmark._fixture(tmp_path / "runtime", experiment.fixture)
    sessions.prepare_runtime(repository, experiment)
    monkeypatch.setattr(sessions, "available_sandbox", lambda: "seatbelt")
    monkeypatch.setattr(
        sessions.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 126, "", "cannot execute"
        ),
    )
    result = sessions.acceptance(
        repository, sessions.ACCEPTANCE / experiment.acceptance, sys.executable, ()
    )
    assert result == {"ran": False, "reason": "sandboxed interpreter could not execute"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor lock semantics")
def test_database_canary_scan_preserves_live_reader_locks(tmp_path: Path) -> None:
    import json
    import sqlite3
    import subprocess
    from contextlib import closing

    database = tmp_path / "control.sqlite3"
    worker = """
import json, sqlite3, sys
with sqlite3.connect(sys.argv[1], timeout=0.1) as connection:
    connection.execute("UPDATE data SET content = printf('%08000d', 2) WHERE id=1")
    connection.commit()
    print(json.dumps(connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()))
"""

    def checkpoint() -> list[int]:
        return json.loads(
            subprocess.check_output([sys.executable, "-c", worker, str(database)])
        )

    with (
        closing(sqlite3.connect(database)) as writer,
        closing(sqlite3.connect(database)) as reader,
    ):
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE data(id INTEGER PRIMARY KEY, content)")
        writer.execute("INSERT INTO data VALUES (1, printf('%08000d', 1))")
        writer.commit()
        reader.execute("BEGIN")
        before = reader.execute("SELECT content FROM data").fetchone()
        assert checkpoint()[0] == 1  # This live reader pins its old WAL snapshot.
        assert not sessions._database_contains_canary(database, b"not-present-canary")
        assert checkpoint()[0] == 1  # Raw scans must not release the reader's locks.
        assert reader.in_transaction
        assert reader.execute("SELECT content FROM data").fetchone() == before


def test_completed_results_survive_a_later_run_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse
    import json
    import sqlite3
    import subprocess

    for name in (
        "LLM_COORD_DATA_HOME",
        "LLM_COORD_STATE_HOME",
        "LLM_COORD_RUNTIME_DIR",
    ):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setattr(sessions, "available_sandbox", lambda: "seatbelt")
    monkeypatch.setattr(sessions.benchmark, "write_config", lambda *args: None)
    monkeypatch.setattr(sessions.benchmark, "_loupe", lambda: ["loupe"])
    monkeypatch.setattr(sessions, "describe", lambda result: "completed pilot")
    monkeypatch.setattr(
        sessions.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "head", ""),
    )
    completed = {"passed": True, "prompt_tokens": 100, "output_tokens": 10}
    calls = 0

    def run_arm(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return completed
        raise sqlite3.DatabaseError("private-canary-must-not-be-recorded")

    monkeypatch.setattr(sessions, "run_arm", run_arm)
    output = tmp_path / "measurements.json"
    arguments = argparse.Namespace(
        profile="fixture",
        experiments=sessions.EXPERIMENTS,
        only=["toolkit-runtime"],
        provider="codex",
        model="fixture",
        effort="low",
        set=[],
        workspace="isolated",
        timeout=30,
        repeat=1,
        scenarios=["clean", "disrupted"],
        arms=["pair"],
        budget_tokens=1_000_000,
        out=output,
    )
    with pytest.raises(sqlite3.DatabaseError):
        sessions.run(arguments)
    document = json.loads(output.read_text())
    assert document["runs"] == [completed]
    assert document["errors"] == [
        {
            "kind": "DatabaseError",
            "experiment": "toolkit-runtime",
            "arm": "pair",
            "scenario": "disrupted",
            "repetition": 1,
        }
    ]
    assert "private-canary" not in output.read_text()


@pytest.mark.skipif(available_sandbox() is None, reason="needs a working sandbox")
@pytest.mark.parametrize("interference", ["hook", "config"])
def test_hidden_acceptance_ignores_candidate_collection_controls(
    tmp_path: Path, interference: str
) -> None:
    _, experiments = sessions.load_experiments()
    experiment = next(e for e in experiments if e.id == "toolkit-runtime")
    repository = benchmark._fixture(tmp_path / "runtime", experiment.fixture)
    sessions.prepare_runtime(repository, experiment)
    if interference == "hook":
        (repository / "conftest.py").write_text(
            "import pytest\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for item in items:\n"
            "        item.add_marker(pytest.mark.skip(reason='candidate hook'))\n"
        )
    else:
        (repository / "pytest.ini").write_text(
            "[pytest]\naddopts = -k test_operator_hand_edit_is_preserved\n"
        )
    result = sessions.acceptance(
        repository, sessions.ACCEPTANCE / experiment.acceptance, sys.executable, ()
    )
    assert result["ran"]
    # The untouched fixture's runtime contract executes and passes; its other
    # hidden contracts execute and fail, regardless of candidate hooks/config.
    assert result["passed"] == 1
    assert result["total"] == 17
    assert result["groups"]["runtime"] == [1, 1]
    if interference == "hook":
        # The ordinary test run retains the project's collection behavior.
        assert result["own_tests_passed"]
        assert "skipped" in result["own_tests"]
