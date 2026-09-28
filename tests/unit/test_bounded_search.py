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

_PROBE = r'''
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
'''


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
