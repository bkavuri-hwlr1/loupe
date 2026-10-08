"""Run real tasks with a real model and check how the agent behaves.

Run with `uv run --extra openai python scripts/live_benchmark.py --profile NAME`.

Every task makes billed model requests, so the benchmark never runs in CI or
the test suite. NAME must be a profile you have already signed in to (`loupe
--profile NAME`, then `/login`); the default profile is refused, because the
benchmark restarts the profile's background service. For the run, that service
gets its own configuration, built from `--set` options, so your configuration
file is not used or changed.

A task runs either in a clone of this repository at the revision the task file
names, or in a fresh Git repository made from one of `benchmark_fixtures/`,
with that task's checks configured. Each task is scored on what the agent did,
not only on its answer: the task's final state, whether it edited files, what
its checks reported, how many questions it asked, and whether every tool
result and answer was recorded exactly once. A task can also be cancelled or
have its background service killed and restarted after its first edit.
Results are written as JSON; `--compare OLD NEW` prints how two runs differ.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm_cli.config.loader import load_settings
from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient

ROOT = Path(__file__).resolve().parents[1]
TASKS = Path(__file__).with_name("live_benchmark.toml")
FIXTURES = Path(__file__).with_name("benchmark_fixtures")
_EXPLORE_EXPECTATIONS = {"any", "never", "expected"}
_EDIT_EXPECTATIONS = {"any", "none", "some"}
_INTERRUPTS = {"cancel", "crash"}
_OUTCOMES = {"completed", "partial", "blocked"}
# Task states a task stays in once it stops working.
_SETTLED = frozenset(
    {
        "reviewing",
        "completed",
        "failed",
        "cancelled",
        "ready_for_integration",
        "operator_attention",
    }
)
_EDIT_TOOLS = frozenset(
    {"write_file", "apply_patch", "create_directory", "delete_file", "rename_file"}
)
# Wall-clock time beyond the monotonic clock's means the machine slept; such a
# run's timings are not comparable.
_SUSPENSION_NOTICE_SECONDS = 5.0


@dataclass(frozen=True)
class Task:
    id: str
    prompt: str
    mode: str = "plan"
    explore: str = "any"
    facts: tuple[tuple[str, ...], ...] = ()
    min_facts: float = 1.0
    # A directory in benchmark_fixtures/; otherwise the pinned clone.
    fixture: str | None = None
    # Configured checks, {name: {argv, ...}}; "{python}" in argv is replaced
    # with this interpreter, which has pytest.
    checks: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # Expected task state once it stops, and the model outcomes allowed.
    state: str = "completed"
    outcomes: tuple[str, ...] = ()
    edits: str = "any"
    verification: str | None = None
    questions: int | None = None
    # Replies to the agent's questions, in order, given only when it asks.
    answers: tuple[str, ...] = ()
    min_answer_characters: int = 0
    max_main_turns: int | None = None
    # "cancel" or "crash", applied after the task's first edit.
    interrupt: str | None = None


@dataclass(frozen=True)
class Suite:
    version: int
    revision: str
    tasks: tuple[Task, ...]


def load_suite(path: Path = TASKS) -> Suite:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    version, revision = data.get("version"), data.get("revision")
    if type(version) is not int or not isinstance(revision, str) or not revision:
        raise ValueError("the task file needs an integer version and a revision")
    tasks: list[Task] = []
    for raw in data.get("tasks", []):
        facts = raw.get("facts", [])
        if not isinstance(facts, list) or not all(
            isinstance(fact, list) and fact and all(isinstance(a, str) for a in fact)
            for fact in facts
        ):
            raise ValueError(f"task {raw.get('id')!r}: facts must be lists of text")
        task = Task(
            id=str(raw["id"]),
            prompt=str(raw["prompt"]).strip(),
            mode=str(raw.get("mode", "plan")),
            explore=str(raw.get("explore", "any")),
            facts=tuple(tuple(fact) for fact in facts),
            min_facts=float(raw.get("min_facts", 1.0)),
            fixture=raw.get("fixture"),
            checks=raw.get("checks", {}),
            state=str(raw.get("state", "completed")),
            outcomes=tuple(raw.get("outcomes", ())),
            edits=str(raw.get("edits", "any")),
            verification=raw.get("verification"),
            questions=raw.get("questions"),
            answers=tuple(raw.get("answers", ())),
            min_answer_characters=int(raw.get("min_answer_characters", 0)),
            max_main_turns=raw.get("max_main_turns"),
            interrupt=raw.get("interrupt"),
        )
        _validate(task)
        tasks.append(task)
    ids = [task.id for task in tasks]
    if not tasks or len(set(ids)) != len(ids):
        raise ValueError("the task file needs tasks with unique ids")
    return Suite(version, revision, tuple(tasks))


def _validate(task: Task) -> None:
    def refuse(problem: str) -> ValueError:
        return ValueError(f"task {task.id!r}: {problem}")

    if task.mode not in {"plan", "normal", "auto"}:
        raise refuse(f"unknown mode {task.mode!r}")
    if task.explore not in _EXPLORE_EXPECTATIONS:
        raise refuse(f"unknown explore {task.explore!r}")
    if not task.prompt or not 0 < task.min_facts <= 1:
        raise refuse("needs a prompt and 0 < min_facts <= 1")
    if task.fixture is not None and not (
        isinstance(task.fixture, str)
        and task.fixture.replace("-", "").isalnum()
        and (FIXTURES / task.fixture).is_dir()
    ):
        raise refuse(f"no fixture named {task.fixture!r}")
    if task.checks and task.fixture is None:
        raise refuse("checks need a fixture")
    if not isinstance(task.checks, Mapping) or not all(
        isinstance(spec, Mapping)
        and isinstance(spec.get("argv"), list)
        and spec["argv"]
        and all(isinstance(item, str) for item in spec["argv"])
        for spec in task.checks.values()
    ):
        raise refuse("each check needs an argv list")
    if task.state not in _SETTLED:
        raise refuse(f"unknown state {task.state!r}")
    if not set(task.outcomes) <= _OUTCOMES:
        raise refuse(f"outcomes must be among {sorted(_OUTCOMES)}")
    if task.edits not in _EDIT_EXPECTATIONS:
        raise refuse(f"unknown edits {task.edits!r}")
    if task.verification not in {None, "passed", "failed"}:
        raise refuse(f"unknown verification {task.verification!r}")
    if task.questions is not None and (
        type(task.questions) is not int or task.questions < 0
    ):
        raise refuse("questions must be a count")
    if not all(isinstance(answer, str) and answer for answer in task.answers):
        raise refuse("answers must be nonblank lines")
    if task.max_main_turns is not None and (
        type(task.max_main_turns) is not int or task.max_main_turns < 1
    ):
        raise refuse("max_main_turns must be a positive count")
    if task.interrupt is not None and task.interrupt not in _INTERRUPTS:
        raise refuse(f"interrupt must be one of {sorted(_INTERRUPTS)}")
    if task.interrupt is not None and task.mode == "plan":
        raise refuse("an interrupt needs an edit, which plan mode cannot make")


def fact_coverage(
    answer: str, facts: Sequence[Sequence[str]]
) -> tuple[list[str], list[str]]:
    """Facts the answer states and misses, each named by its first wording."""

    text = answer.lower()
    found: list[str] = []
    missed: list[str] = []
    for fact in facts:
        (found if any(a.lower() in text for a in fact) else missed).append(fact[0])
    return found, missed


@dataclass
class Metrics:
    answer: str = ""
    outcome: str | None = None
    main_turns: int = 0
    main_tool_calls: dict[str, int] = field(default_factory=dict)
    peak_context_tokens: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    explorations: list[dict[str, Any]] = field(default_factory=list)
    questions: int = 0
    # Edit tool calls that succeeded, and the files a held proposal kept.
    edits: int = 0
    files_kept: int | None = None
    verification: str | None = None
    # Tool results and answer parts recorded more than once.
    repeated_results: int = 0
    repeated_answers: int = 0
    resumed: bool = False


def awaiting_answer(events: Iterable[tuple[str, Mapping[str, Any]]]) -> bool:
    """Whether the task is waiting on a question nobody has answered."""

    pending = 0
    for kind, _ in events:
        if kind == "question.asked":
            pending += 1
        elif kind in {"question.answered", "question.unanswered"}:
            pending -= 1
    return pending > 0


def edit_seen(events: Iterable[tuple[str, Mapping[str, Any]]]) -> bool:
    """Whether an edit tool call has succeeded, so an interrupt can follow."""

    return any(
        kind == "model.tool_result"
        and payload.get("tool") in _EDIT_TOOLS
        and not payload.get("is_error")
        for kind, payload in events
    )


def summarize_events(events: Iterable[tuple[str, Mapping[str, Any]]]) -> Metrics:
    """Measure one task from its durable events, in sequence order."""

    metrics = Metrics()
    answer_parts: dict[int, str] = {}
    efforts: dict[object, object] = {}
    # Long text is split into numbered parts of one event; a part seen twice
    # was recorded twice.
    results: set[tuple[object, object]] = set()
    answers: set[tuple[object, object]] = set()
    # A task that stops without an answer reports no total; add its turns.
    turn_usage: dict[str, int] = {}
    for kind, payload in events:
        if kind == "model.turn.completed":
            metrics.main_turns += 1
            usage = payload.get("usage") or {}
            for key, value in usage.items():
                if type(value) is int:
                    turn_usage[key] = turn_usage.get(key, 0) + value
            context = payload.get("context_tokens")
            if type(context) is not int:
                context = usage.get("prompt_tokens", usage.get("input_tokens", 0))
            metrics.peak_context_tokens = max(metrics.peak_context_tokens, context)
        elif kind == "model.tool_call":
            tool = str(payload.get("tool"))
            metrics.main_tool_calls[tool] = metrics.main_tool_calls.get(tool, 0) + 1
        elif kind == "model.tool_result":
            key = (payload.get("call_id"), payload.get("part", 0))
            metrics.repeated_results += key in results
            results.add(key)
            if payload.get("tool") in _EDIT_TOOLS and not payload.get("is_error"):
                metrics.edits += 1
        elif kind == "question.asked":
            metrics.questions += 1
        elif kind == "execution.resuming":
            metrics.resumed = True
        elif kind in {"workflow.awaiting_review", "workflow.cancelled"}:
            metrics.verification = payload.get("verification")
            count = payload.get("file_count")
            metrics.files_kept = count if type(count) is int else None
        elif kind == "workflow.verification":
            metrics.verification = payload.get("status")
        elif kind == "explore.started":
            efforts[payload.get("exploration_id")] = payload.get("effort")
        elif kind == "explore.finished":
            metrics.explorations.append(
                {
                    "state": payload.get("state"),
                    "tool_calls": payload.get("tool_calls"),
                    "seconds": payload.get("seconds"),
                    "retries": payload.get("retries", 0),
                    "effort": efforts.get(payload.get("exploration_id")),
                    "reason": payload.get("reason"),
                }
            )
        elif kind == "model.finished":
            # A long answer arrives in numbered parts of one message.
            part = payload.get("part", 0)
            key = (payload.get("message_id"), part)
            metrics.repeated_answers += key in answers
            answers.add(key)
            answer_parts[part if type(part) is int else 0] = str(
                payload.get("answer", "")
            )
            metrics.outcome = payload.get("outcome")
            usage = payload.get("usage")
            if isinstance(usage, Mapping):
                metrics.usage = {
                    str(key): value
                    for key, value in usage.items()
                    if type(value) is int
                }
    metrics.answer = "".join(answer_parts[part] for part in sorted(answer_parts))
    if not metrics.usage:
        metrics.usage = turn_usage
    return metrics


def checks(
    task: Task,
    state: str | None,
    metrics: Metrics,
    tasks_created: int = 1,
    interrupted: bool = False,
) -> dict[str, bool]:
    """Pass/fail for each expectation; a task passes when all of them do."""

    found, _ = fact_coverage(metrics.answer, task.facts)
    explored = metrics.main_tool_calls.get("explore", 0) > 0
    result = {
        "state": state == task.state,
        # Every tool result and answer part is recorded once, even across a
        # restart, and the run's lines all went to this one task.
        "recorded_once": not metrics.repeated_results and not metrics.repeated_answers,
        "one_task": tasks_created == 1,
    }
    if task.facts:
        result["facts"] = len(found) >= task.min_facts * len(task.facts)
    if task.explore == "never":
        result["no_explore"] = not explored
    elif task.explore == "expected":
        result["explored"] = explored
    if task.outcomes:
        result["outcome"] = metrics.outcome in task.outcomes
    if task.edits == "none":
        result["no_edits"] = metrics.edits == 0
    elif task.edits == "some":
        result["edited"] = metrics.edits > 0
    if task.verification is not None:
        result["verification"] = metrics.verification == task.verification
    if task.questions is not None:
        result["questions"] = metrics.questions == task.questions
    if task.min_answer_characters:
        result["answer_length"] = len(metrics.answer) >= task.min_answer_characters
    if task.max_main_turns is not None:
        result["turns"] = metrics.main_turns <= task.max_main_turns
    if task.interrupt is not None:
        # The interrupt happened only if the task made an edit first.
        result["interrupted"] = interrupted
    if task.interrupt == "cancel":
        result["edits_kept"] = bool(metrics.files_kept)
    elif task.interrupt == "crash":
        result["resumed"] = metrics.resumed
    return result


def task_result(
    task: Task,
    state: str | None,
    failure: str | None,
    metrics: Metrics,
    wall_seconds: float,
    suspended_seconds: float,
    tasks_created: int = 1,
    interrupted: bool = False,
) -> dict[str, Any]:
    found, missed = fact_coverage(metrics.answer, task.facts)
    usage = metrics.usage
    passed = checks(task, state, metrics, tasks_created, interrupted)
    return {
        "id": task.id,
        "passed": all(passed.values()),
        "checks": passed,
        "state": state,
        "failure_code": failure,
        "outcome": metrics.outcome,
        "facts_found": found,
        "facts_missed": missed,
        "wall_seconds": round(wall_seconds, 1),
        "suspended_seconds": round(suspended_seconds, 1),
        "main_turns": metrics.main_turns,
        "main_tool_calls": sum(metrics.main_tool_calls.values()),
        "tools": metrics.main_tool_calls,
        "peak_context_tokens": metrics.peak_context_tokens,
        # Runs from before prompt_tokens existed only report input_tokens.
        "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
        "output_tokens": usage.get("output_tokens", 0),
        "cached_tokens": usage.get("cache_read_input_tokens", 0),
        "helper_prompt_tokens": usage.get("explore_prompt_tokens", 0),
        "explorations": metrics.explorations,
        "answer_characters": len(metrics.answer),
        "questions": metrics.questions,
        "edits": metrics.edits,
        "files_kept": metrics.files_kept,
        "verification": metrics.verification,
        "repeated_results": metrics.repeated_results,
        "repeated_answers": metrics.repeated_answers,
        "tasks_created": tasks_created,
        "interrupt": task.interrupt,
    }


# Nobody is at the terminal to approve a command or a web page, and a pending
# approval would also count as a question. Sandboxed commands run without
# asking, and pages are not fetched, so runs do not depend on the network.
_DEFAULT_SETTINGS = {"commands": '"allow"', "web_fetch": '"off"'}


def write_config(directory: Path, settings: Sequence[str], profile: str) -> Path:
    """Write the run's configuration, refusing settings Loupe would reject."""

    values = dict(_DEFAULT_SETTINGS)
    for item in settings:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"--set needs KEY=VALUE, not {item!r}")
        if value.strip().lower() in {"true", "false"}:
            literal = value.strip().lower()
        elif value.strip().isdigit():
            literal = value.strip()
        else:
            literal = json.dumps(value.strip())
        values[key.strip()] = literal
    lines = ["[agent]", *(f"{key} = {literal}" for key, literal in values.items())]
    path = directory / "llm-coord" / "config.toml"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    load_settings(path, profile_id=profile)
    return path


