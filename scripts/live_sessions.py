"""Compare one agent with sessions working at once in one shared checkout.

Run with `uv run --extra openai python scripts/live_sessions.py --profile NAME`.

An experiment in `live_sessions.toml` splits a multi-file change into parts.
The "single" arm sends every part to one session in one prompt; the "pair"
arm starts one session per part, all at once, in the same checkout. Both arms
start from a fresh copy of the same fixture and run with a real model, so
every run is billed, and NAME must be a signed-in profile other than
`default`, whose background service is restarted with its own configuration
(see `live_benchmark.py`).

The "disrupted" scenario also edits a file by hand once an agent has edited
it, as someone working in an editor would, and kills and restarts the
background service once every session has edited. The "clean" scenario does
neither.

Nobody is at the terminal, so this script plays the operator: it answers any
question with the experiment's answer, and when a session's work is not
applied it asks again in a new session, up to `max_follow_ups` times. Each is
counted as an intervention. When every session has stopped, hidden acceptance
tests run in a sandbox against a copy of the checkout, together with the
checkout's own tests. Results are written as JSON.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ElementTree
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import live_benchmark as benchmark

from llm_cli.cli.session import remove_session_secret, session_resume_secret
from llm_cli.execution.sandbox import (
    SandboxPolicy,
    available_sandbox,
    protected_home_paths,
    wrap,
)
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient

EXPERIMENTS = Path(__file__).with_name("live_sessions.toml")
ACCEPTANCE = Path(__file__).with_name("benchmark_acceptance")
ARMS = ("single", "pair")
# Acceptance tests named test_operator_* check the disruption's hand edit.
OPERATOR_GROUP = "operator"
SCENARIOS = ("clean", "disrupted")
WORKSPACES = ("shared", "isolated", "mixed")
_RUNTIME_CHECK_MARKER = "C6 runtime read passed"
# Task titles keep the prompt's first 500 characters.
_TITLE_KEY = 200
_POLL_SECONDS = 2.0
# A chat that exits without creating a task has failed to start.
_START_GRACE_SECONDS = 30.0


@dataclass(frozen=True)
class Part:
    name: str
    prompt: str


@dataclass(frozen=True)
class Disruption:
    edit_path: str
    edit_append: str
    crash: bool = True


@dataclass(frozen=True)
class Experiment:
    id: str
    fixture: str
    acceptance: str
    parts: tuple[Part, ...]
    disruption: Disruption
    mode: str = "auto"
    answer: str = "Use your judgment and follow the request as written."
    follow_up: str = "Your earlier change was not applied ({reason}). {prompt}"
    max_follow_ups: int = 1
    checks: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    runtime_canary: bool = False
    runtime_paths: tuple[str, ...] = ()
    approved_runtime_files: tuple[str, ...] = ()

    def lanes(self, arm: str) -> list[tuple[str, str]]:
        """(name, prompt) for each session the arm starts."""

        if arm == "pair":
            return [(part.name, part.prompt) for part in self.parts]
        steps = " ".join(
            f"Change {index}: {part.prompt}"
            for index, part in enumerate(self.parts, start=1)
        )
        return [("all", f"Make these {len(self.parts)} changes. {steps}")]


def load_experiments(path: Path = EXPERIMENTS) -> tuple[int, tuple[Experiment, ...]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    version = data.get("version")
    if type(version) is not int:
        raise ValueError("the experiment file needs an integer version")
    experiments: list[Experiment] = []
    for raw in data.get("experiments", []):
        disruption = raw.get("disruption", {})
        experiment = Experiment(
            id=str(raw["id"]),
            fixture=str(raw["fixture"]),
            acceptance=str(raw["acceptance"]),
            parts=tuple(
                Part(str(part["name"]), str(part["prompt"]).strip())
                for part in raw.get("parts", [])
            ),
            disruption=Disruption(
                edit_path=str(disruption.get("edit_path", "")),
                edit_append=str(disruption.get("edit_append", "")),
                crash=bool(disruption.get("crash", True)),
            ),
            mode=str(raw.get("mode", "auto")),
            answer=str(raw.get("answer", Experiment.answer)).strip(),
            follow_up=str(raw.get("follow_up", Experiment.follow_up)).strip(),
            max_follow_ups=raw.get("max_follow_ups", 1),
            checks=raw.get("checks", {}),
            runtime_canary=raw.get("runtime_canary", False),
            runtime_paths=tuple(raw.get("runtime_paths", ())),
            approved_runtime_files=tuple(raw.get("approved_runtime_files", ())),
        )
        _validate(experiment)
        experiments.append(experiment)
    ids = [experiment.id for experiment in experiments]
    if not experiments or len(set(ids)) != len(ids):
        raise ValueError("the experiment file needs experiments with unique ids")
    return version, tuple(experiments)


def _validate(experiment: Experiment) -> None:
    def refuse(problem: str) -> ValueError:
        return ValueError(f"experiment {experiment.id!r}: {problem}")

    fixture = benchmark.FIXTURES / experiment.fixture
    if not experiment.fixture.replace("-", "").isalnum() or not fixture.is_dir():
        raise refuse(f"no fixture named {experiment.fixture!r}")
    if not experiment.acceptance.replace("-", "").isalnum() or not acceptance_tests(
        ACCEPTANCE / experiment.acceptance
    ):
        raise refuse(f"no acceptance tests named {experiment.acceptance!r}")
    names = [part.name for part in experiment.parts]
    if len(names) < 2 or len(set(names)) != len(names) or "all" in names:
        raise refuse("needs at least two parts with unique names other than 'all'")
    texts = [part.prompt for part in experiment.parts]
    texts += [experiment.answer, experiment.follow_up]
    if not all(text and "\n" not in text for text in texts):
        raise refuse("prompts, the answer, and follow_up must be single lines")
    if experiment.mode not in {"normal", "auto"}:
        raise refuse("mode must be normal or auto, since the parts edit files")
    if "{prompt}" not in experiment.follow_up:
        raise refuse("follow_up must contain {prompt}")
    try:
        experiment.follow_up.format(reason="", prompt="")
    except (KeyError, IndexError, ValueError) as exc:
        raise refuse(f"follow_up only takes {{reason}} and {{prompt}}: {exc}") from exc
    if type(experiment.max_follow_ups) is not int or experiment.max_follow_ups < 0:
        raise refuse("max_follow_ups must be a count")
    disruption = experiment.disruption
    if not (fixture / disruption.edit_path).is_file() or not disruption.edit_append:
        raise refuse("the disruption needs a fixture file and text to append")
    if not isinstance(experiment.checks, Mapping) or not all(
        isinstance(spec, Mapping)
        and isinstance(spec.get("argv"), list)
        and spec["argv"]
        and all(isinstance(item, str) for item in spec["argv"])
        for spec in experiment.checks.values()
    ):
        raise refuse("each check needs an argv list")
    if type(experiment.runtime_canary) is not bool:
        raise refuse("runtime_canary must be a boolean")
    for path in (*experiment.runtime_paths, *experiment.approved_runtime_files):
        if (
            not isinstance(path, str)
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or path != PurePosixPath(path).as_posix()
        ):
            raise refuse("runtime paths must be canonical relative paths")
    if experiment.runtime_canary and (
        ".env" not in experiment.runtime_paths
        or ".env" not in experiment.approved_runtime_files
        or not experiment.checks
    ):
        raise refuse("runtime canary requires .env runtime, exact consent and checks")


def acceptance_tests(directory: Path) -> list[str]:
    """Test function names in a directory's test files, in file order."""

    names: list[str] = []
    for path in sorted(directory.glob("test_*.py")) if directory.is_dir() else []:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names += [
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        ]
    return names


