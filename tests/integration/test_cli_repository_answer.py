"""A real terminal answers repository questions and replays the answer cleanly."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import struct
import subprocess
import termios
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from test_cli_mode_shortcut import _Screen, _Terminal
from test_cli_modes import _ModeCluster
from test_cli_process_orchestration import _turn, _wait

from llm_cli.providers.base import ModelTurn, ToolCallResult

_PROMPT = "Give me a summary of what this repo is doing"
_PARAGRAPHS = (
    "Loupe is a terminal coding agent for local Git repositories.",
    "It connects models to file tools and preserves conversations in a daemon.",
    "Multiple sessions prepare private edits against the same checkout.",
    "Checks and review control publication, while durable state supports recovery.",
)
_ANSWER = "\n\n".join(_PARAGRAPHS)
_LONG_ANSWER = """# Repository guide

Loupe keeps the requested result readable while its operational details stay
out of the conversation.

## Durable delivery

The following deliberately long paragraph crosses several durable event chunks
and many narrow terminal rows: {body}.

```python
def answer_contract(request: str) -> str:
    return "the requested deliverable"
```

The final Markdown paragraph remains visible exactly once after the terminal resize.
""".format(body=" ".join(f"capability-{index:03d}" for index in range(600)))
_FILES = {
    "README.md": "# Loupe\nRAW_README_TOOL_RESULT_MUST_STAY_HIDDEN\n",
    "src/entry.py": "# RAW_SOURCE_TOOL_RESULT_MUST_STAY_HIDDEN\n",
}


class _OverviewProvider:
    """Read real files, then finish naturally without the finish_task tool."""

    name = "process-test"

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls = 0

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> _OverviewProvider:
        return self

    def snapshot(self) -> Mapping[str, object]:
        return {"calls": self.calls}

    def send_user(self, text: str) -> ModelTurn:
        assert _PROMPT in text
        assert self.calls == 0, "An informational answer must not need a finish nudge"
        self.calls += 1
        return ModelTurn(
            text="", tool_calls=tuple(_turn("read_file", path=path) for path in _FILES)
        )

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        if self.model == "summary-only" and self.calls == 2:
            assert len(results) == 1 and results[0].is_error, results
            assert "answer" in results[0].content
            self.calls += 1
            return ModelTurn(text=_ANSWER, stop_reason="end_turn")
        assert self.calls == 1
        assert len(results) == len(_FILES)
        assert all(not result.is_error for result in results), results
        for result, expected in zip(results, _FILES.values(), strict=True):
            assert expected.strip() in result.content
        self.calls += 1
        signals = Path(os.environ["ORCHESTRATION_SIGNALS"])
        (signals / "overview.observed.json").write_text(
            json.dumps(
                {"calls": self.calls, "read_results": [r.content for r in results]}
            )
        )
        if self.model == "resize-answer":
            (signals / "resize-answer.ready").touch()
            _wait(lambda: (signals / "resize-answer.release").exists(), timeout=30)
            return ModelTurn(text=_LONG_ANSWER, stop_reason="end_turn")
        if self.model == "summary-only":
            return ModelTurn(
                text="",
                tool_calls=(
                    _turn("finish_task", summary="Prepared a repository overview."),
                ),
                stop_reason="tool_use",
            )
        return ModelTurn(text=_ANSWER, stop_reason="end_turn")

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        raise AssertionError(f"natural completion must not record results: {results!r}")


def _daemon_entry() -> None:
    from llm_cli.daemon import main

    original = main.DaemonService

    class Service(original):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.providers.register(
                "process-test", lambda model: _OverviewProvider(model)
            )

    main.DaemonService = Service
    main.main(["--profile", "test"])


class _OverviewCluster(_ModeCluster):
    def _spawn(self, name: str, args: list[str]) -> subprocess.Popen[str]:
        # Bypass _ModeCluster's provider substitution while keeping the same
        # isolated daemon, socket, process cleanup and real terminal helpers.
        from test_cli_process_orchestration import _Cluster

        if name == "daemon":
            args = [
                "-c",
                "from test_cli_repository_answer import _daemon_entry; _daemon_entry()",
            ]
        return _Cluster._spawn(self, name, args)


@pytest.fixture
def overview_cluster(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> Iterator[_OverviewCluster]:
    repository = repository_factory(tmp_path, _FILES)
    with TemporaryDirectory(prefix="mfi-answer-", dir="/tmp") as runtime:
        cluster = _OverviewCluster(tmp_path, repository, runtime)
        try:
            yield cluster
        finally:
            cluster.close()


def _assert_answer_only(text: str, task: dict[str, Any]) -> None:
    normalized = " ".join(text.split())
    for paragraph in _PARAGRAPHS:
        assert normalized.count(paragraph) == 1, text
    for hidden in (
        task["task_id"],
        task["session_id"],
        "[intent]",
        "Task summary",
        "RAW_README_TOOL_RESULT",
        "RAW_SOURCE_TOOL_RESULT",
        "read_file",
        "finish_task",
        "provide the complete user-facing answer",
        "Not verified",
        "no file changes",
        "Elapsed",
        "to view this task again",
        "Ctrl-C detaches",
        "Prepared a repository overview",
        "Traceback",
    ):
        assert hidden not in text, text


def _idle_editor_geometry(
    terminal: _Terminal, *, columns: int
) -> tuple[list[str], int, int] | None:
    """Return a complete idle composer frame at either footer layout width."""

    with terminal.screen_lock:
        lines = terminal.screen.lines()
    prompts = [
        index
        for index, line in enumerate(lines)
        if line.startswith("  ❯")  # noqa: RUF001
    ]
    statuses = [
        index
        for index, line in enumerate(lines)
        if (
            line.startswith(" Model:")
            and "Effort:" in line
            and "Mode:" in line
        )
        or (
            line.startswith(" ")
            and " effort · " in line
            and line.endswith(" mode")
        )
    ]
    if not prompts or not statuses:
        return None
    prompt_row, status_row = prompts[-1], statuses[-1]
    if (
        prompt_row == 0
        or status_row <= prompt_row
        or status_row + 1 >= len(lines)
        or lines[prompt_row - 1] != "─" * columns
        or lines[status_row - 1] != "─" * columns
        or not all(lines[prompt_row : status_row - 1])
        or "Enter send" not in lines[status_row + 1]
        or any(lines[status_row + 2 :])
    ):
        return None
    return lines, prompt_row, status_row


def _resize_terminal(terminal: _Terminal, *, rows: int, columns: int) -> None:
    """Resize the actual PTY and reset the test's terminal model to that size."""

    fcntl.ioctl(
        terminal.master,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", rows, columns, 0, 0),
    )
    with terminal.screen_lock:
        terminal.screen = _Screen(rows=rows, columns=columns)
    os.kill(terminal.process.pid, signal.SIGWINCH)


