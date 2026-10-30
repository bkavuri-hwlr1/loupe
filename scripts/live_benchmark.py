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
import ast
import functools
import json
import os
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm_cli.config.loader import load_settings
from llm_cli.errors import LlmCoordError
from llm_cli.execution.sandbox import (
    SandboxPolicy,
    available_sandbox,
    protected_home_paths,
    wrap,
)
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
    # Require one completed independent review with at least this many findings.
    min_review_findings: int | None = None
    # B6 corpus metadata; the exact planted replacement must precede review.
    review_kind: str | None = None
    seed_path: str | None = None
    seed_before: str | None = None
    seed_after: str | None = None
    acceptance: str | None = None
    # B7 starts from real historical bugs and never prescribes a planted edit.
    natural_obvious: bool = False


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
            min_review_findings=raw.get("min_review_findings"),
            review_kind=raw.get("review_kind"),
            seed_path=raw.get("seed_path"),
            seed_before=raw.get("seed_before"),
            seed_after=raw.get("seed_after"),
            acceptance=raw.get("acceptance"),
            natural_obvious=raw.get("natural_obvious", False),
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
    if task.min_review_findings is not None and (
        type(task.min_review_findings) is not int
        or not 0 <= task.min_review_findings <= 10
        or task.mode == "plan"
    ):
        raise refuse("min_review_findings needs an edit mode and a count from 0 to 10")

    if task.review_kind is not None:
        if task.review_kind == "natural":
            if task.mode != "auto" or task.fixture is None:
                raise refuse("natural review tasks need an auto-mode fixture")
            if type(task.natural_obvious) is not bool:
                raise refuse("natural_obvious must be a boolean")
            if any((task.seed_path, task.seed_before, task.seed_after)):
                raise refuse("natural tasks cannot prescribe a planted edit")
            if (
                not isinstance(task.acceptance, str)
                or not task.acceptance.replace("-", "").isalnum()
            ):
                raise refuse("natural tasks need hidden acceptance")
            hidden = Path(__file__).with_name("benchmark_acceptance") / task.acceptance
            if (
                not (hidden / "test_acceptance.py").is_file()
                or not (hidden / "provenance.json").is_file()
            ):
                raise refuse("natural tasks need acceptance and pinned provenance")
            if "\n" in task.prompt or "\r" in task.prompt:
                raise refuse("piped natural prompts must use one physical line")
            return
        if task.review_kind not in {"bug", "clean"} or task.mode != "auto":
            raise refuse("review_kind needs bug or clean in auto mode")
        if not all(
            isinstance(v, str) and v
            for v in (
                task.seed_path,
                task.seed_before,
                task.seed_after,
                task.acceptance,
            )
        ):
            raise refuse("review corpus tasks need seed and acceptance metadata")
        assert task.seed_path and task.seed_before and task.acceptance
        path = Path(task.seed_path)
        if path.is_absolute() or ".." in path.parts or task.fixture is None:
            raise refuse("seed_path must stay in its fixture")
        if not task.acceptance.replace("-", "").isalnum():
            raise refuse("acceptance must name a fixture")
        hidden = Path(__file__).with_name("benchmark_acceptance") / task.acceptance
        if not hidden.is_dir() or not any(hidden.glob("test_*.py")):
            raise refuse("review corpus needs hidden acceptance tests")
        source = (FIXTURES / task.fixture / path).read_text()
        if source.count(task.seed_before) != 1 or task.seed_after not in task.prompt:
            raise refuse("seed must match one fixture line and appear in the prompt")
        if "\n" in task.prompt:
            raise refuse("piped review prompts must use one physical line")


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
    retrieval_calls: int = 0
    helper_retrieval_calls: int = 0
    resumed: bool = False
    review_attempts: int = 0
    reviews: list[dict[str, object]] = field(default_factory=list)
    review_seconds: float = 0.0
    review_incomplete: int = 0
    review_timeouts: int = 0
    review_repair_edits: int = 0


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
    review_started_ms: int | None = None
    repairing_review = False
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
        elif kind == "retrieval.completed":
            metrics.retrieval_calls += 1
            metrics.helper_retrieval_calls += int(bool(payload.get("helper")))
        elif kind == "model.tool_call":
            tool = str(payload.get("tool"))
            metrics.main_tool_calls[tool] = metrics.main_tool_calls.get(tool, 0) + 1
        elif kind == "model.tool_result":
            key = (payload.get("call_id"), payload.get("part", 0))
            metrics.repeated_results += key in results
            results.add(key)
            if payload.get("tool") in _EDIT_TOOLS and not payload.get("is_error"):
                metrics.edits += 1
                metrics.review_repair_edits += int(repairing_review)
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
        elif kind == "review.started":
            metrics.review_attempts += 1
            stamp = payload.get("_benchmark_created_at")
            review_started_ms = stamp if type(stamp) is int else None
        elif kind == "review.finished" and len(metrics.reviews) < 8:
            count = payload.get("findings")
            state = payload.get("state")
            repairing_review = state == "completed" and type(count) is int and count > 0
            metrics.review_incomplete += int(state != "completed")
            reason = payload.get("reason")
            if state != "completed" and isinstance(reason, str):
                metrics.review_timeouts += int(
                    any(
                        word in reason.lower()
                        for word in (
                            "timeout",
                            "timed out",
                            "time limit",
                            "deadline",
                            "time budget",
                        )
                    )
                )
            stamp = payload.get("_benchmark_created_at")
            if review_started_ms is not None and type(stamp) is int:
                metrics.review_seconds += max(0, stamp - review_started_ms) / 1000
            review_started_ms = None
            metrics.reviews.append(
                {
                    "state": (
                        state
                        if isinstance(state, str) and state in {"completed", "failed"}
                        else "invalid"
                    ),
                    "findings": count if type(count) is int and 0 <= count <= 10 else 0,
                }
            )
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
    if task.min_review_findings is not None:
        result["independent_review"] = (
            metrics.review_attempts == 1
            and len(metrics.reviews) == 1
            and metrics.reviews[0]["state"] == "completed"
            and int(metrics.reviews[0]["findings"]) >= task.min_review_findings
        )
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
        "all_tool_calls": sum(metrics.main_tool_calls.values())
        + sum(int(item.get("tool_calls", 0)) for item in metrics.explorations),
        "retrieval_calls": metrics.retrieval_calls,
        "helper_retrieval_calls": metrics.helper_retrieval_calls,
        "tools": metrics.main_tool_calls,
        "peak_context_tokens": metrics.peak_context_tokens,
        # Runs from before prompt_tokens existed only report input_tokens.
        "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
        "output_tokens": usage.get("output_tokens", 0),
        "cached_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_write_tokens": usage.get("cache_creation_input_tokens", 0),
        "helper_prompt_tokens": usage.get("explore_prompt_tokens", 0),
        "explorations": metrics.explorations,
        "review_attempts": metrics.review_attempts,
        "reviews": metrics.reviews,
        "review_prompt_tokens": usage.get("review_prompt_tokens", 0),
        "review_output_tokens": usage.get("review_output_tokens", 0),
        "review_cached_tokens": usage.get("review_cache_read_input_tokens", 0),
        "review_cache_write_tokens": usage.get("review_cache_creation_input_tokens", 0),
        "review_seconds": round(metrics.review_seconds, 3),
        "review_incomplete": metrics.review_incomplete,
        "review_timeouts": metrics.review_timeouts,
        "review_repair_edits": metrics.review_repair_edits,
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
RETRIEVAL_TASKS = frozenset(
    {
        "trace-shared-edit",
        "repo-summary",
        "where-source-privacy",
        "where-index-updates",
        "where-final-answer",
    }
)