def compare(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    """Describe how each task changed between two result files."""

    lines: list[str] = []
    if old.get("suite_version") != new.get("suite_version"):
        lines.append("! These runs used different task versions; compare with care.")
    previous = {task["id"]: task for task in old.get("tasks", [])}
    header = f"{'task':<20} {'passed':>11} {'wall s':>15} {'main calls':>13} "
    lines.append(header + f"{'prompt tokens':>19} {'cached':>17} {'facts':>9}")
    for task in new.get("tasks", []):
        before = previous.get(task["id"])
        if before is None:
            lines.append(f"{task['id']:<20} (new task)")
            continue
        passed, wall, calls, tokens, cached = (
            f"{before.get(key)} → {task.get(key)}"
            for key in (
                "passed",
                "wall_seconds",
                "main_tool_calls",
                "prompt_tokens",
                "cached_tokens",
            )
        )
        facts = f"{len(before['facts_found'])} → {len(task['facts_found'])}"
        lines.append(
            f"{task['id']:<20} {passed:>11} {wall:>15} {calls:>13} {tokens:>19} "
            f"{cached:>17} {facts:>9}"
        )
    return lines


def _loupe() -> list[str]:
    command = Path(sys.executable).with_name("llm-coord")
    if command.exists():
        return [str(command)]
    found = shutil.which("llm-coord")
    if found is None:
        raise SystemExit("llm-coord is not installed; run this with `uv run`")
    return [found]


def _clone(paths: AppPaths, revision: str) -> Path:
    """A clean checkout of the pinned revision, reused across runs."""

    clone = paths.state_dir / "benchmark" / f"repo-{revision[:12]}"
    if not (clone / ".git").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--quiet", "--no-checkout", str(ROOT), str(clone)],
            check=True,
        )
    # Loupe needs a branch to register a repository, not a detached HEAD.
    for arguments in (
        ["checkout", "--quiet", "--force", "-B", "benchmark", revision],
        ["clean", "--quiet", "-fdx"],
    ):
        subprocess.run(["git", "-C", str(clone), *arguments], check=True)
    return clone


