"""A batch of reads answers exactly as the same calls made one after another.

Each case builds two identical brokers, runs the same read calls sequentially
through ``invoke`` on one and as a batch through ``invoke_reads`` on the other,
and compares every outcome, the broker state afterwards, and every event.
"""

from __future__ import annotations

import random
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from test_agent_loop import worktree as worktree

from llm_cli.agent import tools as tools_module
from llm_cli.agent.limits import ExecutionLimits
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import ToolBroker, ToolBudgetExhausted, ToolOutcome

Call = tuple[str, dict[str, object]]
Event = tuple[str, dict[str, object]]

_WORDS = ("alpha", "beta", "gamma", "delta", "render", "value", "TODO", "def")
_FILES = {
    "README.md": "# Example\n\nalpha beta\n",
    "src/app.py": "def main():\n    value = 1\n    return value\n",
    "src/pkg/__init__.py": "",
    "src/pkg/util.py": "".join(
        f"def render_{i}():\n    return {i}\n" for i in range(60)
    ),
    "docs/guide.md": "Guide\n\nTODO: gamma\n",
    "docs/notes/a.md": "delta notes\n",
    "empty.txt": "",
    "big.txt": "".join(f"line {i} beta value\n" for i in range(2000)),
    "build/out.txt": "ignored output\n",
    "debug.log": "ignored log\n",
    ".env": "TOKEN=private\n",
    "secret.txt": "key = sk-proj-" + "a1b2c3d4" * 4 + "\n",
}
_DIRECTORIES = (".", "src", "src/pkg", "docs", "docs/notes", "build", "missing")
_EXTRA_PATHS = ("missing.txt", "binary.dat", "src", "../outside.txt", "docs/new.md")


def _matches_here(
    pattern: str,
    files: list[tuple[str, str]],
    *,
    offset: int,
    limit: int,
    budget: Any,
) -> list[tuple[str, int, str]]:
    """The regex worker's matching, in this process (see bounded_search)."""

    import re

    from llm_cli.agent.bounded_search import SearchError

    budget.remaining()
    try:
        expression = re.compile(pattern)
    except (re.error, RecursionError, OverflowError) as exc:
        raise SearchError("that is not a valid regular expression") from exc
    matches: list[tuple[str, int, str]] = []
    seen = 0
    for relative, content in files:
        for number, line in enumerate(content.splitlines(), start=1):
            if expression.search(line):
                if seen >= offset:
                    matches.append((relative, number, line.strip()[:201]))
                    if len(matches) > limit:
                        return matches
                seen += 1
    return matches


