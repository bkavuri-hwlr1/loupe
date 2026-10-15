"""A review's local deadline also bounds its synchronous source tools."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from llm_cli.agent import source_policy

_PROBE = r"""
import json
import os
import sys
import time
from pathlib import Path
from llm_cli.agent.reviewer import Reviewer
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import ToolBroker
from llm_cli.providers.base import ModelTurn, ToolCallRequest

root, kind, tool, slow = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
cls = SharedToolBroker if kind == "shared" else ToolBroker
broker = cls(root, ("*",))
if tool == "read_diff":
    assert not broker.invoke("read_file", {"path":"a.txt"}).is_error
    edit = broker.invoke("write_file", {"path":"a.txt", "content":"changed\n"})
    assert not edit.is_error
if slow == "git":
    exclude = root / ".git/info/exclude"
    exclude.unlink()
    os.mkfifo(exclude)
args = {"path":"a.txt"} if tool == "read_file" else {}
if tool == "search_text":
    args = {"path":"a.txt", "pattern":"(a+)+$" if slow == "regex" else "a"}

class Provider:
    name = model = "review-tool-deadline"
    def session(self, **kwargs):
        return self
    def send_user(self, text):
        return ModelTurn("", (ToolCallRequest("read", tool, args),), "tool_use")
    def send_tool_results(self, results):
        return ModelTurn('{"findings":[]}')

start = time.monotonic()
result = Reviewer(Provider(), broker, effort_ceiling=None,
    deadline=time.time()+0.2).review("inspect", {"line_counts":{"a.txt":1}})
print(json.dumps({"failed":bool(result.failure), "elapsed":time.monotonic()-start}))
"""


@pytest.mark.parametrize("kind", ["shared", "isolated"])
def test_builtin_review_regex_obeys_review_cutoff(
    tmp_path: Path, repository_factory: Callable[..., Path], kind: str
) -> None:
    root = repository_factory(tmp_path, {"a.txt": "a" * 1000 + "!\n"})
    result = _probe(root, kind, "search_text", "regex", timeout=7)
    assert result["failed"] and result["elapsed"] < 1


@pytest.mark.parametrize(
    ("kind", "tool"),
    [
        (kind, tool)
        for kind in ("shared", "isolated")
        for tool in ("read_file", "list_files", "search_text")
    ]
    + [("shared", "read_diff")],
)
def test_builtin_review_source_git_obeys_review_cutoff(
    tmp_path: Path, repository_factory: Callable[..., Path], kind: str, tool: str
) -> None:
    root = repository_factory(tmp_path, {"a.txt": "a\n"})
    result = _probe(root, kind, tool, "git", timeout=3)
    assert result["failed"] and result["elapsed"] < 1


def _probe(root: Path, kind: str, tool: str, slow: str, *, timeout: float) -> Any:
    with subprocess.Popen(
        [sys.executable, "-I", "-c", _PROBE, str(root), kind, tool, slow],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            assert process.returncode == 0, stderr
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    return json.loads(stdout)


def test_scoped_source_budget_combines_explicit_budget_and_restores_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[float | None] = []

    def git(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        remaining = kwargs["remaining"]
        seen.append(remaining() if remaining is not None else None)
        return subprocess.CompletedProcess([], 1, "")

    monkeypatch.setattr(source_policy, "run_git", git)
    monkeypatch.setattr(source_policy, "_user_excludes_file", lambda *args: "")
    with (
        pytest.raises(RuntimeError, match="scope exits"),
        source_policy.source_deadline(lambda: 0.5),
    ):
        source_policy.excluded_paths(tmp_path, ["a"], remaining=lambda: 0.1)
        source_policy.excluded_paths(tmp_path, ["a"], remaining=lambda: 1)
        with source_policy.source_deadline(lambda: 2):
            source_policy.excluded_paths(tmp_path, ["a"])
        raise RuntimeError("scope exits")
    source_policy.excluded_paths(tmp_path, ["a"])
    assert seen == [0.1, 0.5, 0.5, None]


def test_scoped_deadline_is_checked_before_cached_exclusions(
    tmp_path: Path,
) -> None:
    def expired() -> float:
        raise TimeoutError("review cutoff")

    with (
        source_policy.known_exclusions(tmp_path, {"a": False}),
        source_policy.source_deadline(expired),
        pytest.raises(TimeoutError, match="review cutoff"),
    ):
        source_policy.excluded_paths(tmp_path, ["a"])


def test_exclusion_repository_lock_wait_obeys_local_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = threading.Lock()
    lock.acquire()
    monkeypatch.setattr(source_policy, "_empty_lock", lock)
    deadline = time.monotonic() + 0.05

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("review cutoff")
        return value

    done = threading.Event()
    errors: list[BaseException] = []

    def acquire() -> None:
        try:
            source_policy._empty_repository(remaining)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=acquire, daemon=True)
    before = time.monotonic()
    worker.start()
    try:
        assert done.wait(0.5), "exclusion setup waited past the helper deadline"
    finally:
        lock.release()
        worker.join(2)
    assert len(errors) == 1 and isinstance(errors[0], TimeoutError)
    assert str(errors[0]) == "review cutoff"
    assert time.monotonic() - before < 0.5
