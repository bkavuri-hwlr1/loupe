"""Untrusted regexes are exercised behind an independent process watchdog."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import pytest

_PROBE = r"""
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import ToolBroker, TaskCancelled

bounded = sys.modules.get("llm_cli.agent.bounded_search")
if bounded is not None:
    bounded.SEARCH_TIMEOUT_SECONDS = 0.35
workers = []
matching = threading.Event()
if bounded is not None:
    original_popen = bounded.subprocess.Popen
    def notify_worker(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        workers.append(process)
        matching.set()
        return process
    bounded.subprocess.Popen = notify_worker
kind = SharedToolBroker if sys.argv[3] == "shared" else ToolBroker
cancelled = threading.Event()
broker = kind(Path(sys.argv[2]), ("*",), cancelled=cancelled.is_set)
start = time.monotonic()
if sys.argv[4] == "cancel":
    threading.Timer(0.15, cancelled.set).start()
reader = None
read_results = []
if sys.argv[4] == "parallel":
    other = SharedToolBroker(
        broker.worktree, ("*",), publication_lock=broker.publication_lock
    )
    def read_while_searching():
        assert matching.wait(1)
        result = other.invoke("read_file", {"path": "source.txt"})
        read_results.append(not result.is_error)
        cancelled.set()
    reader = threading.Thread(target=read_while_searching)
    reader.start()
try:
    path = "." if sys.argv[4] == "aggregate" else "source.txt"
    result = broker.invoke("search_text", {"path": path, "pattern": "(a+)+$"})
    output = {"error": result.is_error, "content": result.content}
except TaskCancelled:
    output = {"cancelled": True}
if reader is not None:
    reader.join(1)
    output["parallel_read"] = read_results
output["elapsed"] = time.monotonic() - start
output["running_workers"] = sum(worker.poll() is None for worker in workers)
print(json.dumps(output))
"""


def _probe(root: Path, kind: str, action: str) -> dict[str, object]:
    with subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            _PROBE,
            str(Path(__file__).resolve().parents[2] / "src"),
            str(root),
            kind,
            action,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=3)
            assert process.returncode == 0, stderr
        finally:
            # A regression must not leave either the probe or its regex child
            # running after the independent watchdog kills this process group.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    result: dict[str, object] = json.loads(stdout)
    assert result["running_workers"] == 0
    return result


@pytest.mark.parametrize("kind", ["isolated", "shared"])
@pytest.mark.parametrize("action", ["timeout", "cancel", "aggregate"])
def test_pathological_search_is_bounded_and_cancellable(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    kind: str,
    action: str,
) -> None:
    files = (
        {f"source-{number}.txt": "a" * 22 + "!\n" for number in range(10)}
        if action == "aggregate"
        else {"source.txt": "a" * 100 + "!\n"}
    )
    root = repository_factory(tmp_path, files)
    output = _probe(root, kind, action)
    elapsed = output["elapsed"]
    assert isinstance(elapsed, float) and elapsed < 1.5
    if action == "cancel":
        assert output["cancelled"] is True
    else:
        assert output["error"] is True
        assert "time limit" in str(output["content"])


def test_shared_search_releases_publication_lock_during_matching(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "a" * 100 + "!\n"})
    output = _probe(root, "shared", "parallel")
    assert output["cancelled"] is True
    assert output["parallel_read"] == [True]


def test_worker_preserves_advanced_regex_features_and_ignores_repo_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.agent.bounded_search import SearchBudget, find_matches

    marker = tmp_path / "executed"
    (tmp_path / "re.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError()"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    matches = find_matches(
        r"(?i)(?<=prefix )(?P<word>\w+) (?P=word)$",
        [("source.txt", "unrelated\nprefix Same same\nprefix no match\n")],
        offset=0,
        limit=10,
        budget=SearchBudget(lambda: None),
    )
    assert matches == [("source.txt", 2, "prefix Same same")]
    assert not marker.exists()


@pytest.mark.parametrize("pattern", ["[", "(" * 5_000])
def test_worker_rejects_invalid_patterns_without_crashing_daemon(pattern: str) -> None:
    from llm_cli.agent.bounded_search import SearchBudget, SearchError, find_matches

    with pytest.raises(SearchError, match="not a valid regular expression"):
        find_matches(pattern, [], offset=0, limit=10, budget=SearchBudget(lambda: None))


@pytest.fixture
def fresh_pool(monkeypatch: pytest.MonkeyPatch):
    """Give lifecycle assertions their own bounded children and reap them."""
    from llm_cli.agent import bounded_search

    pool = bounded_search._SearchPool()
    monkeypatch.setattr(bounded_search, "_POOL", pool)
    try:
        yield pool
    finally:
        pool.close()


def test_repeated_searches_reuse_a_bounded_number_of_isolated_workers(
    fresh_pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.agent import bounded_search

    processes = []
    create = bounded_search.subprocess.Popen

    def recording(*args, **kwargs):
        process = create(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(bounded_search.subprocess, "Popen", recording)
    for _ in range(12):
        assert bounded_search.find_matches(
            "needle",
            [("a", "needle\nneedle two\nneedle three\n")],
            offset=1,
            limit=1,
            budget=bounded_search.SearchBudget(lambda: None),
        ) == [("a", 2, "needle two"), ("a", 3, "needle three")]
    assert len(processes) == bounded_search._WORKER_COUNT
    assert all(process.poll() is None for process in processes)
    fresh_pool.close()
    assert all(process.poll() is not None for process in processes)


@pytest.mark.parametrize("cancel", [False, True])
def test_stopped_worker_is_reaped_and_replaced_before_next_search(cancel: bool) -> None:
    import time

    from llm_cli.agent.bounded_search import SearchBudget, SearchError, SearchWorker
    from llm_cli.agent.tools import TaskCancelled

    worker = SearchWorker()
    healthy = (
        json.dumps(
            {"pattern": "alpha", "files": [["a", "alpha\n"]], "offset": 0, "limit": 1}
        ).encode()
        + b"\n"
    )
    bad = (
        json.dumps(
            {
                "pattern": "(a+)+$",
                "files": [["a", "a" * 100 + "!\n"]],
                "offset": 0,
                "limit": 1,
            }
        ).encode()
        + b"\n"
    )
    try:
        assert worker.match(healthy, SearchBudget(lambda: None))["matches"] == [
            ["a", 1, "alpha"]
        ]
        original = worker.process
        assert original is not None
        stop_at = time.monotonic() + 0.1

        def cancelled() -> None:
            if cancel and time.monotonic() >= stop_at:
                raise TaskCancelled("task stopped")

        with pytest.raises(TaskCancelled if cancel else SearchError):
            worker.match(
                bad,
                SearchBudget(
                    cancelled, deadline=stop_at if not cancel else stop_at + 1
                ),
            )
        assert original.poll() is not None
        assert worker.process is None
        assert worker.match(healthy, SearchBudget(lambda: None))["matches"] == [
            ["a", 1, "alpha"]
        ]
        assert worker.process is not original
    finally:
        worker.close()


def test_wait_for_busy_pool_uses_the_search_deadline(fresh_pool) -> None:
    import time
    from contextlib import ExitStack

    from llm_cli.agent.bounded_search import SearchBudget, SearchError, find_matches

    with ExitStack() as stack:
        for _ in fresh_pool.workers:
            stack.enter_context(fresh_pool.lease(SearchBudget(lambda: None)))
        start = time.monotonic()
        with pytest.raises(SearchError, match="time limit"):
            find_matches(
                "alpha",
                [],
                offset=0,
                limit=1,
                budget=SearchBudget(lambda: None, deadline=start + 0.1),
            )
        assert time.monotonic() - start < 1
    assert find_matches(
        "alpha", [("a", "alpha")], offset=0, limit=1, budget=SearchBudget(lambda: None)
    ) == [("a", 1, "alpha")]


def test_worker_exit_is_reported_and_next_request_uses_a_new_child() -> None:
    from llm_cli.agent.bounded_search import SearchBudget, SearchWorker

    worker = SearchWorker()
    payload = b'{"pattern":"x","files":[["a","x"]],"offset":0,"limit":1}\n'
    try:
        worker.match(payload, SearchBudget(lambda: None))
        first = worker.process
        assert first is not None
        first.kill()
        first.wait()
        assert worker.match(payload, SearchBudget(lambda: None))["matches"] == [
            ["a", 1, "x"]
        ]
        assert worker.process is not first
    finally:
        worker.close()