@pytest.fixture(autouse=True)
def patient_searches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compare what reads answer, not how fast a stalled machine runs them.

    A search's five-second limit is wall-clock time, so on a slow runner either
    the sequential or the batched copy could run out where the other does not.
    Each search also starts a regex worker process; macOS CI runners have hung
    starting some of the hundreds these cases need, so both copies match in
    this process instead. The worker itself is tested in test_bounded_search.
    """

    from llm_cli.agent import bounded_search

    monkeypatch.setattr(bounded_search, "SEARCH_TIMEOUT_SECONDS", 120.0)
    monkeypatch.setattr(tools_module, "find_matches", _matches_here)


@pytest.fixture(scope="module")
def repository(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("parallel-reads") / "repo"
    for relative, text in _FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (root / "binary.dat").write_bytes(b"\xff\xfe\x00not utf-8")
    (root / ".gitignore").write_text("build/\n*.log\n.env\n")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["add", "."],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True)
    return root


def _calls(rng: random.Random) -> list[Call]:
    files = [*_FILES, *_EXTRA_PATHS]
    calls: list[Call] = []
    for _ in range(rng.randint(2, 12)):
        kind = rng.choices(
            ("read_file", "list_files", "search_text", "read_diff"),
            weights=(6, 2, 2, 1),
        )[0]
        arguments: dict[str, object] = {}
        if kind == "read_file":
            # A small pool makes repeated reads of one path common.
            arguments["path"] = rng.choice(files[: rng.choice((4, len(files)))])
            if rng.random() < 0.4:
                start = rng.randint(1, 40)
                arguments.update(start_line=start, end_line=start + rng.randint(0, 30))
            if rng.random() < 0.15:
                arguments["refresh"] = True
        elif kind == "list_files":
            arguments["path"] = rng.choice(_DIRECTORIES)
        elif kind == "search_text":
            arguments["pattern"] = rng.choice((*_WORDS, "(", r"render_\d+", "nothing"))
            if rng.random() < 0.5:
                arguments["path"] = rng.choice(_DIRECTORIES[:5])
        calls.append((kind, arguments))
    return calls


def _limits(rng: random.Random) -> ExecutionLimits:
    return rng.choice(
        (
            ExecutionLimits(),
            # Budgets that run out partway through a batch.
            ExecutionLimits(max_read_files=3),
            ExecutionLimits(max_read_bytes=600),
            ExecutionLimits(max_tool_calls=9),
            ExecutionLimits(max_tool_output_bytes=1_024, max_search_results=3),
        )
    )


def _pending_edits(broker: SharedToolBroker, rng: random.Random) -> None:
    """Stage the same private edits on each shared broker before its batch."""

    for path in rng.sample(("docs/guide.md", "src/app.py", "docs/new.md"), k=2):
        broker.invoke("read_file", {"path": path})
        broker.invoke("write_file", {"path": path, "content": f"edited {path}\n"})
    if rng.random() < 0.5:
        broker.invoke("read_file", {"path": "README.md"})
        broker.invoke("delete_file", {"path": "README.md"})


def _run(
    run: Callable[[], list[ToolOutcome]],
) -> tuple[list[ToolOutcome] | None, str | None]:
    try:
        return run(), None
    except ToolBudgetExhausted as exc:
        return None, str(exc)


def _sequential(
    broker: ToolBroker, calls: list[Call]
) -> tuple[list[ToolOutcome] | None, str | None]:
    def run() -> list[ToolOutcome]:
        return [broker.invoke(name, arguments) for name, arguments in calls]

    return _run(run)


def _make(
    kind: str, root: Path, limits: ExecutionLimits, events: list[Event]
) -> ToolBroker:
    options: dict[str, Any] = {
        "worktree": root,
        "scopes": ("*",),
        "limits": limits,
        "on_event": lambda event, payload: events.append((event, dict(payload))),
    }
    return SharedToolBroker(**options) if kind == "shared" else ToolBroker(**options)


def _state(broker: ToolBroker) -> Mapping[str, object]:
    return broker.usage_snapshot()


@pytest.mark.parametrize("kind", ["shared", "isolated"])
@pytest.mark.parametrize("seed", range(60))
def test_a_batch_of_reads_matches_the_same_calls_in_order(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    seed: int,
) -> None:
    rng = random.Random(f"{kind}-{seed}")
    calls = _calls(rng)
    limits = _limits(rng)
    edits = random.Random(rng.random())
    sequential_events: list[Event] = []
    batched_events: list[Event] = []
    one = _make(kind, repository, limits, sequential_events)
    other = _make(kind, repository, limits, batched_events)
    if isinstance(one, SharedToolBroker) and isinstance(other, SharedToolBroker):
        state = edits.getstate()
        _pending_edits(one, edits)
        edits.setstate(state)
        _pending_edits(other, edits)
    prepared: list[int] = []
    prepare = tools_module.ToolBroker._prepare_reads

    def spy(self: ToolBroker, batch: Any) -> Any:
        prepared.append(len(batch))
        return prepare(self, batch)

    monkeypatch.setattr(tools_module.ToolBroker, "_prepare_reads", spy)

    expected = _sequential(one, calls)
    actual = _run(lambda: other.invoke_reads(calls))

    assert prepared == [len(calls)], "the batch did not take the batched path"
    assert actual == expected, calls
    assert _state(other) == _state(one)
    assert batched_events == sequential_events


def _turn(*calls: tuple[str, dict[str, object]]) -> Any:
    from llm_cli.providers.base import ModelTurn, ToolCallRequest

    return ModelTurn(
        text="",
        tool_calls=tuple(
            ToolCallRequest(call_id=f"call_{index}", name=name, arguments=arguments)
            for index, (name, arguments) in enumerate(calls)
        ),
        stop_reason="tool_use",
    )


def _loop(
    worktree: Path, monkeypatch: pytest.MonkeyPatch, batch: int
) -> tuple[Any, Mapping[str, object]]:
    from dataclasses import replace

    from test_agent_loop import ScriptedProvider, _request

    from llm_cli.agent import harness

    monkeypatch.setattr(harness, "_MAX_READ_BATCH", batch)
    provider = ScriptedProvider(
        [
            _turn(
                ("read_file", {"path": "docs/guide.md"}),
                ("list_files", {"path": "docs"}),
                ("search_text", {"pattern": "hi"}),
                ("read_file", {"path": "src/app.py", "start_line": 1}),
                ("read_file", {"path": "docs/missing.md"}),
                ("write_file", {"path": "docs/guide.md", "content": "new\n"}),
                ("read_file", {"path": "docs/guide.md"}),
                ("read_diff", {}),
            ),
            _turn(("finish_task", {"answer": "done", "summary": "done"})),
        ]
    )
    broker = SharedToolBroker(worktree=worktree, scopes=("*",))
    harness.CodingAgentHarness(provider).run(
        replace(_request(worktree), scopes=("*",), workspace_mode="shared"), broker
    )
    return provider.sent_results, broker.usage_snapshot()


def test_the_agent_loop_answers_batched_reads_as_it_did_one_by_one(
    worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one_by_one = _loop(worktree, monkeypatch, batch=1)
    batched = _loop(worktree, monkeypatch, batch=16)

    assert batched == one_by_one
    results = batched[0][0]
    assert "old" in results[0].content and not results[0].is_error
    assert results[4].is_error  # docs/missing.md
    assert "new" in results[6].content  # the read after the write sees it


def test_a_stop_during_a_batch_of_reads_reads_again_on_resume(
    worktree: Path,
) -> None:
    import copy

    from test_agent_loop import ScriptedProvider, _broker, _request

    from llm_cli.agent.harness import CodingAgentHarness

    checkpoints: list[dict[str, object]] = []

    def save_and_stop(state: Mapping[str, object]) -> None:
        saved = copy.deepcopy(dict(state))
        checkpoints.append(saved)
        if saved["phase"] == "tools" and saved["in_flight_call"] == 0:
            raise KeyboardInterrupt

    reads = _turn(
        ("read_file", {"path": "docs/guide.md"}),
        ("list_files", {"path": "docs"}),
    )
    from dataclasses import replace

    with pytest.raises(KeyboardInterrupt):
        CodingAgentHarness(ScriptedProvider([reads])).run(
            replace(_request(worktree), checkpoint=save_and_stop),
            _broker(worktree, events=[]),
        )
    resumed = ScriptedProvider(
        [_turn(("finish_task", {"answer": "read", "summary": "read"}))]
    )
    CodingAgentHarness(resumed).run(
        replace(
            _request(worktree),
            resume_state=checkpoints[-1],
            checkpoint=lambda state: checkpoints.append(copy.deepcopy(dict(state))),
        ),
        _broker(worktree, events=[]),
    )

    first, second = resumed.sent_results[0]
    assert not first.is_error and "old" in first.content
    assert not second.is_error and "docs/guide.md" in second.content


class _GuardingHooks:
    """A pre_tool hook on read_file that allows every call."""

    def guards(self, name: str) -> bool:
        return name == "read_file"

    def before_tool(self, name: str, arguments: Mapping[str, object]) -> None:
        return None


def test_reads_a_pre_tool_hook_guards_run_one_by_one(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = SharedToolBroker(worktree=repository, scopes=("*",))
    broker.hooks = _GuardingHooks()  # type: ignore[assignment]
    prepared: list[object] = []
    monkeypatch.setattr(
        tools_module.ToolBroker,
        "_prepare_reads",
        lambda self, batch: prepared.append(batch),
    )

    outcomes = broker.invoke_reads(
        [("read_file", {"path": "README.md"}), ("list_files", {"path": "."})]
    )

    assert prepared == []
    assert not outcomes[0].is_error and "alpha beta" in outcomes[0].content


def test_a_batch_matches_searches_without_holding_the_read_barrier(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow pattern must not block other sessions' reads and publication."""

    import threading

    broker = SharedToolBroker(worktree=repository, scopes=("*",))
    respond = tools_module._search_response
    free: list[bool] = []

    def matching(*args: Any, **kwargs: Any) -> Any:
        taken: list[bool] = []

        def other_session() -> None:
            acquired = broker.publication_lock.acquire(timeout=1)
            taken.append(acquired)
            if acquired:
                broker.publication_lock.release()

        thread = threading.Thread(target=other_session)
        thread.start()
        thread.join()
        free.extend(taken)
        return respond(*args, **kwargs)

    monkeypatch.setattr(tools_module, "_search_response", matching)
    outcomes = broker.invoke_reads(
        [
            ("search_text", {"pattern": "alpha"}),
            ("search_text", {"pattern": "render"}),
            ("read_file", {"path": "README.md"}),
        ]
    )

    assert free == [True, True]
    assert not any(outcome.is_error for outcome in outcomes)