def checks_toml(checks: Mapping[str, Mapping[str, Any]], python: str) -> str:
    """A `loupe checks configure` file for a task's checks."""

    lines: list[str] = []
    for name, spec in checks.items():
        argv = [python if item == "{python}" else item for item in spec["argv"]]
        lines += [f"[checks.{json.dumps(name)}]", f"argv = {json.dumps(argv)}"]
        lines += [
            f"{key} = {json.dumps(spec[key])}"
            for key in ("cwd", "timeout", "required")
            if key in spec
        ]
        lines.append("")
    return "\n".join(lines)


def _fixture(directory: Path, name: str) -> Path:
    """A fresh Git repository holding a copy of the fixture."""

    shutil.copytree(FIXTURES / name, directory)
    for arguments in (
        ["init", "--quiet", "--initial-branch", "main"],
        ["add", "--all"],
        [
            "-c",
            "user.name=Loupe benchmark",
            "-c",
            "user.email=benchmark@localhost",
            "commit",
            "--quiet",
            "--message",
            f"{name} fixture",
        ],
    ):
        subprocess.run(["git", "-C", str(directory), *arguments], check=True)
    return directory.resolve()


def _tasks_since(database: Path, since_ms: int) -> list[tuple[str, str | None]]:
    """Tasks created since the run started, oldest first."""

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return [
            (str(task_id), state)
            for task_id, state in connection.execute(
                "SELECT task_id, state FROM tasks WHERE created_at >= ? "
                "ORDER BY created_at, rowid",
                (since_ms,),
            )
        ]
    finally:
        connection.close()