_DEFAULT_SETTINGS = {"commands": '"allow"', "web_fetch": '"off"'}


def write_config(directory: Path, settings: Sequence[str], profile: str) -> Path:
    """Write the run's configuration, refusing settings Loupe would reject."""

    values = dict(_DEFAULT_SETTINGS)
    knowledge: str | None = None
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
        if key.strip() == "knowledge.index":
            if value.strip() not in {"off", "lexical"}:
                raise ValueError("knowledge.index must be off or lexical")
            knowledge = value.strip()
        else:
            values[key.strip()] = literal
    lines = ["[agent]", *(f"{key} = {literal}" for key, literal in values.items())]
    if knowledge is not None:
        lines.extend(["", "[knowledge]", f"index = {json.dumps(knowledge)}"])
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

    def key(task: Mapping[str, Any]) -> tuple[object, ...]:
        return (
            task["id"],
            task.get("repetition", 1),
            task.get("review_arm", "off"),
            task.get("retrieval_arm", "off"),
        )

    previous = {key(task): task for task in old.get("tasks", [])}
    header = f"{'task':<20} {'passed':>11} {'wall s':>15} {'main calls':>13} "
    lines.append(header + f"{'prompt tokens':>19} {'cached':>17} {'facts':>9}")
    for task in new.get("tasks", []):
        before = previous.get(key(task))
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
    if any(task.get("review_kind") for task in new.get("tasks", [])):
        lines += ["", "Review arms (counts and tokens are totals; time is median):"]
        for label, document in (("old", old), ("new", new)):
            for arm, summary in review_summary(document).items():
                lines.append(f"{label} {arm}: {json.dumps(summary, sort_keys=True)}")
    if new.get("retrieval_paired"):
        lines += ["", "Retrieval arms (totals; task time excludes index builds):"]
        for label, document in (("old", old), ("new", new)):
            for arm, summary in retrieval_summary(document).items():
                lines.append(f"{label} {arm}: {json.dumps(summary, sort_keys=True)}")
    return lines