def group(name: str) -> str:
    """The request a test checks, from its name: test_GROUP_..."""

    return name.removeprefix("test_").partition("_")[0]


def junit_outcomes(report: str) -> dict[str, bool]:
    """Whether each test case in a JUnit XML report passed."""

    return {
        str(case.get("name")): not any(
            child.tag in {"failure", "error", "skipped"} for child in case
        )
        for case in ElementTree.fromstring(report).iter("testcase")
    }


def score(expected: Sequence[str], outcomes: Mapping[str, bool]) -> dict[str, Any]:
    """Passed and total tests per group; a test that never ran failed."""

    groups: dict[str, list[int]] = {}
    failed: list[str] = []
    for name in expected:
        passed = outcomes.get(name, False)
        counts = groups.setdefault(group(name), [0, 0])
        counts[0] += passed
        counts[1] += 1
        if not passed:
            failed.append(name)
    return {
        "passed": sum(counts[0] for counts in groups.values()),
        "total": len(expected),
        "groups": groups,
        "failed": failed,
    }


def edited_paths(events: Iterable[tuple[str, Mapping[str, Any]]]) -> set[str]:
    """Paths that edit tool calls changed successfully."""

    calls: dict[object, set[str]] = {}
    edited: set[str] = set()
    for kind, payload in events:
        if kind == "model.tool_call" and payload.get("tool") in benchmark._EDIT_TOOLS:
            arguments = payload.get("arguments")
            if not isinstance(arguments, Mapping):
                continue
            calls[payload.get("call_id")] = {
                str(PurePosixPath(value))
                for key in ("path", "source", "destination")
                if isinstance(value := arguments.get(key), str) and value
            }
        elif kind == "model.tool_result" and not payload.get("is_error"):
            edited |= calls.get(payload.get("call_id"), set())
    return edited


def title_key(text: str) -> str:
    return " ".join(text.split())[:_TITLE_KEY]