def _settle(
    database: Path,
    task_id: str,
    deadline: float,
    *,
    answers: Sequence[str],
    reply: Callable[[str], None],
    cancel: Callable[[], None],
) -> tuple[str | None, str | None, bool]:
    """Wait for a task to stop working, once its chat has left.

    Each question the agent asks gets the task's next answer. A question with
    no answer left can never be answered, so the task is cancelled rather than
    left waiting. Returns the state, failure code, and whether that happened.
    """

    remaining = list(answers)
    stranded = False
    while True:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT state, failure_code FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        finally:
            connection.close()
        state, failure = (row[0], row[1]) if row else (None, None)
        if state in _SETTLED or time.time() >= deadline:
            return state, failure, stranded
        if not stranded and awaiting_answer(_events(database, task_id)):
            if remaining:
                reply(remaining.pop(0))
            else:
                stranded = True
                cancel()
        time.sleep(1)


def _reply(client: DaemonClient, task_id: str, answer: str) -> None:
    """Answer the task's pending question, as the chat would."""

    try:
        client.call("task.answer", {"task_id": task_id, "answer": answer})
    except LlmCoordError as exc:
        print(f"  could not answer {task_id}'s question: {exc.message}", flush=True)


def _cancel(loupe: Sequence[str], environment: Mapping[str, str], task_id: str) -> None:
    subprocess.run(
        [*loupe, "task", "cancel", task_id],
        env=environment,
        capture_output=True,
        check=False,
    )