def seed_observed(task: Task, events: Sequence[tuple[str, Mapping[str, Any]]]) -> bool:
    """Replay successful source edits and require the exact first proposal.

    Unsupported mutations invalidate the witness instead of guessing from a
    fragment that might have been repaired before review began.
    """
    if not all((task.fixture, task.seed_path, task.seed_before, task.seed_after)):
        return False
    original = (FIXTURES / str(task.fixture) / str(task.seed_path)).read_text()
    expected = original.replace(str(task.seed_before), str(task.seed_after), 1)
    bodies: dict[str, str | None] = {str(task.seed_path): original}
    pending: dict[object, tuple[object, Mapping[str, Any]]] = {}
    changed = False
    for kind, payload in events:
        if kind == "review.started":
            return changed and bodies.get(str(task.seed_path)) == expected
        if kind == "model.tool_call":
            arguments = payload.get("arguments")
            if isinstance(arguments, Mapping):
                pending[payload.get("call_id")] = (payload.get("tool"), arguments)
        elif kind == "model.tool_result" and payload.get("call_id") in pending:
            tool, arguments = pending.pop(payload.get("call_id"))
            if payload.get("is_error"):
                continue
            path = arguments.get("path")
            if tool == "rename_file":
                source, destination = (
                    arguments.get("source"),
                    arguments.get("destination"),
                )
                if isinstance(source, str) and isinstance(destination, str):
                    bodies[destination] = bodies.pop(source, None)
                    changed |= task.seed_path in {source, destination}
            elif isinstance(path, str) and path in bodies:
                body = bodies[path]
                if tool == "write_file":
                    text = arguments.get("content")
                    bodies[path] = text if isinstance(text, str) else None
                elif tool == "apply_patch":
                    old, new = arguments.get("old_text"), arguments.get("new_text")
                    bodies[path] = (
                        body.replace(old, new, 1)
                        if isinstance(body, str)
                        and isinstance(old, str)
                        and old
                        and isinstance(new, str)
                        and body.count(old) == 1
                        else None
                    )
                elif tool in {"delete_file", "write_binary", "write_symlink"}:
                    bodies[path] = None
                else:
                    continue
                changed |= path == task.seed_path
    return changed and bodies.get(str(task.seed_path)) == expected


def review_summary(document: Mapping[str, Any]) -> dict[str, Any]:
    """Report measured repairs, not semantic precision inferred from counts."""
    natural = [
        t for t in document.get("tasks", []) if t.get("review_kind") == "natural"
    ]
    if natural:
        return natural_review_summary(natural)
    summaries: dict[str, Any] = {}
    tasks = document.get("tasks", [])
    arms = sorted(
        {str(t.get("review_arm", "unspecified")) for t in tasks if t.get("review_kind")}
    )
    for arm in arms:
        rows = [
            t
            for t in tasks
            if t.get("review_kind") and t.get("review_arm", "unspecified") == arm
        ]
        measured = [
            t
            for t in rows
            if t.get("seed_observed")
            and t.get("state") == "completed"
            and t.get("passed")
            and t.get("hidden_acceptance", {}).get("ran")
        ]
        bugs = [t for t in measured if t["review_kind"] == "bug"]
        clean = [t for t in measured if t["review_kind"] == "clean"]
        fixed = sum(t["hidden_acceptance"].get("passed", False) for t in bugs)
        review_fixed = sum(
            bool(
                t["hidden_acceptance"].get("passed")
                and t.get("review_repair_edits")
                and any(r.get("findings", 0) > 0 for r in t.get("reviews", []))
            )
            for t in bugs
        )
        harmed = sum(
            bool(
                not t["hidden_acceptance"].get("passed")
                and t.get("review_repair_edits")
                and any(r.get("findings", 0) > 0 for r in t.get("reviews", []))
            )
            for t in clean
        )
        summary = {
            "runs": len(rows),
            "measured_runs": len(measured),
            "invalid_or_incomplete_runs": len(rows) - len(measured),
            "bug_runs": len(bugs),
            "bugs_fixed": fixed,
            "hidden_pass_rate": fixed / len(bugs) if bugs else None,
            "review_caught_and_fixed": review_fixed,
            "clean_runs": len(clean),
            "harmful_review_repairs": harmed,
            "clean_failures": sum(
                not t["hidden_acceptance"].get("passed", False) for t in clean
            ),
            "findings": sum(
                sum(r.get("findings", 0) for r in t.get("reviews", [])) for t in rows
            ),
            "timeouts": sum(t.get("review_timeouts", 0) for t in rows),
            "incomplete_reviews": sum(t.get("review_incomplete", 0) for t in rows),
            "prompt_tokens": sum(t.get("prompt_tokens", 0) for t in rows),
            "output_tokens": sum(t.get("output_tokens", 0) for t in rows),
            "cached_tokens": sum(t.get("cached_tokens", 0) for t in rows),
            "cache_write_tokens": sum(t.get("cache_write_tokens", 0) for t in rows),
            "review_prompt_tokens": sum(t.get("review_prompt_tokens", 0) for t in rows),
            "review_output_tokens": sum(t.get("review_output_tokens", 0) for t in rows),
            "review_cached_tokens": sum(t.get("review_cached_tokens", 0) for t in rows),
            "review_cache_write_tokens": sum(
                t.get("review_cache_write_tokens", 0) for t in rows
            ),
            "median_wall_seconds": statistics.median(
                t["wall_seconds"] for t in measured
            )
            if measured
            else None,
            "median_review_seconds": statistics.median(
                t.get("review_seconds", 0) for t in measured
            )
            if measured
            else None,
            # Subscription usage is not an API dollar price. A price policy
            # must be supplied separately before any currency can be claimed.
            "cost_usd": None,
            "per_case": {},
        }
        for case in sorted({t["id"] for t in rows}):
            attempted = [t for t in rows if t["id"] == case]
            repeats = [t for t in measured if t["id"] == case]
            summary["per_case"][case] = {
                "runs": len(attempted),
                "measured_runs": len(repeats),
                "invalid_or_incomplete_runs": len(attempted) - len(repeats),
                "hidden_passes": sum(
                    bool(t.get("hidden_acceptance", {}).get("passed")) for t in repeats
                ),
                "median_wall_seconds": statistics.median(
                    t["wall_seconds"] for t in repeats
                )
                if repeats
                else None,
                "median_tokens": statistics.median(
                    t.get("prompt_tokens", 0) + t.get("output_tokens", 0)
                    for t in repeats
                )
                if repeats
                else None,
            }
        all_tokens = summary["prompt_tokens"] + summary["output_tokens"]
        review_tokens = (
            summary["review_prompt_tokens"] + summary["review_output_tokens"]
        )
        summary["review_token_share"] = (
            review_tokens / all_tokens if all_tokens else None
        )
        summary["usage_accounting_valid"] = review_tokens <= all_tokens
        summaries[arm] = summary
    return summaries


