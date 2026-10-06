"""Answer real repository questions with a real model and measure the result.

Run with `uv run --extra openai python scripts/live_benchmark.py --profile NAME`.

Every task makes billed model requests, so the benchmark never runs in CI or
the test suite. NAME must be a profile you have already signed in to (`loupe
--profile NAME`, then `/login`); the default profile is refused, because the
benchmark restarts the profile's background service. For the run, that service
gets its own configuration, built from `--set` options, so your configuration
file is not used or changed. Each task is answered in a clone of this
repository at the revision the task file names. Results are written as JSON;
`--compare OLD NEW` prints how two runs differ.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm_cli.config.loader import load_settings
from llm_cli.paths import AppPaths

ROOT = Path(__file__).resolve().parents[1]
TASKS = Path(__file__).with_name("live_benchmark.toml")
_EXPLORE_EXPECTATIONS = {"any", "never", "expected"}
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
        )
        if task.mode not in {"plan", "normal", "auto"}:
            raise ValueError(f"task {task.id!r}: unknown mode {task.mode!r}")
        if task.explore not in _EXPLORE_EXPECTATIONS:
            raise ValueError(f"task {task.id!r}: unknown explore {task.explore!r}")
        if not task.prompt or not 0 < task.min_facts <= 1:
            raise ValueError(f"task {task.id!r}: needs a prompt and 0 < min_facts <= 1")
        tasks.append(task)
    ids = [task.id for task in tasks]
    if not tasks or len(set(ids)) != len(ids):
        raise ValueError("the task file needs tasks with unique ids")
    return Suite(version, revision, tuple(tasks))


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


def summarize_events(events: Iterable[tuple[str, Mapping[str, Any]]]) -> Metrics:
    """Measure one task from its durable events, in sequence order."""

    metrics = Metrics()
    answer_parts: dict[int, str] = {}
    efforts: dict[object, object] = {}
    for kind, payload in events:
        if kind == "model.turn.completed":
            metrics.main_turns += 1
            usage = payload.get("usage") or {}
            context = payload.get("context_tokens")
            if type(context) is not int:
                context = usage.get("prompt_tokens", usage.get("input_tokens", 0))
            metrics.peak_context_tokens = max(metrics.peak_context_tokens, context)
        elif kind == "model.tool_call":
            tool = str(payload.get("tool"))
            metrics.main_tool_calls[tool] = metrics.main_tool_calls.get(tool, 0) + 1
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
    return metrics


def checks(task: Task, state: str | None, metrics: Metrics) -> dict[str, bool]:
    """Pass/fail for each expectation; a task passes when all of them do."""

    found, _ = fact_coverage(metrics.answer, task.facts)
    explored = metrics.main_tool_calls.get("explore", 0) > 0
    result = {"completed": state == "completed"}
    if task.facts:
        result["facts"] = len(found) >= task.min_facts * len(task.facts)
    if task.explore == "never":
        result["no_explore"] = not explored
    elif task.explore == "expected":
        result["explored"] = explored
    return result


def task_result(
    task: Task,
    state: str | None,
    failure: str | None,
    metrics: Metrics,
    wall_seconds: float,
    suspended_seconds: float,
) -> dict[str, Any]:
    found, missed = fact_coverage(metrics.answer, task.facts)
    usage = metrics.usage
    passed = checks(task, state, metrics)
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
    }


def write_config(directory: Path, settings: Sequence[str], profile: str) -> Path:
    """Write the run's configuration, refusing settings Loupe would reject."""

    lines = ["[agent]"]
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
        lines.append(f"{key.strip()} = {literal}")
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
    lines.append(header + f"{'prompt tokens':>19} {'facts':>9}")
    for task in new.get("tasks", []):
        before = previous.get(task["id"])
        if before is None:
            lines.append(f"{task['id']:<20} (new task)")
            continue
        passed, wall, calls, tokens = (
            f"{before.get(key)} → {task.get(key)}"
            for key in ("passed", "wall_seconds", "main_tool_calls", "prompt_tokens")
        )
        facts = f"{len(before['facts_found'])} → {len(task['facts_found'])}"
        lines.append(
            f"{task['id']:<20} {passed:>11} {wall:>15} {calls:>13} {tokens:>19} "
            f"{facts:>9}"
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


def _new_task(
    database: Path, since_ms: int, log: Path
) -> tuple[str, str | None, str | None]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT task_id, state, failure_code FROM tasks WHERE created_at >= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (since_ms,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"the run created no task; see {log}")
        return str(row[0]), row[1], row[2]
    finally:
        connection.close()


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
        clone = _clone(paths, suite.revision)
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
                since = int(time.time() * 1000)
                wall, monotonic = time.time(), time.monotonic()
                log_path = paths.state_dir / "benchmark" / f"{task.id}.log"
                with open(log_path, "w") as log:
                    subprocess.run(
                        [*chat, "--mode", task.mode, "--repo", str(clone)],
                        input=task.prompt + "\n",
                        text=True,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        env=environment,
                        timeout=arguments.timeout,
                        check=False,
                    )
                wall_seconds = time.time() - wall
                suspended = max(0.0, wall_seconds - (time.monotonic() - monotonic))
                task_id, state, failure = _new_task(paths.control_db, since, log_path)
                metrics = summarize_events(_events(paths.control_db, task_id))
                result = task_result(
                    task, state, failure, metrics, wall_seconds, suspended
                )
                result["task_id"] = task_id
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
        f"  {status} {result['id']}: {result['wall_seconds']}s, "
        f"{result['main_tool_calls']} main calls, "
        f"{len(result['explorations'])} explorations, "
        f"{result['prompt_tokens']:,} prompt tokens, "
        f"facts {len(result['facts_found'])}/"
        f"{len(result['facts_found']) + len(result['facts_missed'])}"
    )
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