def _crash(
    paths: AppPaths, loupe: Sequence[str], environment: Mapping[str, str]
) -> None:
    """Kill the background service without warning, then start it again."""

    pid = json.loads(paths.pid_file.read_text(encoding="utf-8"))["pid"]
    os.kill(pid, signal.SIGKILL)
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    started = subprocess.run(
        [*loupe, "daemon", "start"], env=environment, capture_output=True, check=False
    )
    # The chat may have started it first when its connection dropped.
    status = subprocess.run(
        [*loupe, "daemon", "status"], env=environment, capture_output=True, check=False
    )
    if started.returncode != 0 and status.returncode != 0:
        raise SystemExit("the background service did not restart after the crash")


def _interrupt(
    kind: str,
    process: subprocess.Popen[str],
    paths: AppPaths,
    loupe: Sequence[str],
    environment: Mapping[str, str],
    since_ms: int,
    deadline: float,
) -> bool:
    """After the task's first edit, cancel it or crash the service.

    Returns whether that happened before the chat finished on its own.
    """

    while process.poll() is None and time.time() < deadline:
        created = _tasks_since(paths.control_db, since_ms)
        if created and edit_seen(_events(paths.control_db, created[0][0])):
            if kind == "cancel":
                _cancel(loupe, environment, created[0][0])
            else:
                _crash(paths, loupe, environment)
            return True
        time.sleep(0.5)
    return False


