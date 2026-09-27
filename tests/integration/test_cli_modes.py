"""Real CLI processes enforce plan, normal review and automatic publication."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from test_cli_process_orchestration import _Cluster, _turn, _wait

from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult

_MUTATORS = {
    "write_file",
    "apply_patch",
    "create_directory",
    "delete_file",
    "rename_file",
    "run_check",
}
_PLAN = "Read the existing heading, replace it, then verify the resulting document."


class _ModeProvider:
    name = "process-test"

    def __init__(self, model: str) -> None:
        self.model = model
        self.signals = Path(os.environ["ORCHESTRATION_SIGNALS"])
        self.request: dict[str, Any] = {}
        self.evidence: dict[str, Any] = {}
        self.phase = 0
        self.plan: str | None = None
        self.system = ""
        self.tools: list[str] = []

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> _ModeProvider:
        self.system = system
        self.tools = [str(tool["name"]) for tool in tools]
        self.plan = str(state["plan"]) if state and state.get("plan") else None
        return self

    def snapshot(self) -> Mapping[str, object]:
        return {"plan": self.plan}

    def _record(self) -> None:
        path = self.signals / f"{self.request['label']}.observed.json"
        path.write_text(json.dumps(self.evidence))

    def send_user(self, text: str) -> ModelTurn:
        self.request, _ = json.JSONDecoder().raw_decode(text.split("ORCHESTRATION ")[1])
        self.phase = 0
        self.evidence = {
            "system": self.system,
            "tools": self.tools,
            "prior_plan": self.plan,
        }
        return ModelTurn(
            text="Inspecting source", tool_calls=(_turn("read_file", path="docs/a.md"),)
        )

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        self.evidence[f"results_{self.phase}"] = [
            {"content": result.content, "is_error": result.is_error}
            for result in results
        ]
        self.phase += 1
        action = self.request.get("action", "write")
        if self.phase == 1:
            assert all(not result.is_error for result in results), results
            if self.request.get("pause"):
                (self.signals / f"{self.request['label']}.ready.json").write_text("{}")
                _wait(
                    lambda: (
                        self.signals / f"{self.request['label']}.release"
                    ).exists(),
                    timeout=30,
                )
            if action == "plan":
                self.plan = _PLAN
                self._record()
                return ModelTurn(
                    text=_PLAN,
                    tool_calls=(_turn("finish_task", answer=_PLAN, summary=_PLAN),),
                )
            if action == "forged":
                return ModelTurn(
                    text="Attempting tools that plan mode must refuse",
                    tool_calls=(
                        _turn("write_file", path="docs/a.md", content="forged\n"),
                        _turn(
                            "apply_patch",
                            path="docs/a.md",
                            old_text="base a",
                            new_text="forged",
                        ),
                        _turn("create_directory", path="docs/new-directory"),
                        _turn("delete_file", path="docs/a.md"),
                        _turn(
                            "rename_file",
                            source="docs/a.md",
                            destination="docs/moved.md",
                        ),
                        ToolCallRequest(
                            name="run_check",
                            call_id="forged-check",
                            arguments={"name": "must-not-run"},
                        ),
                    ),
                )
            return ModelTurn(
                text="Preparing the requested change",
                tool_calls=(
                    _turn(
                        "write_file", path="docs/a.md", content=self.request["content"]
                    ),
                ),
            )
        self._record()
        if action == "forged":
            assert all(result.is_error for result in results), results
        else:
            assert all(not result.is_error for result in results), results
        return ModelTurn(
            text="",
            tool_calls=(
                _turn(
                    "finish_task",
                    answer="Completed the requested work.",
                    summary="done",
                ),
            ),
        )

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        self.evidence["terminal_results"] = [result.content for result in results]


def _daemon_entry() -> None:
    from llm_cli.daemon import main

    original = main.DaemonService

    class Service(original):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.providers.register("process-test", lambda model: _ModeProvider(model))

    main.DaemonService = Service
    main.main(["--profile", "test"])


class _ModeCluster(_Cluster):
    def _spawn(self, name: str, args: list[str]) -> subprocess.Popen[str]:
        if name == "daemon":
            args = ["-c", "from test_cli_modes import _daemon_entry; _daemon_entry()"]
        return super()._spawn(name, args)

    def chat(
        self,
        name: str,
        *,
        resume: str | None = None,
        publish: str | None = None,
        mode: str | None = None,
    ) -> subprocess.Popen[str]:
        return self._spawn(
            name,
            [
                "-m",
                "llm_cli",
                "--profile",
                "test",
                "chat",
                "--plain",
                "--repo",
                str(self.repository),
                "--scope",
                "docs/",
                *(
                    ["--resume", resume]
                    if resume
                    else ["--provider", "process-test", "--model", name]
                ),
                *(["--mode", mode] if mode else []),
                *(["--publish", publish] if publish else []),
            ],
        )

    def submit_mode(
        self, process: subprocess.Popen[str], label: str, **kwargs: Any
    ) -> None:
        self.send(process, "ORCHESTRATION " + json.dumps({"label": label, **kwargs}))

    def mode(self, session_id: str) -> str:
        return self.rpc("session.show", session_id=session_id)["session"]["agent_mode"]


@pytest.fixture
def mode_cluster(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> Iterator[_ModeCluster]:
    repository = repository_factory(
        tmp_path, {"docs/a.md": "base a\n", "docs/b.md": "base b\n"}
    )
    with TemporaryDirectory(prefix="mfi-mode-", dir="/tmp") as runtime:
        cluster = _ModeCluster(tmp_path, repository, runtime)
        try:
            yield cluster
        finally:
            cluster.close()


def _git_state(cluster: _ModeCluster, git_run: Callable[..., str]) -> tuple[str, str]:
    return (
        git_run(cluster.repository, "show-ref"),
        git_run(cluster.repository, "write-tree"),
    )


def test_plan_mode_refuses_forged_mutators_and_checks(
    mode_cluster: _ModeCluster, git_run: Callable[..., str]
) -> None:
    cluster = mode_cluster
    before = _git_state(cluster, git_run)
    cluster.rpc("repo.add", path=str(cluster.repository))
    marker = cluster.root / "check-must-not-run"
    cluster.rpc(
        "checks.configure",
        path=str(cluster.repository),
        config={
            "checks": {
                "must-not-run": {
                    "argv": [
                        sys.executable,
                        "-c",
                        f"open({str(marker)!r}, 'w').write('ran')",
                    ],
                }
            },
        },
    )
    process = cluster.chat("plan", mode="plan")
    cluster.submit_mode(process, "plan", action="forged")
    task = cluster.settled("plan")
    assert task["state"] == "completed", task
    assert cluster.mode(task["session_id"]) == "plan"
    observed = json.loads((cluster.signals / "plan.observed.json").read_text())
    assert not _MUTATORS.intersection(observed["tools"])
    assert len(observed["results_1"]) == len(_MUTATORS)
    assert all(result["is_error"] for result in observed["results_1"])
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    assert not (cluster.repository / "docs/new-directory").exists()
    assert not (cluster.repository / "docs/moved.md").exists()
    assert not marker.exists()
    assert cluster.rpc("task.checks", task_id=task["task_id"]) == []
    assert cluster.rpc("task.diff", task_id=task["task_id"])["paths"] == []
    status = cluster.rpc("workspace.status", path=str(cluster.repository))
    assert status["workspace"]["workspace_revision"] == 0
    assert _git_state(cluster, git_run) == before


def test_default_chat_holds_normal_changes_until_cli_apply(
    mode_cluster: _ModeCluster, git_run: Callable[..., str]
) -> None:
    cluster = mode_cluster
    before = _git_state(cluster, git_run)
    process = cluster.chat("normal")
    cluster.submit_mode(process, "normal", content="reviewed result\n")
    task = cluster.settled("normal")
    assert task["state"] == "awaiting_review", task
    assert cluster.mode(task["session_id"]) == "normal"
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    assert (
        "+reviewed result" in cluster.rpc("task.diff", task_id=task["task_id"])["diff"]
    )
    cluster.send(process, "/apply")
    _wait(lambda: cluster.task("normal")["state"] == "completed")
    assert (cluster.repository / "docs/a.md").read_text() == "reviewed result\n"
    assert _git_state(cluster, git_run) == before


@pytest.mark.parametrize(
    ("mode", "publish", "expected_mode", "state"),
    [
        ("auto", None, "auto", "completed"),
        (None, "auto", "auto", "completed"),
        (None, "review", "normal", "awaiting_review"),
    ],
)
def test_explicit_auto_and_legacy_publication_flags(
    mode_cluster: _ModeCluster,
    mode: str | None,
    publish: str | None,
    expected_mode: str,
    state: str,
) -> None:
    cluster = mode_cluster
    process = cluster.chat("mode", mode=mode, publish=publish)
    cluster.submit_mode(process, "mode", content="mode result\n")
    task = cluster.settled("mode")
    assert task["state"] == state, task
    assert cluster.mode(task["session_id"]) == expected_mode
    assert (cluster.repository / "docs/a.md").read_text() == (
        "mode result\n" if expected_mode == "auto" else "base a\n"
    )


def test_plan_to_normal_keeps_conversation_and_resume_keeps_mode(
    mode_cluster: _ModeCluster,
) -> None:
    cluster = mode_cluster
    process = cluster.chat("planning", mode="plan")
    cluster.submit_mode(process, "planning", action="plan")
    task = cluster.settled("planning")
    assert task["state"] == "completed", task
    session_id = task["session_id"]
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    cluster.send(process, "/mode normal")
    _wait(lambda: cluster.mode(session_id) == "normal")
    cluster.submit_mode(process, "implement", content="implemented plan\n")
    assert cluster.settled("implement")["state"] == "awaiting_review"
    assert cluster.task("implement")["session_id"] == session_id
    observed = json.loads((cluster.signals / "implement.observed.json").read_text())
    assert observed["prior_plan"] == _PLAN
    assert "write_file" in observed["tools"]
    cluster.send(process, "/detach")
    assert process.wait(timeout=10) == 0
    resumed = cluster.chat("resumed", resume=session_id)
    cluster.send(resumed, "/mode")
    _wait(lambda: "normal" in (cluster.root / "resumed.log").read_text().lower())
    assert cluster.mode(session_id) == "normal"
    cluster.send(resumed, "/apply")
    _wait(lambda: cluster.task("implement")["state"] == "completed")
    assert (cluster.repository / "docs/a.md").read_text() == "implemented plan\n"
    cluster.send(resumed, "/mode auto")
    _wait(lambda: cluster.mode(session_id) == "auto")
    cluster.submit_mode(resumed, "automatic", content="automatic followup\n")
    assert cluster.settled("automatic")["state"] == "completed"
    assert (cluster.repository / "docs/a.md").read_text() == "automatic followup\n"


def test_mode_cannot_change_while_a_detached_task_is_running(
    mode_cluster: _ModeCluster,
) -> None:
    cluster = mode_cluster
    process = cluster.chat("busy", mode="auto")
    cluster.submit_mode(process, "busy", content="finished in auto\n", pause=True)
    cluster.ready("busy")
    task = cluster.task("busy")
    process.send_signal(signal.SIGINT)
    _wait(lambda: "Detached;" in (cluster.root / "busy.log").read_text())
    cluster.send(process, "/mode plan")
    _wait(lambda: "still running" in (cluster.root / "busy.log").read_text())
    assert cluster.mode(task["session_id"]) == "auto"
    cluster.release("busy")
    assert cluster.settled("busy")["state"] == "completed"
    assert (cluster.repository / "docs/a.md").read_text() == "finished in auto\n"


@pytest.mark.parametrize("mode", ["plan", "normal", "auto"])
def test_explicit_run_mode_uses_durable_shared_session(
    mode_cluster: _ModeCluster, git_run: Callable[..., str], mode: str
) -> None:
    cluster = mode_cluster
    cluster.rpc("repo.add", path=str(cluster.repository))
    before = _git_state(cluster, git_run)
    title = "ORCHESTRATION " + json.dumps(
        {
            "label": "run-mode",
            "action": "plan" if mode == "plan" else "write",
            "content": "one-shot mode result\n",
        }
    )
    process = cluster._spawn(
        "run-mode",
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
            "run-mode",
            "--mode",
            mode,
            "--follow",
        ],
    )
    assert process.wait(timeout=20) == 0, (cluster.root / "run-mode.log").read_text()
    task = cluster.task("run-mode")
    assert task["session_id"] is not None
    assert cluster.mode(task["session_id"]) == mode
    assert task["state"] == ("awaiting_review" if mode == "normal" else "completed")
    assert (cluster.repository / "docs/a.md").read_text() == (
        "one-shot mode result\n" if mode == "auto" else "base a\n"
    )
    if mode == "normal":
        assert (
            cluster.rpc("task.apply", task_id=task["task_id"])["state"] == "published"
        )
        assert (
            cluster.repository / "docs/a.md"
        ).read_text() == "one-shot mode result\n"
    assert _git_state(cluster, git_run) == before