@pytest.mark.parametrize("columns", [40, 80, 120])
@pytest.mark.parametrize("model", ["overview", "summary-only"])
def test_repo_question_answers_once_in_terminal_and_durable_replay(
    overview_cluster: _OverviewCluster,
    git_run: Callable[..., str],
    columns: int,
    model: str,
) -> None:
    cluster = overview_cluster
    with contextlib.closing(_Terminal(cluster, model, columns=columns)) as terminal:
        initial = len(terminal.text)
        terminal.write(_PROMPT + "\r")

        def completed() -> dict[str, Any] | None:
            tasks = cluster.rpc("task.list")
            return next((task for task in tasks if task["state"] == "completed"), None)

        task = _wait(completed)
        _wait(lambda: _idle_editor_geometry(terminal, columns=columns))
        live = terminal.text[initial:]
        _assert_answer_only(live, task)
        assert "/attach" not in live

        events = cluster.rpc("task.events", task_id=task["task_id"])
        finished = [
            event for event in events if event["event_type"] == "model.finished"
        ]
        assert len(finished) == 1
        assert finished[0]["payload"]["answer"] == _ANSWER
        evidence = json.loads((cluster.signals / "overview.observed.json").read_text())
        assert evidence["calls"] == 2
        assert "RAW_README_TOOL_RESULT" in evidence["read_results"][0]
        assert "RAW_SOURCE_TOOL_RESULT" in evidence["read_results"][1]
        assert git_run(cluster.repository, "status", "--porcelain") == ""

        replay_start = len(terminal.text)
        terminal.write("/attach\r")
        _wait(
            lambda: _PARAGRAPHS[0]
            in " ".join(terminal.text[replay_start:].split())
        )
        _wait(lambda: _idle_editor_geometry(terminal, columns=columns))
        replay = terminal.text[replay_start:]
        _assert_answer_only(replay, task)

        # A fresh viewer has no in-memory renderer state to help deduplication.
        viewer = cluster._spawn(
            "replayed-answer",
            [
                "-m",
                "llm_cli",
                "--profile",
                "test",
                "--plain",
                "task",
                "watch",
                task["task_id"],
            ],
        )
        assert viewer.wait(timeout=10) == 0
        replayed = (cluster.root / "replayed-answer.log").read_text()
        _assert_answer_only(replayed, task)
        assert "/attach" not in replayed


def test_long_markdown_answer_survives_real_resize_and_chunked_delivery(
    overview_cluster: _OverviewCluster,
) -> None:
    cluster = overview_cluster
    with contextlib.closing(
        _Terminal(cluster, "resize-answer", rows=42, columns=100)
    ) as terminal:
        initial = len(terminal.text)
        terminal.write(_PROMPT + "\r")
        _wait((cluster.signals / "resize-answer.ready").exists)

        # Resize while the provider is paused. The accepted answer is larger
        # than the durable event chunk, so its replay arrives in split parts.
        _resize_terminal(terminal, rows=52, columns=40)
        (cluster.signals / "resize-answer.release").touch()

        def completed() -> dict[str, Any] | None:
            tasks = cluster.rpc("task.list")
            return next((task for task in tasks if task["state"] == "completed"), None)

        task = _wait(completed)
        lines, prompt_row, status_row = _wait(
            lambda: _idle_editor_geometry(terminal, columns=40)
        )
        assert status_row == prompt_row + 2, "\n".join(lines)
        assert "normal mode" in lines[status_row]
        assert "Enter send" in lines[status_row + 1]

        live = terminal.text[initial:]
        normalized = " ".join(live.split())
        for marker in (
            "Repository guide",
            "Durable delivery",
            "capability-000",
            "capability-599",
            "def answer_contract(request: str) -> str:",
            "The final Markdown paragraph remains visible exactly once",
        ):
            assert normalized.count(marker) == 1, marker
        assert "Traceback" not in live
        assert "RAW_README_TOOL_RESULT" not in live
        assert task["task_id"] not in live

        events = cluster.rpc("task.events", task_id=task["task_id"])
        finished = [
            event for event in events if event["event_type"] == "model.finished"
        ]
        assert len(finished) >= 2
        assert [event["payload"]["part"] for event in finished] == list(
            range(len(finished))
        )
        assert sum(bool(event["payload"]["final"]) for event in finished) == 1
        assert "".join(event["payload"]["answer"] for event in finished) == _LONG_ANSWER