def _events(database: Path, task_id: str) -> list[tuple[str, dict[str, Any]]]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return [
            (str(kind), json.loads(payload))
            for kind, payload in connection.execute(
                "SELECT event_type, payload_json FROM task_events "
                "WHERE task_id = ? ORDER BY sequence",
                (task_id,),
            )
        ]
    finally:
        connection.close()


def run(arguments: argparse.Namespace) -> int:
    suite = load_suite(arguments.tasks)
    tasks = [t for t in suite.tasks if not arguments.only or t.id in arguments.only]
    if not tasks:
        raise SystemExit("no task matches --only")
    profile = arguments.profile
    if profile == "default":
        raise SystemExit("use a separate, signed-in profile, not the default one")
    paths = AppPaths.resolve(profile)
    loupe = [*_loupe(), "--profile", profile]
    with tempfile.TemporaryDirectory(prefix="loupe-benchmark-") as temporary:
        # Loupe refuses configuration paths through symlinks, and macOS's
        # temporary directory is reached through one.
        config_home = Path(temporary).resolve()
        write_config(config_home, arguments.set, profile)
        environment = {**os.environ, "LLM_COORD_CONFIG_HOME": str(config_home)}
        clone = (
            _clone(paths, suite.revision)
            if any(task.fixture is None for task in tasks)
            else None
        )
        fixtures = paths.state_dir / "benchmark" / "fixtures"
        shutil.rmtree(fixtures, ignore_errors=True)
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
        chat = [*loupe, "chat", "--plain"]
        for option in ("provider", "model", "effort"):
            value = getattr(arguments, option)
            if value:
                chat += [f"--{option}", value]
        results: list[dict[str, Any]] = []
        spent = 0
        try:
            for task in tasks:
                if spent >= arguments.budget_tokens:
                    print(f"Stopping before {task.id}: the token budget is spent.")
                    break
                print(f"Running {task.id}…", flush=True)
                repository = clone
                if task.fixture is not None:
                    repository = _fixture(fixtures / task.id, task.fixture)
                    if task.checks:
                        config = Path(temporary) / f"{task.id}-checks.toml"
                        config.write_text(
                            checks_toml(task.checks, sys.executable), encoding="utf-8"
                        )
                        for command in (
                            ["repo", "add", str(repository)],
                            [
                                "checks",
                                "configure",
                                *("--repo", str(repository)),
                                *("--file", str(config)),
                            ],
                        ):
                            subprocess.run(
                                [*loupe, *command],
                                env=environment,
                                capture_output=True,
                                check=True,
                            )
                assert repository is not None
                since = int(time.time() * 1000)
                wall, monotonic = time.time(), time.monotonic()
                deadline = wall + arguments.timeout
                log_path = paths.state_dir / "benchmark" / f"{task.id}.log"
                with open(log_path, "w") as log:
                    process = subprocess.Popen(
                        [*chat, "--mode", task.mode, "--repo", str(repository)],
                        stdin=subprocess.PIPE,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        env=environment,
                    )
                    assert process.stdin is not None
                    # Only the prompt is typed. A question finds no more input,
                    # so the chat leaves it pending and exits, and the answer
                    # is given below, only if the agent asked.
                    process.stdin.write(task.prompt + "\n")
                    process.stdin.close()
                    interrupted = task.interrupt is not None and _interrupt(
                        task.interrupt,
                        process,
                        paths,
                        loupe,
                        environment,
                        since,
                        deadline,
                    )
                    try:
                        process.wait(timeout=max(1.0, deadline - time.time()))
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                created = _tasks_since(paths.control_db, since)
                if not created:
                    raise SystemExit(f"the run created no task; see {log_path}")
                task_id = created[0][0]
                # After a crash or a question, the chat can leave while the
                # task still works.
                state, failure, stranded = _settle(
                    paths.control_db,
                    task_id,
                    deadline,
                    answers=task.answers,
                    reply=functools.partial(_reply, DaemonClient(paths), task_id),
                    cancel=functools.partial(_cancel, loupe, environment, task_id),
                )
                wall_seconds = time.time() - wall
                suspended = max(0.0, wall_seconds - (time.monotonic() - monotonic))
                metrics = summarize_events(_events(paths.control_db, task_id))
                result = task_result(
                    task,
                    state,
                    failure,
                    metrics,
                    wall_seconds,
                    suspended,
                    tasks_created=len(created),
                    interrupted=interrupted,
                )
                result["task_id"] = task_id
                # Left waiting on a question after its chat had gone.
                result["stranded_question"] = stranded
                results.append(result)
                spent += result["prompt_tokens"] + result["output_tokens"]
                print(_line(result), flush=True)
        finally:
            subprocess.run([*loupe, "daemon", "stop"], capture_output=True, check=False)
    output = arguments.out or (
        paths.state_dir / "benchmark" / f"results-{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    document = {
        "suite_version": suite.version,
        "revision": suite.revision,
        "loupe_commit": head,
        "settings": {
            "set": list(arguments.set),
            "provider": arguments.provider,
            "model": arguments.model,
            "effort": arguments.effort,
        },
        "tasks": results,
    }
    Path(output).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"Results: {output}")
    return 0 if results and all(result["passed"] for result in results) else 1