def test_a_batch_never_reads_a_file_its_call_refuses(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = SharedToolBroker(worktree=repository, scopes=("*",))
    observe = SharedToolBroker._observe
    read: list[str] = []

    def recording(self: SharedToolBroker, relative: str, **kwargs: Any) -> Any:
        read.append(relative)
        return observe(self, relative, **kwargs)

    monkeypatch.setattr(SharedToolBroker, "_observe", recording)
    outcomes = broker.invoke_reads(
        [
            ("read_file", {"path": ".env"}),
            ("read_file", {"path": "build/out.txt"}),
            ("read_file", {"path": "README.md"}),
        ]
    )

    assert [outcome.is_error for outcome in outcomes] == [True, True, False]
    assert ".env" not in read and "build/out.txt" not in read
    assert read == ["README.md"]


def test_waiting_for_the_rest_of_a_batch_is_not_search_time(repository: Path) -> None:
    import time

    from llm_cli.agent.bounded_search import SearchBudget

    broker = SharedToolBroker(worktree=repository, scopes=("*",))
    arguments: dict[str, object] = {"pattern": "alpha"}
    snapshot = broker._batch_snapshot(arguments, SearchBudget(lambda: None))
    # Its limit ran out a second ago, but only after eleven seconds of waiting
    # for the rest of the batch, which is not this search's own time.
    now = time.monotonic()
    budget = SearchBudget(lambda: None, deadline=now - 1)
    prepared = tools_module._SearchPrepared(budget, snapshot, ready_at=now - 11)

    (outcome,) = broker._match_searches([("search_text", arguments)], [prepared])

    assert isinstance(outcome, ToolOutcome) and not outcome.is_error
    assert "README.md" in outcome.content
