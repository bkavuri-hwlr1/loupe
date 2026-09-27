"""Operator clarifications traverse real CLI input, Unix RPC and agent tools."""

from __future__ import annotations

import json
import signal
import subprocess
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from test_cli_process_orchestration import (
    _CONTEXT,
    _BarrierProvider,
    _Cluster,
    _turn,
    _wait,
)

from llm_cli.providers.base import ModelTurn, ToolCallResult


class _QuestionProvider(_BarrierProvider):
    """Read, ask, then use precisely the operator's answer in a source edit."""

    def send_user(self, text: str) -> ModelTurn:
        self.phase = 0
        return super().send_user(text)

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        assert all(not result.is_error for result in results), results
        self.evidence[f"results_{self.phase}"] = [result.content for result in results]
        self.phase += 1
        if self.phase == 1:
            if self.request.get("prewrite"):
                return ModelTurn(
                    text="Preparing a private draft before clarification",
                    tool_calls=tuple(
                        _turn("write_file", path=path, content="provisional draft\n")
                        for path in self.request["files"]
                    ),
                )
            self.phase = 2
        if self.phase == 2:
            return ModelTurn(
                text="One decision is needed to finish this edit",
                tool_calls=(
                    _turn(
                        "ask_user",
                        question=self.request["question"],
                        **(
                            {"options": self.request["options"]}
                            if "options" in self.request
                            else {}
                        ),
                    ),
                ),
            )
        if self.phase == 3:
            self.evidence["answer"] = results[0].content.split(_CONTEXT)[0]
            self._record("answered")
            return ModelTurn(
                text="Applying your answer",
                tool_calls=tuple(
                    _turn(
                        "write_file", path=path, content=self.evidence["answer"] + "\n"
                    )
                    for path in self.request["files"]
                ),
            )
        return ModelTurn(
            text="",
            tool_calls=(
                _turn(
                    "finish_task",
                    answer="Used your answer to prepare the requested edits.",
                    summary="used operator answer",
                ),
            ),
        )


def _daemon_entry() -> None:
    from llm_cli.daemon import main

    original = main.DaemonService

    class Service(original):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.providers.register(
                "process-test",
                lambda model: (
                    _BarrierProvider(model)
                    if model.startswith("peer")
                    else _QuestionProvider(model)
                ),
            )

    main.DaemonService = Service
    main.main(["--profile", "test"])


class _QuestionCluster(_Cluster):
    def _spawn(self, name: str, args: list[str]) -> subprocess.Popen[str]:
        if name == "daemon":
            args = [
                "-c",
                "from test_cli_questions import _daemon_entry; _daemon_entry()",
            ]
        return super()._spawn(name, args)

    def pending(self, label: str, *, log_name: str | None = None) -> dict[str, Any]:
        task_id = self.task(label)["task_id"]
        question = _wait(
            lambda: (
                pending
                if (pending := self.rpc("task.question", task_id=task_id))["pending"]
                else None
            )
        )
        log = self.root / f"{log_name or label}.log"
        _wait(lambda: "Magnifio needs your input" in log.read_text())
        return question