def _line(result: Mapping[str, Any]) -> str:
    status = "pass" if result["passed"] else "FAIL"
    failed = [name for name, ok in result["checks"].items() if not ok]
    line = (
        f"  {status} {result['id']}: {result['state']}, {result['wall_seconds']}s, "
        f"{result['main_tool_calls']} main calls, "
        f"{len(result['explorations'])} explorations, "
        f"{result['prompt_tokens']:,} prompt tokens"
    )
    facts = len(result["facts_found"]) + len(result["facts_missed"])
    if facts:
        line += f", facts {len(result['facts_found'])}/{facts}"
    if failed:
        line += f" (failed: {', '.join(failed)})"
    if result["suspended_seconds"] > _SUSPENSION_NOTICE_SECONDS:
        line += f"; the machine slept {result['suspended_seconds']}s, so time is off"
    return line


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", help="a signed-in profile other than default")
    parser.add_argument("--tasks", type=Path, default=TASKS, help="task file")
    parser.add_argument("--only", nargs="+", metavar="ID", help="run these tasks")
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
        default=3_000_000,
        help="start no task once this many tokens are spent (default 3M)",
    )
    parser.add_argument(
        "--timeout", type=int, default=1_800, help="seconds allowed per task"
    )
    parser.add_argument("--out", type=Path, help="where to write the results")
    parser.add_argument(
        "--compare", nargs=2, type=Path, metavar=("OLD", "NEW"), help="compare runs"
    )
    arguments = parser.parse_args(argv)
    if arguments.compare:
        old, new = (json.loads(path.read_text()) for path in arguments.compare)
        print("\n".join(compare(old, new)))
        return 0
    if not arguments.profile:
        parser.error("--profile is required to run the benchmark")
    return run(arguments)


if __name__ == "__main__":
    sys.exit(main())
