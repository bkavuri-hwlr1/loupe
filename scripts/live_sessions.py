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
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import live_benchmark as benchmark

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
SCENARIOS = ("clean", "disrupted")
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
    checkout: Path, tests: Path, python: str, protected: Sequence[Path]
) -> dict[str, Any]:
    """Run the checkout's own tests and the hidden ones, sandboxed, on a copy."""

    expected = acceptance_tests(tests)
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
        pytest(f"--junitxml={report}", "loupe_acceptance")
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


def _start(
    context: Context, repository: Path, mode: str, lane: Lane, text: str
) -> None:
    attempt = len(lane.texts)
    log = context.work / f"{repository.name}-{lane.name}-{attempt}.log"
    with open(log, "w") as output:
        process = subprocess.Popen(
            [*context.chat, "--mode", mode, "--repo", str(repository)],
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
    if experiment.checks:
        config = context.configs / f"{name}-checks.toml"
        config.write_text(
            benchmark.checks_toml(experiment.checks, sys.executable), encoding="utf-8"
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
    lanes = [Lane(lane, prompt) for lane, prompt in experiment.lanes(arm)]
    since = int(time.time() * 1000)
    wall, started = time.time(), time.monotonic()
    deadline = started + context.timeout
    for lane in lanes:
        _start(context, repository, experiment.mode, lane, lane.prompt)
    rows: dict[str, TaskRow] = {}
    answers_given: dict[str, int] = {}
    interventions = {"answers": 0, "follow_ups": 0}
    edit_at = crash_at = None
    timed_out = False
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
                )
                pending = True
        if not pending:
            break
        if time.monotonic() >= deadline:
            timed_out = True
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
    return {
        "experiment": experiment.id,
        "arm": arm,
        "scenario": scenario,
        "repetition": repetition,
        "repository": str(repository),
        "timed_out": timed_out,
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
        "acceptance": acceptance(
            repository,
            ACCEPTANCE / experiment.acceptance,
            sys.executable,
            protected,
        ),
    }


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
    if result["timed_out"]:
        line += "; TIMED OUT"
    if result["suspended_seconds"] > benchmark._SUSPENSION_NOTICE_SECONDS:
        line += f"; the machine slept {result['suspended_seconds']}s"
    return line


def run(arguments: argparse.Namespace) -> int:
    version, experiments = load_experiments(arguments.experiments)
    experiments = tuple(
        e for e in experiments if not arguments.only or e.id in arguments.only
    )
    if not experiments:
        raise SystemExit("no experiment matches --only")
    if arguments.profile == "default":
        raise SystemExit("use a separate, signed-in profile, not the default one")
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
    with tempfile.TemporaryDirectory(prefix="loupe-sessions-") as temporary:
        config_home = Path(temporary).resolve()
        benchmark.write_config(config_home, arguments.set, arguments.profile)
        environment = {**os.environ, "LLM_COORD_CONFIG_HOME": str(config_home)}
        work = paths.state_dir / "benchmark" / "sessions"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        context = Context(
            paths, loupe, chat, environment, work, config_home, arguments.timeout
        )
        subprocess.run([*loupe, "daemon", "stop"], capture_output=True, check=False)
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
                if spent >= arguments.budget_tokens:
                    print("Stopping: the token budget is spent.")
                    break
                print(
                    f"Running {experiment.id} {arm}/{scenario} #{repetition}…",
                    flush=True,
                )
                result = run_arm(context, experiment, arm, scenario, repetition)
                results.append(result)
                spent += result["prompt_tokens"] + result["output_tokens"]
                print(describe(result), flush=True)
        finally:
            subprocess.run([*loupe, "daemon", "stop"], capture_output=True, check=False)
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
    document = {
        "experiment_version": version,
        "loupe_commit": head,
        "settings": {
            "set": list(arguments.set),
            "provider": arguments.provider,
            "model": arguments.model,
            "effort": arguments.effort,
        },
        "runs": results,
    }
    Path(output).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"Results: {output}")
    return 0


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