def not_applied_reason(state: str | None, failure: str | None) -> str | None:
    """Why a finished task's work is not in the checkout, or None if it is."""

    if state == "completed":
        return None
    if failure == "PATH_BASE_MISMATCH":
        return "the files changed before it could be applied"
    if state == "reviewing":
        return "it was held for review"
    return f"it ended {str(state).replace('_', ' ')}"


@dataclass
class Lane:
    name: str
    prompt: str
    # Every prompt this lane has sent, and the task each one became.
    texts: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    processes: list[subprocess.Popen[str]] = field(default_factory=list)
    started: list[float] = field(default_factory=list)
    follow_ups: int = 0
    workspace: str = "shared"

    def waiting_for_task(self) -> bool:
        return len(self.task_ids) < len(self.texts)

    def claim(self, title: str) -> bool:
        """Whether a new task with this title is this lane's latest prompt."""

        return self.waiting_for_task() and title_key(title) == title_key(
            self.texts[len(self.task_ids)]
        )


@dataclass
class TaskRow:
    task_id: str
    title: str
    state: str | None
    failure: str | None
    session_id: str | None
    created_at: int
    finished_at: int | None


def _task_rows(database: Path, since_ms: int) -> list[TaskRow]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return [
            TaskRow(*row)
            for row in connection.execute(
                "SELECT task_id, title, state, failure_code, session_id, "
                "created_at, finished_at FROM tasks WHERE created_at >= ? "
                "ORDER BY created_at, rowid",
                (since_ms,),
            )
        ]
    finally:
        connection.close()


def _diverged_batches(database: Path, sessions: Iterable[str]) -> int:
    sessions = sorted(set(sessions))
    if not sessions:
        return 0
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        marks = ",".join("?" * len(sessions))
        (count,) = connection.execute(
            "SELECT count(*) FROM workspace_batches WHERE state = 'diverged' "
            f"AND session_id IN ({marks})",
            sessions,
        ).fetchone()
        return int(count)
    finally:
        connection.close()


def acceptance(
    checkout: Path,
    tests: Path,
    python: str,
    protected: Sequence[Path],
    skip: Collection[str] = (),
) -> dict[str, Any]:
    """Run the checkout's own tests and the hidden ones, sandboxed, on a copy.

    Tests in a `skip` group are not scored.
    """

    expected = [name for name in acceptance_tests(tests) if group(name) not in skip]
    kind = available_sandbox()
    if kind is None:
        return {"ran": False, "reason": "no working sandbox on this machine"}
    with tempfile.TemporaryDirectory(prefix="loupe-acceptance-") as temporary:
        root = Path(temporary).resolve()
        copy = root / "repo"
        shutil.copytree(
            checkout,
            copy,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
        )
        shutil.copytree(tests, copy / "loupe_acceptance")
        policy = SandboxPolicy(writable=(root,), protected=tuple(protected))
        environment = {
            "PATH": os.defpath,
            "HOME": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

        def pytest(*arguments: str) -> subprocess.CompletedProcess[str]:
            argv = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *arguments]
            return subprocess.run(
                wrap(kind, policy, argv, cwd=copy),
                cwd=copy,
                env=environment,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )

        own = pytest("tests")
        report = root / "acceptance.xml"
        hidden = pytest(f"--junitxml={report}", "loupe_acceptance")
        if own.returncode in {126, 127} or hidden.returncode in {126, 127}:
            return {"ran": False, "reason": "sandboxed interpreter could not execute"}
        outcomes = junit_outcomes(report.read_text()) if report.exists() else {}
    lines = (own.stdout or own.stderr).strip().splitlines()
    return {
        "ran": True,
        "own_tests_passed": own.returncode == 0,
        "own_tests": lines[-1] if lines else "",
        **score(expected, outcomes),
    }


@dataclass
class Context:
    paths: AppPaths
    loupe: list[str]
    chat: list[str]
    environment: dict[str, str]
    work: Path
    configs: Path
    timeout: float
    workspace: str = "shared"
    token_budget: int | None = None


def lane_workspace(selection: str, index: int) -> str:
    """Mixed pairs put the first lane in shared and later lanes in isolated."""
    if selection not in WORKSPACES:
        raise ValueError("unknown workspace selection")
    return (
        "shared"
        if selection == "mixed" and index == 0
        else ("isolated" if selection == "mixed" else selection)
    )


def event_tokens(events: Iterable[tuple[str, Mapping[str, Any]]]) -> int:
    usage = benchmark.summarize_events(events).usage
    return int(usage.get("prompt_tokens", usage.get("input_tokens", 0))) + int(
        usage.get("output_tokens", 0)
    )