def valid_natural_measurement(row: Mapping[str, Any]) -> bool:
    checks = row.get("checks", {})
    return bool(
        row.get("initial_acceptance", {}).get("witnessed")
        and row.get("initial_acceptance", {}).get("ran")
        and row.get("hidden_acceptance", {}).get("ran")
        and checks.get("one_task")
        and checks.get("recorded_once")
        and not row.get("runner_timed_out")
        and row.get("failure_code") != "PROVIDER_UNAVAILABLE"
    )


def natural_matched_pairs(rows: Sequence[Mapping[str, Any]]) -> int:
    pairs: dict[tuple[object, object], set[str]] = {}
    for row in rows:
        if valid_natural_measurement(row):
            pairs.setdefault((row.get("id"), row.get("repetition")), set()).add(
                str(row.get("review_arm"))
            )
    return sum(arms == {"off", "on"} for arms in pairs.values())


def natural_review_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Score actual first/final candidates; a finding alone proves no catch."""
    summaries: dict[str, Any] = {}
    for arm in ("off", "on"):
        attempted = [row for row in rows if row.get("review_arm") == arm]
        if not attempted:
            continue
        measured = [row for row in attempted if valid_natural_measurement(row)]

        def repaired(row: Mapping[str, Any]) -> bool:
            return bool(
                row.get("review_repair_edits")
                and any(
                    review.get("findings", 0) > 0 for review in row.get("reviews", [])
                )
            )

        total_tokens = sum(
            row.get("prompt_tokens", 0) + row.get("output_tokens", 0)
            for row in attempted
        )
        reviewer_tokens = sum(
            row.get("review_prompt_tokens", 0) + row.get("review_output_tokens", 0)
            for row in attempted
        )
        passed = sum(bool(row["hidden_acceptance"].get("passed")) for row in measured)
        summaries[arm] = {
            "runs": len(attempted),
            "measured_runs": len(measured),
            "invalid_or_incomplete_runs": len(attempted) - len(measured),
            "hidden_passes": passed,
            "hidden_pass_rate": passed / len(attempted),
            "initial_hidden_passes": sum(
                bool(row["initial_acceptance"].get("passed")) for row in measured
            ),
            "candidate_review_catches": sum(
                not row["initial_acceptance"].get("passed")
                and bool(row["hidden_acceptance"].get("passed"))
                and repaired(row)
                for row in measured
            ),
            "harmful_review_repairs": sum(
                bool(row["initial_acceptance"].get("passed"))
                and not row["hidden_acceptance"].get("passed")
                and repaired(row)
                for row in measured
            ),
            "findings": sum(
                sum(review.get("findings", 0) for review in row.get("reviews", []))
                for row in attempted
            ),
            "timeouts": sum(row.get("review_timeouts", 0) for row in attempted),
            "incomplete_reviews": sum(
                row.get("review_incomplete", 0) for row in attempted
            ),
            "prompt_tokens": sum(row.get("prompt_tokens", 0) for row in attempted),
            "output_tokens": sum(row.get("output_tokens", 0) for row in attempted),
            "review_prompt_tokens": sum(
                row.get("review_prompt_tokens", 0) for row in attempted
            ),
            "review_output_tokens": sum(
                row.get("review_output_tokens", 0) for row in attempted
            ),
            "review_token_share": reviewer_tokens / total_tokens
            if total_tokens
            else None,
            "usage_accounting_valid": reviewer_tokens <= total_tokens,
            "median_wall_seconds": statistics.median(
                row["wall_seconds"] for row in measured
            )
            if measured
            else None,
            "median_review_seconds": statistics.median(
                row.get("review_seconds", 0) for row in measured
            )
            if measured
            else None,
            "cost_usd": None,
            "per_case": {
                case: {
                    "runs": sum(row["id"] == case for row in attempted),
                    "hidden_passes": sum(
                        row["id"] == case
                        and bool(row["hidden_acceptance"].get("passed"))
                        for row in measured
                    ),
                    "initial_hidden_passes": sum(
                        row["id"] == case
                        and bool(row["initial_acceptance"].get("passed"))
                        for row in measured
                    ),
                }
                for case in sorted({row["id"] for row in attempted})
            },
        }
    return summaries


def natural_first_acceptance(
    task: Task,
    events: Sequence[tuple[str, Mapping[str, Any]]],
    protected: Sequence[Path],
) -> dict[str, Any]:
    """Reconstruct successful edits before review without executing candidate code.

    Unknown or ambiguous mutations make attribution unavailable. Hidden tests
    still run only through the existing sandboxed, witnessed acceptance runner.
    """
    assert task.fixture is not None
    pending: dict[object, tuple[object, Mapping[str, Any]]] = {}
    completed: dict[object, object] = {}
    mutations = _EDIT_TOOLS | {"write_binary", "write_symlink"}
    with tempfile.TemporaryDirectory(prefix="loupe-natural-first-") as temporary:
        root = Path(temporary).resolve() / "repo"
        shutil.copytree(FIXTURES / task.fixture, root)

        def target(value: object) -> Path:
            if (
                not isinstance(value, str)
                or not value
                or "\\" in value
                or "\0" in value
            ):
                raise ValueError("invalid replay path")
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
                raise ValueError("invalid replay path")
            return root / path

        try:
            for kind, payload in events:
                if kind == "review.started":
                    if pending:
                        raise ValueError("unfinished mutation before review")
                    break
                if kind == "model.tool_call":
                    tool = payload.get("tool")
                    if tool not in mutations:
                        continue
                    arguments = payload.get("arguments")
                    call_id = payload.get("call_id")
                    if (
                        not isinstance(arguments, Mapping)
                        or not isinstance(call_id, str)
                        or not call_id
                        or call_id in pending
                    ):
                        raise ValueError("unwitnessed mutation")
                    pending[call_id] = (tool, arguments)
                elif kind == "model.tool_result" and payload.get("tool") in mutations:
                    call_id = payload.get("call_id")
                    part = payload.get("part", 0)
                    if (
                        type(part) is int
                        and part > 0
                        and completed.get(call_id) == payload.get("tool")
                    ):
                        continue
                    if call_id not in pending:
                        raise ValueError("mutation result without matching call")
                    tool, arguments = pending.pop(call_id)
                    if payload.get("tool") != tool:
                        raise ValueError("mutation tool changed")
                    completed[call_id] = tool
                    if payload.get("is_error"):
                        continue
                    if tool == "rename_file":
                        source = target(arguments.get("source"))
                        destination = target(arguments.get("destination"))
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        source.rename(destination)
                    elif tool in {
                        "write_file",
                        "apply_patch",
                        "delete_file",
                        "create_directory",
                    }:
                        path = target(arguments.get("path"))
                        if tool == "create_directory":
                            path.mkdir(parents=True, exist_ok=True)
                        elif tool == "delete_file":
                            path.unlink()
                        else:
                            if tool == "write_file":
                                content = arguments.get("content")
                                if not isinstance(content, str):
                                    raise ValueError("nontext replay")
                            else:
                                old, new = (
                                    arguments.get("old_text"),
                                    arguments.get("new_text"),
                                )
                                content = path.read_text()
                                if (
                                    not isinstance(old, str)
                                    or not old
                                    or not isinstance(new, str)
                                    or content.count(old) != 1
                                ):
                                    raise ValueError("ambiguous replay")
                                content = content.replace(old, new, 1)
                            if len(content.encode()) > 1024 * 1024:
                                raise ValueError("oversized replay")
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_text(content)
                    elif tool in {"write_binary", "write_symlink"}:
                        raise ValueError("unsupported replay")
                    inventory = [path for path in root.rglob("*") if path.is_file()]
                    if (
                        len(inventory) > 256
                        or sum(path.stat().st_size for path in inventory)
                        > 8 * 1024 * 1024
                    ):
                        raise ValueError("oversized replay inventory")
            if pending:
                raise ValueError("unfinished mutation")
        except (OSError, UnicodeError, ValueError):
            return {
                "ran": False,
                "passed": False,
                "witnessed": False,
                "reason": "initial candidate could not be reconstructed exactly",
            }
        result = hidden_acceptance(task, root, protected)
        result["witnessed"] = True
        return result


def retrieval_summary(document: Mapping[str, Any]) -> dict[str, Any]:
    """Matched task arms; include failures rather than cherry-picking answers."""
    rows = document.get("tasks", [])
    summaries: dict[str, Any] = {}
    for arm in ("off", "on"):
        measured = [row for row in rows if row.get("retrieval_arm") == arm]
        if not measured:
            continue
        summaries[arm] = {
            "runs": len(measured),
            "passed": sum(bool(row["passed"]) for row in measured),
            "facts_found": sum(len(row["facts_found"]) for row in measured),
            "facts_total": sum(
                len(row["facts_found"]) + len(row["facts_missed"]) for row in measured
            ),
            "prompt_tokens": sum(row["prompt_tokens"] for row in measured),
            "output_tokens": sum(row["output_tokens"] for row in measured),
            "tool_calls": sum(row["all_tool_calls"] for row in measured),
            "retrieval_calls": sum(row["retrieval_calls"] for row in measured),
            "median_wall_seconds": statistics.median(
                row["wall_seconds"] for row in measured
            ),
            "index_build_seconds": sum(row["index_build_seconds"] for row in measured),
            "cost_usd": None,
            "per_case": {},
        }
        for case in sorted({row["id"] for row in measured}):
            cases = [row for row in measured if row["id"] == case]
            summaries[arm]["per_case"][case] = {
                "runs": len(cases),
                "passed": sum(bool(row["passed"]) for row in cases),
                "median_wall_seconds": statistics.median(
                    row["wall_seconds"] for row in cases
                ),
                "median_tokens": statistics.median(
                    row["prompt_tokens"] + row["output_tokens"] for row in cases
                ),
                "median_tool_calls": statistics.median(
                    row["all_tool_calls"] for row in cases
                ),
            }
    return summaries


def hidden_acceptance(
    task: Task, repository: Path, protected: Sequence[Path]
) -> dict[str, Any]:
    """Execute only shipped hidden tests on a sandboxed disposable copy."""
    if task.acceptance is None:
        return {"ran": False, "reason": "not a review corpus task"}
    kind = available_sandbox()
    if kind is None:
        return {"ran": False, "reason": "no working sandbox"}
    tests = Path(__file__).with_name("benchmark_acceptance") / task.acceptance
    expected = sorted(
        node.name
        for path in tests.glob("test_*.py")
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )
    with tempfile.TemporaryDirectory(prefix="loupe-review-acceptance-") as temporary:
        root = Path(temporary).resolve()
        copy = root / "repo"
        shutil.copytree(
            repository,
            copy,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
        )
        hidden = root / "hidden_acceptance"
        shutil.copytree(tests, hidden)
        configuration = root / "pytest.ini"
        configuration.write_text("[pytest]\n", encoding="utf-8")
        report = root / "acceptance.xml"
        environment = {
            "PATH": os.defpath,
            "HOME": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "BENCHMARK_REPOSITORY": str(copy),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        command = [
            sys.executable,
            "-I",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--noconftest",
            "-c",
            str(configuration),
            f"--junitxml={report}",
            str(hidden),
        ]
        completed = subprocess.run(
            wrap(
                kind,
                SandboxPolicy(
                    writable=(root,),
                    protected=tuple(protected),
                    # Worktrees may live below a protected .codex directory.
                    # Expose the interpreter environment, never its sibling
                    # source, hidden acceptance corpus or credential stores.
                    readable=(Path(sys.prefix).resolve(),),
                ),
                command,
                cwd=copy,
            ),
            cwd=copy,
            env=environment,
            capture_output=True,
            timeout=120,
            check=False,
        )
        cases = []
        try:
            if report.stat().st_size <= 1024 * 1024:
                cases = list(ET.fromstring(report.read_bytes()).iter("testcase"))
        except (OSError, ET.ParseError):
            pass
        witnessed = (
            bool(expected) and sorted(case.get("name") for case in cases) == expected
        )
        ran = completed.returncode in {0, 1} and witnessed
        return {
            "ran": ran,
            "passed": ran
            and completed.returncode == 0
            and all(
                not any(
                    case.find(tag) is not None
                    for tag in ("failure", "error", "skipped")
                )
                for case in cases
            ),
            "returncode": completed.returncode,
            "reason": None if ran else "acceptance runner failed or tests missing",
        }


def execution_order(
    tasks: Sequence[Task], repeat: int, paired: bool
) -> list[tuple[int, Task, str | None]]:
    order: list[tuple[int, Task, str | None]] = []
    for iteration in range(1, repeat + 1):
        arms: Sequence[str | None] = (
            (("off", "on") if iteration % 2 else ("on", "off")) if paired else (None,)
        )
        order.extend((iteration, task, arm) for task in tasks for arm in arms)
    return order


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
        if state in _SETTLED:
            return state, failure, stranded
        if time.time() >= deadline:
            # A detached chat must not leave a provider task running while the
            # benchmark admits another arm. The caller stops the run too.
            cancel()
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
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(task_events)")
        }
        timestamp = "created_at" if "created_at" in columns else "NULL"
        rows = connection.execute(
            f"SELECT event_type, payload_json, {timestamp} FROM task_events "
            "WHERE task_id = ? ORDER BY sequence",
            (task_id,),
        )
        result = []
        for kind, payload, created_at in rows:
            value = json.loads(payload)
            if type(created_at) is int:
                value["_benchmark_created_at"] = created_at
            result.append((str(kind), value))
        return result
    finally:
        connection.close()


def durable_usage(database: Path, task_id: str) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT c.checkpoint_json FROM execution_checkpoints c "
            "JOIN task_executions e ON e.execution_id = c.execution_id "
            "WHERE e.task_id = ? ORDER BY e.task_attempt DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        checkpoint = json.loads(rows[0]) if rows else {}
        usage = checkpoint.get("usage_total", {})
        return {
            key: value
            for key, value in usage.items()
            if isinstance(key, str) and type(value) is int and value >= 0
        }
    finally:
        connection.close()


def run(arguments: argparse.Namespace) -> int:
    profile = arguments.profile
    if profile == "default":
        raise SystemExit("use a separate, signed-in profile, not the default one")
    suite = load_suite(arguments.tasks)
    repeat = getattr(arguments, "repeat", 1)
    review_paired = getattr(arguments, "review_paired", False)
    retrieval_paired = getattr(arguments, "retrieval_paired", False)
    if review_paired and retrieval_paired:
        raise SystemExit("review and retrieval comparisons must run separately")
    paired = review_paired or retrieval_paired
    tasks = [t for t in suite.tasks if not arguments.only or t.id in arguments.only]
    if review_paired and not arguments.only:
        tasks = [t for t in tasks if t.review_kind in {"bug", "clean"}]
    if retrieval_paired and not arguments.only:
        tasks = [t for t in tasks if t.id in RETRIEVAL_TASKS]
    if not tasks:
        raise SystemExit("no task matches --only")
    if repeat < 1 or arguments.budget_tokens < 1:
        raise SystemExit("repeat and token budget must be positive")
    if review_paired and any(task.review_kind is None for task in tasks):
        raise SystemExit("--review-paired requires only review corpus tasks")
    natural = any(task.review_kind == "natural" for task in tasks)
    if natural and (
        not review_paired or any(task.review_kind != "natural" for task in tasks)
    ):
        raise SystemExit("natural review tasks require a separate paired comparison")
    if any(task.acceptance for task in tasks) and available_sandbox() is None:
        raise SystemExit("hidden acceptance requires a working sandbox")
    paths = AppPaths.resolve(profile)
    loupe = [*_loupe(), "--profile", profile]
    output = arguments.out or (
        paths.state_dir / "benchmark" / f"results-{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    expected = len(tasks) * repeat * (2 if paired else 1)

    def save(results: list[dict[str, Any]], spent: int, status: str) -> None:
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
            "run_kind": "live",
            "repeat": repeat,
            "review_paired": review_paired,
            "retrieval_paired": retrieval_paired,
            "budget_tokens": arguments.budget_tokens,
            "spent_tokens": spent,
            "budget_respected": spent <= arguments.budget_tokens,
            "expected_runs": expected,
            "complete": len(results) == expected,
            "status": status,
        }
        document["retrieval_summary"] = retrieval_summary(document)
        document["review_summary"] = review_summary(document)
        if natural:
            document["matched_pairs"] = natural_matched_pairs(results)
            document["expected_pairs"] = len(tasks) * repeat
        document["measurement_complete"] = (
            document["complete"]
            and all(
                arm["invalid_or_incomplete_runs"] == 0
                for arm in document["review_summary"].values()
            )
            and (natural or all(result["passed"] for result in results))
            and (not natural or document["matched_pairs"] == document["expected_pairs"])
        )
        if natural and status == "finished" and not document["measurement_complete"]:
            document["status"] = "incomplete"
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_name(target.name + ".pending")
        pending.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        os.replace(pending, target)

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
            for repetition, task, review_arm in execution_order(tasks, repeat, paired):
                wait_between_tasks(getattr(arguments, "pause_file", None))
                # Keep a conservative measured per-task reserve. Provider usage
                # is known after requests, so this is admission control rather
                # than a false claim of a provider-side hard billing limit.
                reserve = max(
                    100_000,
                    2
                    * max(
                        (r["prompt_tokens"] + r["output_tokens"] for r in results),
                        default=0,
                    ),
                )
                if spent >= arguments.budget_tokens or (
                    paired and spent and arguments.budget_tokens - spent < reserve
                ):
                    print(f"Stopping before {task.id}: the token budget is spent.")
                    break
                print(
                    f"Running {task.id} #{repetition} {review_arm or ''}…", flush=True
                )
                if review_arm is not None:
                    subprocess.run(
                        [*loupe, "daemon", "stop"],
                        env=environment,
                        capture_output=True,
                        check=False,
                    )
                    arm_settings = (
                        [
                            "knowledge.index="
                            + ("lexical" if review_arm == "on" else "off")
                        ]
                        if retrieval_paired
                        else [f"review={review_arm}"]
                    )
                    write_config(config_home, [*arguments.set, *arm_settings], profile)
                    subprocess.run(
                        [*loupe, "daemon", "start"],
                        env=environment,
                        capture_output=True,
                        check=True,
                    )
                repository = clone
                if task.fixture is not None:
                    run_id = f"{task.id}-{repetition}-{review_arm or 'single'}"
                    repository = _fixture(fixtures / run_id, task.fixture)
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
                build_seconds = 0.0
                if retrieval_paired and review_arm == "on":
                    started_build = time.monotonic()
                    client = DaemonClient(paths, timeout_seconds=90)
                    client.call("repo.add", {"path": str(repository)})
                    built = client.call("index.build", {"path": str(repository)})
                    if built.get("state") != "ready":
                        raise SystemExit("retrieval comparison requires a ready index")
                    build_seconds = time.monotonic() - started_build
                since = int(time.time() * 1000)
                wall, monotonic = time.time(), time.monotonic()
                deadline = wall + arguments.timeout
                log_path = (
                    paths.state_dir
                    / "benchmark"
                    / f"{task.id}-{repetition}-{review_arm or 'single'}.log"
                )
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
                    # Plain chat submits each physical line as a new task.
                    prompt = task.prompt.replace("\r\n", " ").replace("\r", " ")
                    process.stdin.write(prompt.replace("\n", " ") + "\n")
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
                events = _events(paths.control_db, task_id)
                metrics = summarize_events(events)
                for key, value in durable_usage(paths.control_db, task_id).items():
                    metrics.usage[key] = max(metrics.usage.get(key, 0), value)
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
                result["repetition"] = repetition
                result["review_arm"] = review_arm or next(
                    (
                        item.partition("=")[2]
                        for item in reversed(arguments.set)
                        if item.startswith("review=")
                    ),
                    "off",
                )
                if retrieval_paired:
                    result["retrieval_arm"] = review_arm
                    result["review_arm"] = next(
                        (
                            item.partition("=")[2]
                            for item in reversed(arguments.set)
                            if item.startswith("review=")
                        ),
                        "off",
                    )
                    result["index_build_seconds"] = round(build_seconds, 3)
                if task.review_kind is not None:
                    result["review_kind"] = task.review_kind
                    if task.review_kind != "natural":
                        result["seed_observed"] = seed_observed(task, events)
                    result["hidden_acceptance"] = hidden_acceptance(
                        task,
                        repository,
                        (
                            *protected_home_paths(Path.home()),
                            paths.data_dir,
                            paths.state_dir,
                            paths.config_dir,
                        ),
                    )
                    if task.review_kind == "natural":
                        result["natural_obvious"] = task.natural_obvious
                        result["natural_provenance"] = json.loads(
                            (
                                Path(__file__).with_name("benchmark_acceptance")
                                / str(task.acceptance)
                                / "provenance.json"
                            ).read_text()
                        )
                        result["initial_acceptance"] = natural_first_acceptance(
                            task,
                            events,
                            (
                                *protected_home_paths(Path.home()),
                                paths.data_dir,
                                paths.state_dir,
                                paths.config_dir,
                            ),
                        )
                # Left waiting on a question after its chat had gone.
                result["stranded_question"] = stranded
                result["runner_timed_out"] = state not in _SETTLED
                results.append(result)
                spent += result["prompt_tokens"] + result["output_tokens"]
                print(_line(result), flush=True)
                save(results, spent, "running")
                if result["runner_timed_out"] or failure == "PROVIDER_UNAVAILABLE":
                    print(
                        "Stopping after task timeout or provider unavailability.",
                        flush=True,
                    )
                    break
        finally:
            subprocess.run([*loupe, "daemon", "stop"], capture_output=True, check=False)
    save(results, spent, "finished")
    print(f"Results: {output}")
    return (
        0
        if results
        and len(results) == expected
        and spent <= arguments.budget_tokens
        and all(result["passed"] for result in results)
        else 1
    )


def wait_between_tasks(pause_file: Path | None) -> None:
    """Let offline measurements pause admission without abandoning a live cell."""
    announced = False
    while pause_file is not None and pause_file.exists():
        if not announced:
            print("Paused between completed benchmark cells.", flush=True)
            announced = True
        time.sleep(0.25)


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
    parser.add_argument("--repeat", type=int, default=1, help="repetitions per task")
    parser.add_argument(
        "--retrieval-paired",
        action="store_true",
        help="alternate optional project retrieval off/on for discovery tasks",
    )
    parser.add_argument(
        "--review-paired",
        action="store_true",
        help="alternate review off/on for each corpus task and repetition",
    )
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
        "--pause-file",
        type=Path,
        help="pause new tasks while this file exists; active cells finish normally",
    )
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
