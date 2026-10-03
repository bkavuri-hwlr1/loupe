"""The bounded tool surface a model is allowed to drive.

This is the enforcement boundary named in the implementation plan's trust
model: read tools are confined to the managed worktree, and write tools are
confined to the *claim's scopes* inside it.  A model that asks to write outside
its claim gets a correctable error rather than an exception, because a path
discovered outside scope is supposed to cause a replan, not a crashed run.

Enforcement here is cooperative, not a sandbox.  It stops a model that follows
its instructions from wandering; it does not stop arbitrary code, which is why
publication still revalidates the real Git object graph before anything leaves
the worktree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path

from llm_cli.agent.bounded_search import (
    SearchBudget,
    SearchError,
    SearchSnapshot,
    find_matches,
)
from llm_cli.agent.limits import (
    DEFAULT_LIMITS,
    MAX_ANSWER_CHARACTERS,
    MAX_TASK_SUMMARY_CHARACTERS,
    ExecutionLimits,
)
from llm_cli.agent.modes import validate_agent_mode
from llm_cli.agent.source_policy import ensure_safe_content, excluded_paths
from llm_cli.coordination.scopes import (
    ScopeValidationError,
    normalize_changed_path,
    normalize_scope,
    uncovered_paths,
)
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.git.validate import collect_changed_paths

_SKIPPED_DIRECTORIES = frozenset({".git"})
_TRUNCATION_NOTE = "\n\n[... output truncated at {limit} bytes ...]"
MAX_QUESTION_CHARACTERS = 2_000
_COMPLETION_OUTCOMES = {"completed", "blocked", "partial"}
_MAX_QUESTION_OPTIONS = 5
_MAX_OPTION_CHARACTERS = 200
_MAX_INSPECTION_FILE_BYTES = 8 * 1024 * 1024


class TaskCancelled(RuntimeError):
    """An explicit stop reached a safe execution boundary."""


class ToolBudgetExhausted(RuntimeError):
    """The execution consumed its hard tool-call budget."""


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """One tool result, shaped for handing straight back to the model."""

    content: str
    is_error: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ToolUsage:
    """Running totals a driver reports and a policy layer can inspect."""

    calls: int = 0
    files_read: int = 0
    bytes_read: int = 0
    writes: int = 0
    denied: int = 0
    finished: bool = False
    summary: str = ""
    answer: str = ""
    outcome: str = "completed"


@dataclass(slots=True)
class ToolBroker:
    """Execute model tool calls against one claimed worktree.

    ``asker`` is supplied only by an interactive run or session; without it the
    ``ask_user`` tool is not offered, so a background run can never block
    forever on a question nobody is there to answer.
    """

    worktree: Path
    scopes: tuple[str, ...]
    case_insensitive_filesystem: bool = False
    base_oid: str | None = None
    limits: ExecutionLimits = DEFAULT_LIMITS
    asker: Callable[[str], str] | None = None
    on_event: Callable[[str, dict[str, object]], None] | None = None
    cancelled: Callable[[], bool] | None = None
    check_runner: Callable[[str], ToolOutcome] | None = None
    finish_gate: Callable[[], ToolOutcome | None] | None = None
    usage: ToolUsage = field(default_factory=ToolUsage)
    agent_mode: str = "auto"
    _partial_reads: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        validate_agent_mode(self.agent_mode)

    def check_cancelled(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise TaskCancelled("task stopped; pending edits retained")

    def emit(self, event_type: str, payload: dict[str, object]) -> None:
        """Publish one durable lifecycle event, if anyone is listening.

        These task-local events include the operator's full model/tool
        transcript. Text and arguments are untrusted display data. They must
        remain separate from metadata-only checkout coordination and must not
        be injected into another session's model context or operational logs.
        """

        if self.on_event is not None:
            self.on_event(event_type, payload)

    def usage_snapshot(self) -> dict[str, object]:
        """Return JSON-safe counters so a resumed turn keeps every limit."""

        return {
            "calls": self.usage.calls,
            "files_read": self.usage.files_read,
            "bytes_read": self.usage.bytes_read,
            "writes": self.usage.writes,
            "denied": self.usage.denied,
            "finished": self.usage.finished,
            "summary": self.usage.summary,
            "answer": self.usage.answer,
            "outcome": self.usage.outcome,
            "partial_reads": sorted(self._partial_reads),
        }

    def restore_usage(self, saved: Mapping[str, object]) -> None:
        """Restore a checkpointed budget without accepting malformed counters."""

        integer_fields = ("calls", "files_read", "bytes_read", "writes", "denied")
        values: dict[str, int] = {}
        for field_name in integer_fields:
            value = saved.get(field_name, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("saved tool usage contains an invalid counter")
            values[field_name] = value
        finished = saved.get("finished", False)
        summary = saved.get("summary", "")
        answer = saved.get("answer", "")
        outcome = saved.get("outcome", "completed")
        if (
            not isinstance(finished, bool)
            or not isinstance(summary, str)
            or not isinstance(answer, str)
            or len(answer) > MAX_ANSWER_CHARACTERS
            or not isinstance(outcome, str)
            or outcome not in _COMPLETION_OUTCOMES
        ):
            raise ValueError("saved tool usage contains an invalid completion state")
        if values["calls"] > self.limits.max_tool_calls:
            raise ValueError("saved tool usage exceeds the configured call budget")
        if self.agent_mode == "plan" and values["writes"]:
            raise ValueError("plan mode cannot restore a checkpoint containing edits")
        partial_reads = saved.get("partial_reads", [])
        if (
            not isinstance(partial_reads, list)
            or len(partial_reads) > self.limits.max_read_files
            or any(
                not isinstance(path, str) or normalize_changed_path(path) != path
                for path in partial_reads
            )
        ):
            raise ValueError("saved partial file reads are malformed")
        self._partial_reads = set(partial_reads)
        self.usage = ToolUsage(
            calls=values["calls"],
            files_read=values["files_read"],
            bytes_read=values["bytes_read"],
            writes=values["writes"],
            denied=values["denied"],
            finished=finished,
            summary=summary[:MAX_TASK_SUMMARY_CHARACTERS],
            answer=answer,
            outcome=outcome,
        )

    def record_interrupted_call(self) -> None:
        """Count a tool call whose filesystem outcome cannot be replayed safely."""

        if self.usage.calls >= self.limits.max_tool_calls:
            raise ToolBudgetExhausted(
                f"execution exceeded its {self.limits.max_tool_calls} tool-call budget"
            )
        self.usage.calls += 1

    def tool_names(self) -> tuple[str, ...]:
        names = [
            "list_files",
            "read_file",
            "search_text",
            "write_file",
            "apply_patch",
            "read_diff",
            "validate_changes",
            "finish_task",
        ]
        if self.check_runner is not None:
            names.append("run_check")
        names.extend(("create_directory", "delete_file", "rename_file"))
        if self.asker is not None:
            names.append("ask_user")
        if self.agent_mode == "plan":
            allowed = {
                "list_files",
                "read_file",
                "search_text",
                "read_diff",
                "validate_changes",
                "finish_task",
                "ask_user",
            }
            names = [name for name in names if name in allowed]
        return tuple(names)

    def invoke(self, name: str, arguments: Mapping[str, object]) -> ToolOutcome:
        """Run one tool call, counting it against the execution budget."""

        self.check_cancelled()
        if self.usage.finished:
            return _error("the task is already finished; make no further tool calls")
        if self.usage.calls >= self.limits.max_tool_calls:
            raise ToolBudgetExhausted(
                f"execution exceeded its {self.limits.max_tool_calls} tool-call budget"
            )
        self.usage.calls += 1
        self.emit("tool.called", {"tool": name, "call": self.usage.calls})
        if self.agent_mode == "plan" and name not in self.tool_names():
            self.usage.denied += 1
            return _error(
                "Plan mode is read-only; editing and running checks are unavailable. "
                "Inspect the source and finish with a plan."
            )
        handler = _HANDLERS.get(name)
        if handler is None or name not in self.tool_names():
            return _error(f"unknown tool {name!r}")
        try:
            # The allowlist decides which tool is exposed; dispatch through the
            # instance so alternate workspace backends can implement it safely.
            bound_handler: Callable[[Mapping[str, object]], ToolOutcome] = getattr(
                self, handler.__name__
            )
            return bound_handler(arguments)
        except ScopeValidationError as exc:
            self.usage.denied += 1
            return _error(f"that path is not usable: {exc}")
        except LlmCoordError as exc:
            return _error(f"the repository refused that operation: {exc.message}")
        except OSError as exc:
            return _error(
                f"the filesystem refused that operation: {exc.strerror or exc}"
            )

    # -- read tools ------------------------------------------------------

    def _list_files(self, arguments: Mapping[str, object]) -> ToolOutcome:
        raw = _string(arguments, "path", default=".")
        directory = self._directory(raw)
        entries: list[str] = []
        children, capped = _bounded_children(directory, self.limits.max_scanned_files)
        excluded = excluded_paths(
            self.worktree,
            [child.relative_to(self.worktree).as_posix() for child in children],
        )
        for child in children:
            if child.name in _SKIPPED_DIRECTORIES:
                continue
            relative = child.relative_to(self.worktree).as_posix()
            if relative in excluded or child.is_symlink():
                continue
            entries.append(f"{relative}/" if child.is_dir() else relative)
        return _listing_response(
            entries,
            arguments,
            self.limits,
            stop_reason="scanned_file_limit; narrow path" if capped else None,
        )

    def _read_file(self, arguments: Mapping[str, object]) -> ToolOutcome:
        relative = normalize_changed_path(_string(arguments, "path"))
        _read_range(arguments)
        if self.usage.files_read >= self.limits.max_read_files:
            return _error(
                f"this execution already read its limit of "
                f"{self.limits.max_read_files} files"
            )
        target = self._existing_file(relative)
        size = target.stat().st_size
        range_request = any(
            key in arguments for key in ("start_line", "end_line", "start_column")
        )
        if (
            not range_request
            and self.usage.bytes_read + size > self.limits.max_read_bytes
        ):
            return _error("this execution reached its total read-size limit")
        if range_request and size > _MAX_INSPECTION_FILE_BYTES:
            return _error("that file exceeds the 8 MiB bounded range-scan limit")
        try:
            if range_request:
                with target.open("rb") as stream:
                    raw_content = stream.read(_MAX_INSPECTION_FILE_BYTES + 1)
                if len(raw_content) > _MAX_INSPECTION_FILE_BYTES:
                    return _error(
                        "that file exceeds the 8 MiB bounded range-scan limit"
                    )
                text = raw_content.decode("utf-8")
            else:
                text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _error(f"{relative} is not UTF-8 text")
        ensure_safe_content(text)
        self.usage.files_read += 1
        result = _read_response(text, arguments, self.limits)
        returned_bytes = result.metadata.get("bytes_returned", 0)
        assert isinstance(returned_bytes, int)
        charge = returned_bytes if range_request else size
        if self.usage.bytes_read + charge > self.limits.max_read_bytes:
            self._partial_reads.add(relative)
            return _error(
                "this execution reached its total read-size limit; request fewer lines"
            )
        self.usage.bytes_read += charge
        if not result.is_error and result.metadata.get("complete_file", False):
            self._partial_reads.discard(relative)
        else:
            self._partial_reads.add(relative)
        return result

    def _search_text(self, arguments: Mapping[str, object]) -> ToolOutcome:
        budget = SearchBudget(self.check_cancelled)
        try:
            snapshot = self._search_snapshot(arguments, budget)
            return _search_response(snapshot, arguments, self.limits, budget)
        except SearchError as exc:
            return _error(str(exc))

    def _search_snapshot(
        self, arguments: Mapping[str, object], budget: SearchBudget
    ) -> SearchSnapshot:
        _page_options(arguments, self.limits)
        pattern = _string(arguments, "pattern")
        raw = _string(arguments, "path", default=".")
        target = (
            self.worktree
            if raw.strip() in {"", ".", "./", "*"}
            else self._safe_path(
                normalize_scope(raw).rstrip("/"), remaining=budget.remaining
            )
        )
        entries = (
            [(str(target.parent), [], [target.name])]
            if target.is_file()
            else os.walk(self._directory(raw, remaining=budget.remaining))
        )
        files: list[tuple[str, str]] = []
        scanned = 0
        scanned_bytes = 0
        fingerprint = hashlib.sha256((pattern + "\0" + raw).encode("utf-8"))

        def finish(*, stop_reason: str | None = None) -> SearchSnapshot:
            return SearchSnapshot(files, fingerprint.hexdigest(), stop_reason)

        # os.walk rather than rglob: pruning subdirectories in place stops the
        # walk from descending into .git at all, and streaming avoids building
        # a list of every path in the repository before the limit applies.
        for directory, subdirectories, names in entries:
            budget.remaining()
            parent = Path(directory).relative_to(self.worktree)
            candidates = [
                (parent / name).as_posix()
                for name in [*subdirectories, *names]
                if name not in _SKIPPED_DIRECTORIES
            ]
            excluded = excluded_paths(
                self.worktree, candidates, remaining=budget.remaining
            )
            subdirectories[:] = sorted(
                name
                for name in subdirectories
                if name not in _SKIPPED_DIRECTORIES
                and (parent / name).as_posix() not in excluded
            )
            for name in sorted(names):
                budget.remaining()
                if (parent / name).as_posix() in excluded:
                    continue
                if scanned >= self.limits.max_scanned_files:
                    return finish(stop_reason="scanned_file_limit; narrow path")
                candidate = Path(directory) / name
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                scanned += 1
                try:
                    size = candidate.stat().st_size
                    if scanned_bytes + size > self.limits.max_read_bytes:
                        return finish(stop_reason="read_size_limit; narrow path")
                    with candidate.open("rb") as stream:
                        raw_content = stream.read(
                            self.limits.max_read_bytes - scanned_bytes + 1
                        )
                    scanned_bytes += len(raw_content)
                    if scanned_bytes > self.limits.max_read_bytes:
                        return finish(stop_reason="read_size_limit; narrow path")
                    content = raw_content.decode("utf-8")
                    ensure_safe_content(content)
                except (UnicodeDecodeError, OSError):
                    continue
                relative = candidate.relative_to(self.worktree).as_posix()
                fingerprint.update(
                    json.dumps(
                        [relative, hashlib.sha256(raw_content).hexdigest()]
                    ).encode("utf-8")
                )
                files.append((relative, content))
        return finish()

    def _read_diff(self, arguments: Mapping[str, object]) -> ToolOutcome:
        del arguments
        changed = collect_changed_paths(self.worktree)
        excluded = excluded_paths(self.worktree, list(changed))
        changed = tuple(path for path in changed if path not in excluded)
        if not changed:
            return _ok("(no changes yet)", self.limits)
        result = run_git(
            self.worktree,
            [
                "diff",
                "--no-color",
                "HEAD",
                "--",
                *(f":(literal){path}" for path in changed),
            ],
            check=False,
        )
        tracked = result.stdout if isinstance(result.stdout, str) else ""
        ensure_safe_content(tracked)
        listing = "\n".join(f"  {path}" for path in changed)
        return _ok(f"Changed paths:\n{listing}\n\n{tracked}".strip(), self.limits)

    # -- write tools -----------------------------------------------------

    def _write_file(self, arguments: Mapping[str, object]) -> ToolOutcome:
        relative = normalize_changed_path(_string(arguments, "path"))
        if relative in self._partial_reads:
            return _error(
                "only part of this file was shown; use apply_patch to preserve "
                "unseen content, or read the complete file before replacing it"
            )
        content = _string(arguments, "content")
        denial = self._write_denial(relative)
        if denial is not None:
            return denial
        encoded = content.encode("utf-8")
        if len(encoded) > self.limits.max_write_bytes:
            return _error("that content exceeds the per-write size limit")
        ensure_safe_content(content)
        target = self._write_target(relative, must_exist=False)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.usage.writes += 1
        return _ok(f"wrote {relative} ({len(encoded)} bytes)", self.limits)

    def _apply_patch(self, arguments: Mapping[str, object]) -> ToolOutcome:
        relative = normalize_changed_path(_string(arguments, "path"))
        old_text = _string(arguments, "old_text")
        new_text = _string(arguments, "new_text")
        if not old_text:
            return _error("old_text must not be empty; use write_file to create a file")
        denial = self._write_denial(relative)
        if denial is not None:
            return denial
        target = self._write_target(relative, must_exist=True)
        try:
            current = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _error(f"{relative} is not UTF-8 text")
        ensure_safe_content(current)
        occurrences = current.count(old_text)
        if occurrences == 0:
            return _error(f"old_text does not appear in {relative}")
        if occurrences > 1:
            return _error(
                f"old_text appears {occurrences} times in {relative}; "
                "include enough surrounding context to make it unique"
            )
        replacement = current.replace(old_text, new_text, 1)
        ensure_safe_content(replacement)
        target.write_text(replacement, encoding="utf-8")
        self.usage.writes += 1
        return _ok(f"edited {relative}", self.limits)

    def _run_check(self, arguments: Mapping[str, object]) -> ToolOutcome:
        if self.check_runner is None:
            return _error("no checks configured")
        result = self.check_runner(_string(arguments, "name"))
        return ToolOutcome(_ok(result.content, self.limits).content, result.is_error)

    def _create_directory(self, arguments: Mapping[str, object]) -> ToolOutcome:
        path = normalize_changed_path(_string(arguments, "path"))
        denied = self._write_denial(path)
        if denied:
            return denied
        target = self._write_target(path, must_exist=False)
        target.mkdir(parents=True, exist_ok=True)
        self.usage.writes += 1
        return _ok(f"created {path}", self.limits)

    def _delete_file(self, arguments: Mapping[str, object]) -> ToolOutcome:
        path = normalize_changed_path(_string(arguments, "path"))
        denied = self._write_denial(path)
        if denied:
            return denied
        target = self._write_target(path, must_exist=True)
        ensure_safe_content(target.read_text(encoding="utf-8"))
        target.unlink()
        self.usage.writes += 1
        return _ok(f"deleted {path}", self.limits)

    def _rename_file(self, arguments: Mapping[str, object]) -> ToolOutcome:
        source = normalize_changed_path(_string(arguments, "source"))
        destination = normalize_changed_path(_string(arguments, "destination"))
        if source.casefold() == destination.casefold():
            return _error("same-path and case-only renames are unavailable")
        for path in (source, destination):
            denied = self._write_denial(path)
            if denied:
                return denied
        origin = self._write_target(source, must_exist=True)
        target = self._write_target(destination, must_exist=False)
        if target.exists():
            return _error("rename destination must be absent")
        ensure_safe_content(origin.read_text(encoding="utf-8"))
        target.parent.mkdir(parents=True, exist_ok=True)
        origin.rename(target)
        self.usage.writes += 1
        return _ok(f"renamed {source} -> {destination}", self.limits)

    # -- lifecycle tools -------------------------------------------------

    def _validate_changes(self, arguments: Mapping[str, object]) -> ToolOutcome:
        del arguments
        changed = collect_changed_paths(self.worktree)
        if not changed:
            return _ok("no changes yet; nothing to validate", self.limits)
        try:
            outside = uncovered_paths(
                self.scopes,
                changed,
                case_insensitive_filesystem=self.case_insensitive_filesystem,
            )
        except ScopeValidationError as exc:
            return _error(str(exc))
        if outside:
            return _error(
                "these changed paths are outside the claim and would be "
                f"refused at publication: {', '.join(outside)}"
            )
        listing = "\n".join(f"  {path}" for path in changed)
        return _ok(
            f"all {len(changed)} changed path(s) are in scope:\n{listing}", self.limits
        )

    def complete(
        self, *, answer: str, summary: str = "", outcome: str = "completed"
    ) -> ToolOutcome:
        """Apply the same completion gate to natural answers and finish tools."""

        self.check_cancelled()
        if outcome not in _COMPLETION_OUTCOMES:
            return _error("outcome must be completed, blocked, or partial")
        if not answer.strip():
            return _error(
                "provide the complete user-facing answer in 'answer' before "
                "finishing; 'summary' is only an optional operational report. "
                "Include the actual findings, explanation, or requested deliverable."
            )
        if len(answer) > MAX_ANSWER_CHARACTERS:
            return _error(
                f"answer exceeds the {MAX_ANSWER_CHARACTERS}-character limit; "
                "provide a shorter complete answer"
            )
        if (
            outcome == "completed"
            and self.finish_gate is not None
            and self.agent_mode != "plan"
        ):
            refused = self.finish_gate()
            if refused is not None:
                return ToolOutcome(_ok(refused.content, self.limits).content, True)
        self.usage.finished = True
        self.usage.summary = summary[:MAX_TASK_SUMMARY_CHARACTERS]
        self.usage.answer = answer
        self.usage.outcome = outcome
        return _ok("task marked finished", self.limits)

    def _finish_task(self, arguments: Mapping[str, object]) -> ToolOutcome:
        # Saved completions are restored through restore_usage; new calls must
        # supply an answer even when an older conversation used summary alone.
        answer = _string(arguments, "answer", default="")
        summary = _string(arguments, "summary", default="")
        outcome = _string(arguments, "outcome", default="completed")
        return self.complete(answer=answer, summary=summary, outcome=outcome)

    def _ask_user(self, arguments: Mapping[str, object]) -> ToolOutcome:
        if self.asker is None:
            return _error("no interactive session is attached to answer questions")
        question = _string(arguments, "question").strip()
        if not question:
            return _error("question must not be blank")
        options: list[str] = []
        if "options" in arguments:
            raw_options = arguments["options"]
            if not isinstance(raw_options, list) or not (
                2 <= len(raw_options) <= _MAX_QUESTION_OPTIONS
            ):
                return _error("options must be a list of 2 to 5 suggested answers")
            for option in raw_options:
                if (
                    not isinstance(option, str)
                    or not option.strip()
                    or len(option) > _MAX_OPTION_CHARACTERS
                    or "\n" in option
                    or "\r" in option
                ):
                    return _error(
                        "each option must be a nonblank single line of at most "
                        f"{_MAX_OPTION_CHARACTERS} characters"
                    )
                options.append(option.strip())
            if len(set(options)) != len(options):
                return _error("suggested answers must be distinct")
            question += "\n\n" + "\n".join(
                f"{index}. {option}" for index, option in enumerate(options, 1)
            )
            question += "\n\nReply with a number or your own answer."
        if len(question) > MAX_QUESTION_CHARACTERS:
            return _error(
                "question and formatted options must fit within "
                f"{MAX_QUESTION_CHARACTERS} characters; ask a shorter question"
            )
        answer = self.asker(question)
        # Only exact choice numbers are shortcuts. Preserve all other input as
        # free text so a user's custom answer is never silently discarded.
        selections = {str(index): option for index, option in enumerate(options, 1)}
        return _ok(selections.get(answer.strip(), answer), self.limits)

    # -- path safety -----------------------------------------------------

    def _write_denial(self, relative: str) -> ToolOutcome | None:
        """Refuse a write the claim does not cover, without ending the run."""

        try:
            outside = uncovered_paths(
                self.scopes,
                (relative,),
                case_insensitive_filesystem=self.case_insensitive_filesystem,
            )
        except ScopeValidationError as exc:
            self.usage.denied += 1
            return _error(str(exc))
        if outside:
            self.usage.denied += 1
            return _error(
                f"{relative} is outside this task's claimed scopes "
                f"({', '.join(self.scopes)}); request a wider claim rather than "
                "writing somewhere else"
            )
        return None

    def _directory(
        self, raw: str, *, remaining: Callable[[], float] | None = None
    ) -> Path:
        # A model naturally writes "." for the root; the scope grammar has no
        # dot components, so it is translated before validation rather than
        # loosening what a scope may contain.
        if raw.strip() in {"", ".", "./", "*"}:
            return self.worktree
        scope = normalize_scope(raw)
        if scope == "*":
            return self.worktree
        relative = scope.rstrip("/")
        target = self._safe_path(relative, remaining=remaining)
        if not target.is_dir():
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                f"{_display_path(relative)} is not a directory. "
                "Use list_files to check the exact path.",
            )
        return target

    def _existing_file(self, relative: str) -> Path:
        target = self._safe_path(relative)
        if not target.is_file():
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                f"{_display_path(relative)} is not a readable file. "
                "Use list_files to check the exact path.",
            )
        return target

    def _write_target(self, relative: str, *, must_exist: bool) -> Path:
        """Resolve a path a write is about to land on, refusing every alias.

        Both write tools come through here.  Checking only that the final
        resolved path stays inside the worktree is not enough: a symlink inside
        the claim that points at another directory inside the worktree resolves
        to a legitimate-looking path, and the scope check ran against the
        spelling, not the destination.  Refusing symlinked components closes
        that, so the path a scope was checked against is the path written.
        """

        components = relative.split("/")
        current = self.worktree
        for component in components[:-1]:
            current = current / component
            if current.is_symlink():
                raise LlmCoordError(
                    ErrorCode.REPOSITORY_UNSAFE,
                    "a write would traverse a repository symlink",
                )
        target = current / components[-1]
        if target.is_symlink() or target.is_dir():
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                "the write target is not a regular repository file",
            )
        if must_exist and not target.is_file():
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE, f"{relative} is not an editable file"
            )
        # Mutations must honor the same source exclusions as reads: renaming
        # a private file to an ordinary name must not make its bytes readable.
        self._safe_path(relative)
        return target

    def _safe_path(
        self, relative: str, *, remaining: Callable[[], float] | None = None
    ) -> Path:
        """Resolve a worktree-relative path for reading.

        ``normalize_changed_path`` has already rejected traversal and absolute
        spellings; this catches the case those cannot see, where a component
        inside the worktree is a link pointing back out of it.  Reads need only
        this containment check, because reading anywhere in the worktree is
        allowed -- it is writing that the claim constrains.
        """

        candidate = (self.worktree / relative).absolute()
        self._assert_within_worktree(relative, candidate)
        resolved_relative = (
            candidate.resolve(strict=False)
            .relative_to(self.worktree.resolve(strict=False))
            .as_posix()
        )
        if excluded_paths(
            self.worktree, [relative, resolved_relative], remaining=remaining
        ):
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE, "excluded files are not model source"
            )
        return candidate

    def _assert_within_worktree(self, relative: str, candidate: Path) -> None:
        resolved = candidate.resolve(strict=False)
        root = self.worktree.resolve(strict=False)
        if resolved != root and not resolved.is_relative_to(root):
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                f"{relative} resolves outside the managed worktree",
            )


def _integer(
    arguments: Mapping[str, object], key: str, *, default: int, minimum: int = 0
) -> int:
    value = arguments.get(key, default)
    if type(value) is not int or not minimum <= value <= 1_000_000_000:
        raise ScopeValidationError(
            f"{key} must be an integer between {minimum} and 1000000000"
        )
    return value


def _read_range(arguments: Mapping[str, object]) -> tuple[int, int | None, int]:
    if "refresh" in arguments and not isinstance(arguments["refresh"], bool):
        raise ScopeValidationError("refresh must be a boolean")
    start = _integer(arguments, "start_line", default=1, minimum=1)
    end = (
        _integer(arguments, "end_line", default=start, minimum=1)
        if "end_line" in arguments
        else None
    )
    column = _integer(arguments, "start_column", default=1, minimum=1)
    if end is not None and end < start:
        raise ScopeValidationError("end_line must be at least start_line")
    return start, end, column


def _metadata_content(body: str, metadata: Mapping[str, object]) -> str:
    label = "output truncated; " if metadata.get("truncated") else ""
    encoded = json.dumps(dict(metadata), ensure_ascii=True, separators=(",", ":"))
    return f"{body}\n[{label}metadata={encoded}]"


def _read_response(
    text: str, arguments: Mapping[str, object], limits: ExecutionLimits
) -> ToolOutcome:
    """Preserve default full reads; make bounded windows explicitly continuable."""

    start, requested_end, column = _read_range(arguments)
    lines = text.splitlines(keepends=True)
    total = len(lines)
    if start > max(1, total):
        return _error(f"start_line exceeds this file's {total} lines")
    if lines and column > len(lines[start - 1]) + 1:
        return _error("start_column exceeds the selected line")
    numbered = any(
        key in arguments for key in ("start_line", "end_line", "start_column")
    )
    end = min(requested_end or total, total)
    if not numbered and len(text.encode("utf-8")) <= limits.max_tool_output_bytes:
        return ToolOutcome(
            text,
            metadata={
                "complete_file": True,
                "truncated": False,
                "bytes_returned": len(text.encode("utf-8")),
            },
        )
    budget = limits.max_tool_output_bytes
    while budget > 0:
        pieces: list[str] = []
        remaining = budget
        next_line: int | None = end + 1 if end < total else None
        next_column: int | None = 1 if next_line is not None else None
        visible_end = start - 1
        clipped = False
        returned_bytes = 0
        for number in range(start, end + 1):
            offset = column - 1 if number == start else 0
            source = lines[number - 1][offset:]
            prefix = f"{number}: " if numbered else ""
            rendered = (prefix + source).encode("utf-8")
            if len(rendered) <= remaining:
                pieces.append(prefix + source)
                remaining -= len(rendered)
                returned_bytes += len(source.encode("utf-8"))
                visible_end = number
                continue
            part = rendered[:remaining].decode("utf-8", errors="ignore")
            if len(part) > len(prefix):
                pieces.append(part)
                visible_end = number
                next_column = offset + len(part) - len(prefix) + 1
                returned_bytes += len(part[len(prefix) :].encode("utf-8"))
            else:
                next_column = offset + 1
            next_line = number
            clipped = True
            break
        complete = start == 1 and column == 1 and end == total and not clipped
        metadata: dict[str, object] = {
            "complete_file": complete,
            "start_line": start,
            "end_line": visible_end,
            "total_lines": total,
            "truncated": not complete,
            "next_start_line": next_line,
            "next_start_column": next_column,
            "bytes_returned": returned_bytes,
        }
        body = "".join(pieces)
        response = _metadata_content(body, metadata)
        excess = len(response.encode("utf-8")) - limits.max_tool_output_bytes
        if excess <= 0:
            if clipped and not body:
                return _error(
                    "the tool output budget cannot fit a line "
                    "and its continuation metadata"
                )
            return ToolOutcome(response, metadata=metadata)
        budget -= excess
    return _error("the tool output budget cannot fit the read continuation metadata")


def _page_options(
    arguments: Mapping[str, object], limits: ExecutionLimits
) -> tuple[int, int]:
    offset = _integer(arguments, "offset", default=0)
    snapshot = arguments.get("snapshot")
    if offset and snapshot is None:
        raise ScopeValidationError("continuing a page requires its snapshot token")
    if snapshot is not None and (
        not isinstance(snapshot, str) or re.fullmatch(r"[a-f0-9]{64}", snapshot) is None
    ):
        raise ScopeValidationError(
            "snapshot must be the token returned by the first page"
        )
    limit = min(
        _integer(arguments, "limit", default=limits.max_search_results, minimum=1),
        limits.max_search_results,
    )
    return offset, limit


def _search_response(
    snapshot: SearchSnapshot,
    arguments: Mapping[str, object],
    limits: ExecutionLimits,
    budget: SearchBudget,
) -> ToolOutcome:
    offset, limit = _page_options(arguments, limits)
    matches = find_matches(
        _string(arguments, "pattern"),
        snapshot.files,
        offset=offset,
        limit=limit,
        budget=budget,
    )
    return _page_response(
        [_search_match(*match) for match in matches[:limit]],
        offset=offset,
        more=len(matches) > limit,
        explicit="offset" in arguments or "limit" in arguments,
        empty="(no matches at this offset)" if offset else "(no matches)",
        limits=limits,
        stop_reason=snapshot.stop_reason,
        snapshot=snapshot.fingerprint,
        expected_snapshot=arguments.get("snapshot"),
    )


def _search_match(relative: str, number: int, line: str) -> str:
    stripped = line.strip()
    suffix = " [... line truncated; use read_file ...]" if len(stripped) > 200 else ""
    return f"{relative}:{number}: {stripped[:200]}{suffix}"


def _page_response(
    entries: list[str],
    *,
    offset: int,
    more: bool,
    explicit: bool,
    empty: str,
    limits: ExecutionLimits,
    stop_reason: str | None = None,
    snapshot: str | None = None,
    expected_snapshot: object = None,
) -> ToolOutcome:
    if expected_snapshot is not None and expected_snapshot != snapshot:
        return _error(
            "source or query changed since the previous page; "
            "restart at offset 0 without a snapshot token"
        )
    body = "\n".join(entries) or empty
    if (
        not explicit
        and not more
        and stop_reason is None
        and len(body.encode("utf-8")) <= limits.max_tool_output_bytes
    ):
        return ToolOutcome(body, metadata={"truncated": False, "next_offset": None})
    visible = list(entries)
    while True:
        metadata: dict[str, object] = {
            "offset": offset,
            "returned": len(visible),
            "truncated": more or stop_reason is not None,
            "next_offset": offset + len(visible) if more else None,
        }
        if stop_reason is not None:
            metadata["stop_reason"] = stop_reason
        if snapshot is not None:
            metadata["snapshot"] = snapshot
        response = _metadata_content("\n".join(visible) or empty, metadata)
        if len(response.encode("utf-8")) <= limits.max_tool_output_bytes:
            if entries and not visible:
                return _error(
                    "the tool output budget cannot fit one result "
                    "and its continuation metadata"
                )
            return ToolOutcome(response, metadata=metadata)
        if not visible:
            return _error("the tool output budget cannot fit continuation metadata")
        visible.pop()
        more = True


def _listing_response(
    entries: list[str],
    arguments: Mapping[str, object],
    limits: ExecutionLimits,
    *,
    stop_reason: str | None = None,
) -> ToolOutcome:
    offset, limit = _page_options(arguments, limits)
    snapshot = hashlib.sha256(
        json.dumps([arguments.get("path", "."), entries], ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    return _page_response(
        entries[offset : offset + limit],
        offset=offset,
        more=offset + limit < len(entries),
        explicit="offset" in arguments or "limit" in arguments,
        empty="(no entries at this offset)" if offset else "(empty directory)",
        limits=limits,
        stop_reason=stop_reason,
        snapshot=snapshot,
        expected_snapshot=arguments.get("snapshot"),
    )


def _bounded_children(directory: Path, limit: int) -> tuple[list[Path], bool]:
    children = list(islice(directory.iterdir(), limit + 1))
    return sorted(children[:limit], key=lambda child: child.name), len(children) > limit


def _ok(content: str, limits: ExecutionLimits) -> ToolOutcome:
    encoded = content.encode("utf-8")
    if len(encoded) <= limits.max_tool_output_bytes:
        return ToolOutcome(content=content)
    clipped = encoded[: limits.max_tool_output_bytes].decode("utf-8", "ignore")
    note = _TRUNCATION_NOTE.format(limit=limits.max_tool_output_bytes)
    return ToolOutcome(content=clipped + note)


def _error(message: str) -> ToolOutcome:
    return ToolOutcome(content=message, is_error=True)


def _display_path(path: str) -> str:
    """Expose invisible spelling differences without changing filesystem paths."""
    return "".join(
        character.encode("unicode_escape").decode("ascii")
        if unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
        else character
        for character in path
    )


def _string(
    arguments: Mapping[str, object], key: str, *, default: str | None = None
) -> str:
    value = arguments.get(key, default)
    if not isinstance(value, str):
        raise ScopeValidationError(f"{key} must be a string")
    return value


_HANDLERS: Mapping[str, Callable[[ToolBroker, Mapping[str, object]], ToolOutcome]] = {
    "run_check": ToolBroker._run_check,
    "create_directory": ToolBroker._create_directory,
    "delete_file": ToolBroker._delete_file,
    "rename_file": ToolBroker._rename_file,
    "list_files": ToolBroker._list_files,
    "read_file": ToolBroker._read_file,
    "search_text": ToolBroker._search_text,
    "read_diff": ToolBroker._read_diff,
    "write_file": ToolBroker._write_file,
    "apply_patch": ToolBroker._apply_patch,
    "validate_changes": ToolBroker._validate_changes,
    "finish_task": ToolBroker._finish_task,
    "ask_user": ToolBroker._ask_user,
}


def tool_schemas(names: Sequence[str]) -> tuple[dict[str, object], ...]:
    """Return JSON-Schema definitions for the named tools, in order."""

    return tuple(_SCHEMAS[name] for name in names if name in _SCHEMAS)


def all_tool_names() -> tuple[str, ...]:
    """Every tool any broker can offer; native histories may reference any."""

    return tuple(_SCHEMAS)


def _schema(
    name: str, description: str, properties: dict[str, object], required: list[str]
) -> dict[str, object]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


_TEXT = {"type": "string"}

_SCHEMAS: Mapping[str, dict[str, object]] = {
    "run_check": _schema(
        "run_check",
        "Run a configured named check against pending edits.",
        {"name": _TEXT},
        ["name"],
    ),
    "create_directory": _schema(
        "create_directory",
        "Create a source directory within scope.",
        {"path": _TEXT},
        ["path"],
    ),
    "delete_file": _schema(
        "delete_file",
        "Delete one regular UTF-8 source file within scope.",
        {"path": _TEXT},
        ["path"],
    ),
    "rename_file": _schema(
        "rename_file",
        (
            "Rename a regular UTF-8 file to an absent destination; b"
            "oth paths must be in scope."
        ),
        {"source": _TEXT, "destination": _TEXT},
        ["source", "destination"],
    ),
    "list_files": _schema(
        "list_files",
        "List sorted entries of one directory. Continue with next_offset from "
        "the metadata when truncated. Pages reflect current checkout contents.",
        {
            "path": {**_TEXT, "description": "Directory path, '.' for the root."},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1},
            "snapshot": {
                **_TEXT,
                "description": "Pass the first page's snapshot with every nonzero "
                "offset; restart if it changes.",
            },
        },
        [],
    ),
    "read_file": _schema(
        "read_file",
        "Read UTF-8 source. Optional one-based start_line/end_line select a "
        "numbered range; start_column continues a long line. Follow metadata "
        "continuation positions when truncated. Partial shared reads keep the "
        "same observed source until refresh=true explicitly adopts current source. "
        "Use apply_patch "
        "after partial reads; replacing a file requires a complete visible read.",
        {
            "path": {**_TEXT, "description": "Repository-relative file path."},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "start_column": {"type": "integer", "minimum": 1},
            "refresh": {
                "type": "boolean",
                "description": "Explicitly refresh a pinned partial-read base "
                "when there are no pending edits.",
            },
        },
        ["path"],
    ),
    "search_text": _schema(
        "search_text",
        "Search for a Python regular expression. Results include file/line "
        "references. Use offset/limit to page matches and follow continuation "
        "metadata; narrow path when a scan limit is reached. Search pages are "
        "live context and do not authorize replacing a file.",
        {
            "pattern": {**_TEXT, "description": "Regular expression to search for."},
            "path": {
                **_TEXT,
                "description": "File or directory relative to the repo; '.' for root.",
            },
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1},
            "snapshot": {
                **_TEXT,
                "description": "Pass the first page's snapshot with every nonzero "
                "offset; restart if it changes.",
            },
        },
        ["pattern"],
    ),
    "read_diff": _schema(
        "read_diff",
        "Show every change made so far in this working tree.",
        {},
        [],
    ),
    "write_file": _schema(
        "write_file",
        "Create or replace one file. The path must be inside the task's "
        "claimed scopes.",
        {
            "path": {**_TEXT, "description": "Repository-relative file path."},
            "content": {**_TEXT, "description": "Complete new file contents."},
        },
        ["path", "content"],
    ),
    "apply_patch": _schema(
        "apply_patch",
        "Replace one unique span of text in an existing file. Preferred over "
        "write_file for edits. The path must be inside the task's claimed scopes.",
        {
            "path": {**_TEXT, "description": "Repository-relative file path."},
            "old_text": {
                **_TEXT,
                "description": "Exact text to replace; must be unique.",
            },
            "new_text": {**_TEXT, "description": "Replacement text."},
        },
        ["path", "old_text", "new_text"],
    ),
    "validate_changes": _schema(
        "validate_changes",
        "Check that every change made so far is inside the claimed scopes. "
        "Call this before finishing.",
        {},
        [],
    ),
    "finish_task": _schema(
        "finish_task",
        "Finish with the actual answer or deliverable the user requested. "
        "An account of work performed is not an answer. Declaring completion "
        "does not publish edits or override required checks.",
        {
            "answer": {
                **_TEXT,
                "minLength": 1,
                "maxLength": MAX_ANSWER_CHARACTERS,
                "description": "Complete user-facing Markdown answer, plan, findings, "
                "or change explanation. Never merely say an answer was prepared.",
            },
            "summary": {**_TEXT, "description": "Optional short operational report."},
            "outcome": {
                **_TEXT,
                "enum": ["completed", "blocked", "partial"],
                "description": "Whether the requested work is complete "
                "or remains unfinished.",
            },
        },
        ["answer"],
    ),
    "ask_user": _schema(
        "ask_user",
        "Ask the user for missing information or a decision needed to proceed, "
        "and wait for their answer. Inspect available context first; do not ask "
        "for routine implementation choices or confirmation of authorized work. "
        "Optional numbered suggestions still allow a custom answer. The "
        "question and formatted options together must fit within 2000 characters.",
        {
            "question": {
                **_TEXT,
                "minLength": 1,
                "maxLength": MAX_QUESTION_CHARACTERS,
                "description": "One concise question explaining the decision needed.",
            },
            "options": {
                "type": "array",
                "items": {
                    **_TEXT,
                    "minLength": 1,
                    "maxLength": _MAX_OPTION_CHARACTERS,
                },
                "minItems": 2,
                "maxItems": _MAX_QUESTION_OPTIONS,
                "uniqueItems": True,
                "description": "Optional distinct, single-line suggested answers.",
            },
        },
        ["question"],
    ),
}


__all__ = [
    "ToolBroker",
    "ToolBudgetExhausted",
    "ToolOutcome",
    "ToolUsage",
    "tool_schemas",
]