def prepare_runtime(repository: Path, experiment: Experiment) -> bytes | None:
    if not experiment.runtime_canary:
        return None
    canary = ("sk-" + secrets.token_hex(20)).encode()
    (repository / ".env").write_bytes(b"LOUPE_C6_CANARY=" + canary + b"\n")
    (repository / ".env").chmod(0o600)
    ignored = subprocess.run(
        ["git", "-C", str(repository), "check-ignore", "--", ".env"],
        capture_output=True,
        check=False,
    )
    if ignored.returncode:
        raise ValueError("runtime canary must be ignored by the fixture")
    return canary


def git_invariants(repository: Path) -> dict[str, str]:
    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    return {
        "head": git("rev-parse", "HEAD"),
        "index": git("write-tree"),
        "worktrees": git("worktree", "list", "--porcelain"),
    }


def _private_workspaces(
    paths: AppPaths, session_ids: set[str]
) -> list[tuple[str, str]]:
    if not session_ids:
        return []
    with closing(
        sqlite3.connect(f"file:{paths.control_db}?mode=ro", uri=True)
    ) as connection:
        marks = ",".join("?" for _ in session_ids)
        return [
            (str(path), str(state))
            for path, state in connection.execute(
                "SELECT w.canonical_path,w.state FROM sessions s JOIN workspaces w "
                f"USING(workspace_id) WHERE s.session_id IN ({marks}) "
                "AND w.mode='isolated'",
                sorted(session_ids),
            )
        ]


def close_run_sessions(
    context: Context, session_ids: set[str], task_ids: set[str]
) -> bool:
    """Dispose only settled held proposals, then close this run's conversations."""
    client = DaemonClient(context.paths, timeout_seconds=60)
    with closing(
        sqlite3.connect(f"file:{context.paths.control_db}?mode=ro", uri=True)
    ) as con:
        retained = [
            str(task_id)
            for task_id, status, proposal in con.execute(
                "SELECT task_id,status,proposal_json FROM task_workflows"
            )
            if task_id in task_ids
            and proposal is not None
            and status in {"awaiting_review", "cancelled"}
        ]
        states = dict(con.execute("SELECT session_id,state FROM sessions"))
    for task_id in retained:
        client.call("task.discard", {"task_id": task_id}, autostart=False)
    for session_id in sorted(session_ids):
        if states.get(session_id) != "closed":
            client.call(
                "session.close",
                {
                    "session_id": session_id,
                    "resume_secret": session_resume_secret(context.paths, session_id),
                },
                autostart=False,
            )
        remove_session_secret(context.paths, session_id)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        private = _private_workspaces(context.paths, session_ids)
        if all(
            state == "removed" and not Path(path).exists() for path, state in private
        ):
            return True
        time.sleep(0.1)
    return False


def _contains_canary(path: Path, canary: bytes) -> bool:
    if not path.is_file():
        return False
    tail = b""
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            combined = tail + chunk
            if canary in combined:
                return True
            tail = combined[-len(canary) :]
    return False