@pytest.fixture
def question_cluster(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> Iterator[_QuestionCluster]:
    repository = repository_factory(
        tmp_path, {"docs/a.md": "base a\n", "docs/b.md": "base b\n"}
    )
    with TemporaryDirectory(prefix="mfi-ask-", dir="/tmp") as runtime:
        cluster = _QuestionCluster(tmp_path, repository, runtime)
        try:
            yield cluster
        finally:
            cluster.close()


@pytest.mark.parametrize(
    ("options", "answer", "expected"),
    [
        (["JSON", "CSV"], "2", "CSV"),
        (["JSON", "CSV"], "Use tab-separated values", "Use tab-separated values"),
        (None, "Include the timezone", "Include the timezone"),
    ],
    ids=["numbered-choice", "custom-choice", "freeform-question"],
)
def test_cli_answer_drives_edit_while_independent_peer_completes(
    question_cluster: _QuestionCluster,
    options: list[str] | None,
    answer: str,
    expected: str,
) -> None:
    cluster = question_cluster
    primary, peer = cluster.chat("primary"), cluster.chat("peer")
    cluster.submit(
        primary,
        "primary",
        {"docs/a.md": ""},
        question="Which export format should this use?",
        **({"options": options} if options is not None else {}),
    )
    pending = cluster.pending("primary")
    assert "Which export format should this use?" in pending["question"]
    output = (cluster.root / "primary.log").read_text()
    if options is not None:
        assert "1. JSON" in output and "2. CSV" in output
        assert "Reply with a number or your own answer." in output
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    cluster.submit(peer, "peer", {"docs/b.md": "peer completed while waiting\n"})
    cluster.ready("peer")
    cluster.release("peer")
    assert cluster.settled("peer")["state"] == "completed"
    assert cluster.rpc("task.question", task_id=cluster.task("primary")["task_id"])[
        "pending"
    ]
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    cluster.send(primary, answer)
    assert cluster.settled("primary")["state"] == "completed"
    evidence = json.loads((cluster.signals / "primary.answered.json").read_text())
    assert evidence["answer"] == expected
    assert (cluster.repository / "docs/a.md").read_text() == expected + "\n"
    assert (
        cluster.repository / "docs/b.md"
    ).read_text() == "peer completed while waiting\n"
    assert not cluster.rpc("task.question", task_id=cluster.task("primary")["task_id"])[
        "pending"
    ]


def test_question_survives_detach_and_resume_without_reasking_after_answer(
    question_cluster: _QuestionCluster,
) -> None:
    cluster = question_cluster
    primary = cluster.chat("primary")
    cluster.submit(
        primary,
        "primary",
        {"docs/a.md": ""},
        question="Which name should appear in the heading?",
        options=["Overview", "Details"],
    )
    original_question = cluster.pending("primary")
    original_task = cluster.task("primary")
    primary.send_signal(signal.SIGINT)
    _wait(lambda: "Question left pending" in (cluster.root / "primary.log").read_text())
    cluster.send(primary, "/detach")
    assert primary.wait(timeout=10) == 0
    assert (
        cluster.rpc("task.question", task_id=original_task["task_id"])["question_id"]
        == original_question["question_id"]
    )
    resumed = cluster.chat("resumed", resume=original_task["session_id"])
    cluster.send(resumed, "/attach")
    assert (
        cluster.pending("primary", log_name="resumed")["question_id"]
        == (original_question["question_id"])
    )
    cluster.send(resumed, "1")
    assert cluster.settled("primary")["state"] == "completed"
    assert (cluster.repository / "docs/a.md").read_text() == "Overview\n"
    cluster.send(resumed, "/attach")
    cluster.send(resumed, "/exit")
    assert resumed.wait(timeout=10) == 0
    # /attach replays the historical question in the transcript, but the
    # resolved question must never open a second answer editor.
    assert (cluster.root / "resumed.log").read_text().count("  ? ") == 1
    events = cluster.rpc("task.events", task_id=original_task["task_id"], limit=500)
    assert sum(event["event_type"] == "question.asked" for event in events) == 1
    assert sum(event["event_type"] == "question.answered" for event in events) == 1


def test_stop_at_question_retains_draft_and_does_not_stop_peer(
    question_cluster: _QuestionCluster,
) -> None:
    cluster = question_cluster
    primary, peer = cluster.chat("primary"), cluster.chat("peer")
    cluster.submit(
        primary,
        "primary",
        {"docs/a.md": ""},
        question="Should the draft use the new wording?",
        options=["Use the new wording", "Keep the old wording"],
        prewrite=True,
    )
    cluster.pending("primary")
    task_id = cluster.task("primary")["task_id"]
    assert "+provisional draft" in cluster.rpc("task.diff", task_id=task_id)["diff"]
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    cluster.send(primary, "/stop")
    assert cluster.settled("primary")["state"] == "cancelled"
    assert not cluster.rpc("task.question", task_id=task_id)["pending"]
    assert "+provisional draft" in cluster.rpc("task.diff", task_id=task_id)["diff"]
    assert not (cluster.signals / "primary.answered.json").exists()
    cluster.submit(peer, "peer", {"docs/b.md": "peer completed after cancellation\n"})
    cluster.ready("peer")
    cluster.release("peer")
    assert cluster.settled("peer")["state"] == "completed"
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    assert (
        cluster.repository / "docs/b.md"
    ).read_text() == "peer completed after cancellation\n"


def test_interactive_run_asks_and_publishes_answer_to_isolated_result_ref(
    question_cluster: _QuestionCluster, git_run: Callable[..., str]
) -> None:
    cluster = question_cluster
    cluster.rpc("repo.add", path=str(cluster.repository))
    title = "ORCHESTRATION " + json.dumps(
        {
            "label": "run-question",
            "files": {"docs/a.md": ""},
            "question": "Which summary format should the task produce?",
            "options": ["Compact", "Detailed"],
        }
    )
    before = (
        git_run(cluster.repository, "rev-parse", "HEAD"),
        git_run(cluster.repository, "write-tree"),
    )
    process = cluster._spawn(
        "run-question",
        [
            "-m",
            "llm_cli",
            "--profile",
            "test",
            "--plain",
            "run",
            title,
            "--repo",
            str(cluster.repository),
            "--scope",
            "docs/",
            "--provider",
            "process-test",
            "--model",
            "run-question",
            "--interactive",
        ],
    )
    pending = cluster.pending("run-question")
    assert "1. Compact" in pending["question"]
    assert "2. Detailed" in pending["question"]
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    cluster.send(process, "2")
    assert process.wait(timeout=10) == 0
    task = cluster.task("run-question")
    assert task["state"] == "ready_for_integration", task
    evidence = json.loads((cluster.signals / "run-question.answered.json").read_text())
    assert evidence["answer"] == "Detailed"
    assert (
        git_run(
            cluster.repository,
            "show",
            f"refs/llm-coord/tasks/{task['task_id']}:docs/a.md",
        )
        == "Detailed"
    )
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    assert before == (
        git_run(cluster.repository, "rev-parse", "HEAD"),
        git_run(cluster.repository, "write-tree"),
    )