def _database_contains_canary(database: Path, canary: bytes) -> bool:
    # Closing any raw descriptor for a SQLite DB or SHM inode releases this
    # process's POSIX locks, including locks owned by live SQLite connections.
    # Use a fresh process; send the value privately on stdin, never argv/env.
    program = """
import pathlib, sys
canary = sys.stdin.buffer.read()
for path in pathlib.Path(sys.argv[1]).parent.glob('control.sqlite3*'):
    if not path.is_file():
        continue
    tail = b''
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            combined = tail + chunk
            if canary in combined:
                sys.exit(1)
            tail = combined[-len(canary):]
sys.exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(database)],
        input=canary,
        capture_output=True,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError("private database canary scan did not complete")
    return result.returncode == 1


def audit_run(
    paths: AppPaths,
    repository: Path,
    before: Mapping[str, str],
    canary: bytes | None,
    lanes: Sequence[Lane],
    rows: Mapping[str, TaskRow],
    scenario: str,
    edit_at: float | None,
    crash_at: float | None,
    cleanup: bool,
) -> dict[str, bool]:
    after = git_invariants(repository)
    invariants = {
        "head_unchanged": before["head"] == after["head"],
        "index_unchanged": before["index"] == after["index"],
        "worktree_registry_restored": before["worktrees"] == after["worktrees"],
        "sessions_closed_private_workspaces_removed": cleanup,
        "disruption_completed": scenario != "disrupted"
        or (edit_at is not None and crash_at is not None),
    }
    with closing(
        sqlite3.connect(f"file:{paths.control_db}?mode=ro", uri=True)
    ) as connection:
        bindings = {
            str(session_id): (str(mode), str(state))
            for session_id, mode, state in connection.execute(
                "SELECT session_id,workspace_mode,state FROM sessions"
            )
        }
    invariants["requested_workspaces_confirmed"] = all(
        lane.task_ids
        and bindings.get(rows[lane.task_ids[-1]].session_id)
        == (lane.workspace, "closed")
        for lane in lanes
    )
    if canary is not None:
        invariants.update(
            {
                "primary_canary_preserved": (repository / ".env").is_file()
                and (repository / ".env").read_bytes()
                == b"LOUPE_C6_CANARY=" + canary + b"\n",
                "database_has_no_canary": not _database_contains_canary(
                    paths.control_db, canary
                ),
                "daemon_log_has_no_canary": not _contains_canary(
                    paths.log_file, canary
                ),
                "chat_logs_have_no_canary": not any(
                    _contains_canary(path, canary)
                    for path in repository.parent.glob("*.log")
                ),
                "session_files_have_no_canary": not any(
                    _contains_canary(path, canary)
                    for path in paths.session_dir.rglob("*")
                ),
                "managed_env_copies_absent": not any(paths.data_dir.rglob(".env")),
            }
        )
        completed = {
            lane.task_ids[-1]
            for lane in lanes
            if lane.task_ids and rows[lane.task_ids[-1]].state == "completed"
        }
        with closing(
            sqlite3.connect(f"file:{paths.control_db}?mode=ro", uri=True)
        ) as connection:
            passed = {
                str(task_id)
                for task_id, name, state, output in connection.execute(
                    "SELECT task_id,name,state,output FROM check_runs"
                )
                if name == "runtime"
                and state == "passed"
                and _RUNTIME_CHECK_MARKER in (output or "")
            }
        invariants["each_lane_ran_runtime_check"] = (
            len(completed) == len(lanes) and completed <= passed
        )
    return invariants


def _start(
    context: Context,
    repository: Path,
    mode: str,
    lane: Lane,
    text: str,
    approved_runtime_files: Sequence[str] = (),
) -> None:
    attempt = len(lane.texts)
    log = context.work / f"{repository.name}-{lane.name}-{attempt}.log"
    with open(log, "w") as output:
        process = subprocess.Popen(
            [
                *context.chat,
                "--mode",
                mode,
                "--repo",
                str(repository),
                "--workspace",
                lane.workspace,
                *(
                    argument
                    for path in (
                        approved_runtime_files if lane.workspace == "isolated" else ()
                    )
                    for argument in ("--approve-secret", path)
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            env=context.environment,
        )
    assert process.stdin is not None
    process.stdin.write(text + "\n")
    process.stdin.close()
    lane.texts.append(text)
    lane.processes.append(process)
    lane.started.append(time.monotonic())


def _note(started: float, message: str) -> None:
    print(f"    {time.monotonic() - started:6.0f}s  {message}", flush=True)


def run_arm(
    context: Context,
    experiment: Experiment,
    arm: str,
    scenario: str,
    repetition: int,
) -> dict[str, Any]:
    name = f"{experiment.id}-{arm}-{scenario}-{repetition}"
    repository = benchmark._fixture(context.work / name, experiment.fixture)
    before = git_invariants(repository)
    canary = prepare_runtime(repository, experiment)
    if experiment.checks:
        config = context.configs / f"{name}-checks.toml"
        config.write_text(
            "runtime_paths = "
            + json.dumps(list(experiment.runtime_paths))
            + "\n"
            + benchmark.checks_toml(experiment.checks, sys.executable),
            encoding="utf-8",
        )
        for command in (
            ["repo", "add", str(repository)],
            ["checks", "configure", "--repo", str(repository), "--file", str(config)],
        ):
            subprocess.run(
                [*context.loupe, *command],
                env=context.environment,
                capture_output=True,
                check=True,
            )
    database = context.paths.control_db
    disrupted = scenario == "disrupted"
    lanes = [
        Lane(lane, prompt, workspace=lane_workspace(context.workspace, index))
        for index, (lane, prompt) in enumerate(experiment.lanes(arm))
    ]
    since = int(time.time() * 1000)
    wall, started = time.time(), time.monotonic()
    deadline = started + context.timeout
    for lane in lanes:
        _start(
            context,
            repository,
            experiment.mode,
            lane,
            lane.prompt,
            experiment.approved_runtime_files,
        )
    rows: dict[str, TaskRow] = {}
    answers_given: dict[str, int] = {}
    interventions = {"answers": 0, "follow_ups": 0}
    edit_at = crash_at = None
    timed_out = False
    budget_stopped = False
    while True:
        for row in _task_rows(database, since):
            if row.task_id not in rows:
                owner = next((lane for lane in lanes if lane.claim(row.title)), None)
                if owner is None:
                    continue
                owner.task_ids.append(row.task_id)
                _note(started, f"{owner.name}: task {row.task_id} started")
            rows[row.task_id] = row
        events = {
            task_id: benchmark._events(database, task_id)
            for lane in lanes
            for task_id in lane.task_ids
        }
        current = {lane.name: lane.task_ids[-1] for lane in lanes if lane.task_ids}

        def settled(task_id: str) -> bool:
            return rows[task_id].state in benchmark._SETTLED

        target = experiment.disruption.edit_path
        if (
            disrupted
            and edit_at is None
            and any(
                target in edited_paths(task_events) for task_events in events.values()
            )
        ):
            with open(repository / target, "a", encoding="utf-8") as handle:
                handle.write(experiment.disruption.edit_append)
            edit_at = time.monotonic() - started
            _note(started, f"operator edited {target} by hand")
        if (
            disrupted
            and experiment.disruption.crash
            and crash_at is None
            and len(current) == len(lanes)
            and all(
                settled(task) or benchmark.edit_seen(events[task])
                for task in current.values()
            )
            and not all(settled(task) for task in current.values())
        ):
            benchmark._crash(context.paths, context.loupe, context.environment)
            crash_at = time.monotonic() - started
            _note(started, "killed and restarted the background service")
        for lane in lanes:
            task = current.get(lane.name)
            if task is None or settled(task):
                continue
            asked = sum(kind == "question.asked" for kind, _ in events[task])
            if (
                benchmark.awaiting_answer(events[task])
                and answers_given.get(task, 0) < asked
            ):
                benchmark._reply(DaemonClient(context.paths), task, experiment.answer)
                answers_given[task] = answers_given.get(task, 0) + 1
                interventions["answers"] += 1
                _note(started, f"{lane.name}: answered a question")
        pending = False
        for lane in lanes:
            if lane.waiting_for_task():
                pending = True
                exited = lane.processes[-1].poll() is not None
                if (
                    exited
                    and time.monotonic() - lane.started[-1] > _START_GRACE_SECONDS
                ):
                    raise SystemExit(
                        f"{lane.name}'s chat created no task; see {context.work}"
                    )
                continue
            task = lane.task_ids[-1]
            if not settled(task):
                pending = True
                continue
            row = rows[task]
            reason = not_applied_reason(row.state, row.failure)
            if reason is not None and lane.follow_ups < experiment.max_follow_ups:
                lane.follow_ups += 1
                interventions["follow_ups"] += 1
                _note(started, f"{lane.name}: {row.state}, {reason}; asking again")
                _start(
                    context,
                    repository,
                    experiment.mode,
                    lane,
                    experiment.follow_up.format(reason=reason, prompt=lane.prompt),
                    experiment.approved_runtime_files,
                )
                pending = True
        if not pending:
            break
        observed_tokens = sum(
            event_tokens(task_events) for task_events in events.values()
        )
        budget_stopped = context.token_budget is not None and observed_tokens >= max(
            0, context.token_budget - 100_000
        )
        if time.monotonic() >= deadline or budget_stopped:
            timed_out = not budget_stopped
            for task_id, row in rows.items():
                if row.state not in benchmark._SETTLED:
                    benchmark._cancel(context.loupe, context.environment, task_id)
            break
        time.sleep(_POLL_SECONDS)
    for lane in lanes:
        for process in lane.processes:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    wall_seconds = time.time() - wall
    elapsed = time.monotonic() - started
    rows = {row.task_id: row for row in _task_rows(database, since)}
    tasks = [
        _task_result(lane.name, rows[task_id], benchmark._events(database, task_id))
        for lane in lanes
        for task_id in lane.task_ids
    ]
    tokens = {
        key: sum(task[key] for task in tasks)
        for key in (
            "prompt_tokens",
            "output_tokens",
            "cached_tokens",
            "helper_prompt_tokens",
        )
    }
    protected = (
        context.paths.config_dir,
        context.paths.data_dir,
        context.paths.state_dir,
        context.paths.runtime_dir,
        *protected_home_paths(Path.home()),
    )
    acceptance_result = acceptance(
        repository,
        ACCEPTANCE / experiment.acceptance,
        sys.executable,
        protected,
        skip=() if edit_at is not None else (OPERATOR_GROUP,),
    )
    session_ids = {row.session_id for row in rows.values() if row.session_id}
    cleanup = close_run_sessions(context, session_ids, set(rows))
    invariants = audit_run(
        context.paths,
        repository,
        before,
        canary,
        lanes,
        rows,
        scenario,
        edit_at,
        crash_at,
        cleanup,
    )
    result = {
        "experiment": experiment.id,
        "workspace": context.workspace,
        "arm": arm,
        "scenario": scenario,
        "repetition": repetition,
        "repository": str(repository),
        "timed_out": timed_out,
        "budget_stopped": budget_stopped,
        "wall_seconds": round(wall_seconds, 1),
        "suspended_seconds": round(max(0.0, wall_seconds - elapsed), 1),
        "lanes": {
            lane.name: {
                "applied": bool(lane.task_ids)
                and not_applied_reason(
                    rows[lane.task_ids[-1]].state, rows[lane.task_ids[-1]].failure
                )
                is None,
                "follow_ups": lane.follow_ups,
                "workspace": lane.workspace,
            }
            for lane in lanes
        },
        "tasks": tasks,
        **tokens,
        "interventions": interventions,
        "operator_edit_seconds": None if edit_at is None else round(edit_at, 1),
        "crash_seconds": None if crash_at is None else round(crash_at, 1),
        "diverged_batches": _diverged_batches(
            database, (row.session_id for row in rows.values() if row.session_id)
        ),
        "changed_files": _changed_files(repository),
        "acceptance": acceptance_result,
        "invariants": invariants,
        "passed": (
            not timed_out
            and not budget_stopped
            and acceptance_result.get("ran", False)
            and acceptance_result.get("own_tests_passed", False)
            and acceptance_result.get("passed") == acceptance_result.get("total")
            and all(invariants.values())
            and all(task["recorded_once"] for task in tasks)
            and all(
                lane.task_ids and rows[lane.task_ids[-1]].state == "completed"
                for lane in lanes
            )
        ),
    }
    if canary is not None:
        encoded = json.dumps(result)
        exposed = canary.decode() in encoded
        result = json.loads(encoded.replace(canary.decode(), "[canary withheld]"))
        result["invariants"]["report_has_no_canary"] = not exposed
        result["passed"] = result["passed"] and not exposed
    return result


def _task_result(
    lane: str, row: TaskRow, events: list[tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    metrics = benchmark.summarize_events(events)
    usage = metrics.usage
    return {
        "lane": lane,
        "task_id": row.task_id,
        "state": row.state,
        "failure_code": row.failure,
        "outcome": metrics.outcome,
        "seconds": None
        if row.finished_at is None
        else round((row.finished_at - row.created_at) / 1000, 1),
        "main_turns": metrics.main_turns,
        "main_tool_calls": sum(metrics.main_tool_calls.values()),
        "tools": metrics.main_tool_calls,
        "edits": metrics.edits,
        "questions": metrics.questions,
        "verification": metrics.verification,
        "resumed": metrics.resumed,
        "recorded_once": not metrics.repeated_results and not metrics.repeated_answers,
        # Times the agent was told what other sessions were doing.
        "coordination_updates": sum(
            kind == "model.coordination_updated" for kind, _ in events
        ),
        "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
        "output_tokens": usage.get("output_tokens", 0),
        "cached_tokens": usage.get("cache_read_input_tokens", 0),
        "helper_prompt_tokens": usage.get("explore_prompt_tokens", 0),
        "answer": metrics.answer[:2000],
    }


def _changed_files(repository: Path) -> list[str]:
    status = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return sorted(line[3:] for line in status.splitlines() if line)


def describe(result: Mapping[str, Any]) -> str:
    accepted = result["acceptance"]
    if accepted.get("ran"):
        groups = ", ".join(
            f"{name} {passed}/{total}"
            for name, (passed, total) in accepted["groups"].items()
        )
        tests = f"hidden tests {accepted['passed']}/{accepted['total']} ({groups})"
        tests += ", own tests " + ("pass" if accepted["own_tests_passed"] else "FAIL")
    else:
        tests = f"tests not run: {accepted.get('reason')}"
    interventions = result["interventions"]
    line = (
        f"  {result['arm']}/{result['scenario']}: {result['wall_seconds']}s, "
        f"{len(result['tasks'])} tasks, "
        f"{result['prompt_tokens'] + result['output_tokens']:,} tokens, "
        f"{interventions['answers']} answers + {interventions['follow_ups']} "
        f"follow-ups, {result['diverged_batches']} rejected batches; {tests}"
    )
    failed = [key for key, passed in result.get("invariants", {}).items() if not passed]
    if failed:
        line += "; FAILED invariants: " + ", ".join(failed)
    if result.get("budget_stopped"):
        line += "; TOKEN BUDGET STOP"
    if result["timed_out"]:
        line += "; TIMED OUT"
    if result["suspended_seconds"] > benchmark._SUSPENSION_NOTICE_SECONDS:
        line += f"; the machine slept {result['suspended_seconds']}s"
    return line


def _checkpoint(output: Path, document: Mapping[str, Any]) -> None:
    """Retain completed measurements before starting another billed case."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(arguments: argparse.Namespace) -> int:
    version, experiments = load_experiments(arguments.experiments)
    experiments = tuple(
        e for e in experiments if not arguments.only or e.id in arguments.only
    )
    if not experiments:
        raise SystemExit("no experiment matches --only")
    if arguments.profile == "default":
        raise SystemExit("use a separate, signed-in profile, not the default one")
    if any(e.runtime_canary for e in experiments) and not all(
        os.environ.get(name)
        for name in ("LLM_COORD_DATA_HOME", "LLM_COORD_RUNTIME_DIR")
    ):
        raise SystemExit(
            "runtime smoke runs require separate LLM_COORD_DATA_HOME and "
            "LLM_COORD_RUNTIME_DIR; keep the credential state directory unchanged"
        )
    if available_sandbox() is None:
        raise SystemExit("acceptance tests need a working sandbox on this machine")
    paths = AppPaths.resolve(arguments.profile)
    loupe = [*benchmark._loupe(), "--profile", arguments.profile]
    chat = [*loupe, "chat", "--plain"]
    for option in ("provider", "model", "effort"):
        value = getattr(arguments, option)
        if value:
            chat += [f"--{option}", value]
    results: list[dict[str, Any]] = []
    output = arguments.out or (
        paths.state_dir
        / "benchmark"
        / f"sessions-{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    head = subprocess.run(
        ["git", "-C", str(benchmark.ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    document: dict[str, Any] = {
        "experiment_version": version,
        "loupe_commit": head,
        "settings": {
            "set": list(arguments.set),
            "provider": arguments.provider,
            "model": arguments.model,
            "effort": arguments.effort,
            "workspace": arguments.workspace,
        },
        "runs": results,
        "errors": [],
    }
    _checkpoint(output, document)
    with tempfile.TemporaryDirectory(prefix="loupe-sessions-") as temporary:
        config_home = Path(temporary).resolve()
        benchmark.write_config(config_home, arguments.set, arguments.profile)
        environment = {**os.environ, "LLM_COORD_CONFIG_HOME": str(config_home)}
        paths = AppPaths.resolve(arguments.profile, environ=environment)
        work = paths.state_dir / "benchmark" / "sessions" / str(time.time_ns())
        work.mkdir(parents=True)
        context = Context(
            paths,
            loupe,
            chat,
            environment,
            work,
            config_home,
            arguments.timeout,
            arguments.workspace,
        )
        subprocess.run(
            [*loupe, "daemon", "stop"],
            env=environment,
            capture_output=True,
            check=False,
        )
        started = subprocess.run(
            [*loupe, "daemon", "start"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if started.returncode != 0:
            message = (started.stderr or started.stdout).strip()
            raise SystemExit(f"the background service did not start: {message}")
        # Alternate which arm goes first, so neither always meets a busier or
        # quieter provider.
        runs = [
            (repetition, experiment, scenario, arm)
            for repetition in range(1, arguments.repeat + 1)
            for experiment in experiments
            for scenario in arguments.scenarios
            for arm in arguments.arms[:: 1 if repetition % 2 else -1]
        ]
        spent = 0
        try:
            for repetition, experiment, scenario, arm in runs:
                if spent + 250_000 >= arguments.budget_tokens:
                    print("Stopping: the token budget is spent.")
                    break
                print(
                    f"Running {experiment.id} {arm}/{scenario} #{repetition}…",
                    flush=True,
                )
                context.token_budget = arguments.budget_tokens - spent
                try:
                    result = run_arm(context, experiment, arm, scenario, repetition)
                except BaseException as error:
                    # Error messages can include subprocess output. Retain only
                    # the error class and case identity, never private bytes.
                    document["errors"].append(
                        {
                            "kind": type(error).__name__,
                            "experiment": experiment.id,
                            "arm": arm,
                            "scenario": scenario,
                            "repetition": repetition,
                        }
                    )
                    _checkpoint(output, document)
                    raise
                results.append(result)
                _checkpoint(output, document)
                spent += result["prompt_tokens"] + result["output_tokens"]
                print(describe(result), flush=True)
                if not result["passed"]:
                    print(
                        "Stopping: acceptance or an isolation invariant failed.",
                        flush=True,
                    )
                    break
        finally:
            subprocess.run(
                [*loupe, "daemon", "stop"],
                env=environment,
                capture_output=True,
                check=False,
            )
    _checkpoint(output, document)
    print(f"Results: {output}")
    return 0 if results and all(result["passed"] for result in results) else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", required=True, help="a signed-in profile")
    parser.add_argument("--experiments", type=Path, default=EXPERIMENTS)
    parser.add_argument("--only", nargs="+", metavar="ID", help="run these experiments")
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS))
    parser.add_argument(
        "--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS)
    )
    parser.add_argument("--repeat", type=int, default=1, help="runs of each arm")
    parser.add_argument("--workspace", choices=WORKSPACES, default="shared")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="an [agent] setting for this run, such as explore_effort=low",
    )
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument(
        "--budget-tokens",
        type=int,
        default=8_000_000,
        help="start no arm once this many tokens are spent (default 8M)",
    )
    parser.add_argument(
        "--timeout", type=int, default=3_600, help="seconds allowed per arm"
    )
    parser.add_argument("--out", type=Path, help="where to write the results")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
