"""Daemon-level recovery: boot resolution, on-demand repair, and shutdown drain."""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from llm_cli.agent.driver import DriverCapabilities, RunRequest, RunResult
from llm_cli.config.models import Settings
from llm_cli.coordination.models import (
    ClaimRecord,
    ClaimState,
    RepositoryRecord,
    TaskRecord,
)
from llm_cli.daemon.service import DaemonService
from llm_cli.execution import runner as runner_module
from llm_cli.execution.runner import (
    FixtureWrite,
    FixtureWriteRunner,
    parse_fixture_writes,
)
from llm_cli.paths import AppPaths
from llm_cli.protocol.envelopes import Request
from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult


def _git(path: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        },
    )


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "base",
    )
    return repository


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths.resolve(
        "test",
        environ={
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home=tmp_path,
    )


def _service(tmp_path: Path, boot_id: str) -> DaemonService:
    return DaemonService(
        _paths(tmp_path), Settings(profile_id="test"), asyncio.Event(), boot_id=boot_id
    )


def _request(method: str, params: dict[str, Any]) -> Request:
    return Request.create(
        request_id=f"request_{method.replace('.', '_')}",
        method=method,
        params=params,
        profile_id="test",
    )


def _abandon_an_execution(service: DaemonService, repository: Path) -> tuple[str, str]:
    """Leave one durable execution exactly as a killed worker would leave it."""

    registered = service.store.list_repositories()[0]
    task = service.store.create_task(
        repository_id=registered.repository_id,
        task_id="task-abandoned",
        title="abandoned",
    )
    claim = service.coordinator.request_claim(task.task_id, ["docs/"])
    with pytest.raises(KeyboardInterrupt):
        service.fixture_runner.execute(
            repository=registered,
            task=task,
            claim=claim,
            writes=parse_fixture_writes(["docs/guide.md=interrupted\n"]),
        )
    return task.task_id, claim.claim_id


def test_startup_resolves_executions_a_previous_boot_abandoned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        dead = _service(tmp_path, "boot-dead")
        dead.initialize()
        await dead.handle(_request("repo.add", {"path": str(repository)}))

        def die(*args: object, **kwargs: object) -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr(runner_module, "create_result_commit", die)
        task_id, claim_id = _abandon_an_execution(dead, repository)
        assert dead.store.get_claim(claim_id).state is ClaimState.ACTIVE_WORK  # type: ignore[union-attr]
        dead.close()

        live = _service(tmp_path, "boot-live")
        live.initialize()

        assert live.startup_recovery is not None
        assert live.startup_recovery.summary == {"released": 1}
        assert live.store.get_claim(claim_id).state is ClaimState.RELEASED  # type: ignore[union-attr]
        execution = live.store.get_execution(task_id, 1)
        assert execution is not None
        assert execution.state == "failed"

        # A session that only pings still learns that the boot repaired work.
        ping = await live.handle(_request("system.ping", {}))
        assert ping["startup_recovery"]["summary"] == {"released": 1}
        live.close()

    asyncio.run(scenario())


def test_task_recover_repairs_intents_left_undecidable_at_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        dead = _service(tmp_path, "boot-dead")
        dead.initialize()
        await dead.handle(_request("repo.add", {"path": str(repository)}))

        def die(*args: object, **kwargs: object) -> object:
            raise KeyboardInterrupt

        monkeypatch.setattr(runner_module, "publish_task_ref", die)
        task_id, claim_id = _abandon_an_execution(dead, repository)
        dead.close()

        # The repository is unreachable at boot, so the outcome cannot be read
        # and the reservation must stay blocking rather than be guessed at.
        moved = tmp_path / "moved-away"
        repository.rename(moved)
        live = _service(tmp_path, "boot-live")
        live.initialize()
        assert live.startup_recovery is not None
        assert live.startup_recovery.summary == {"operator_attention": 1}
        assert live.store.get_claim(claim_id).state is ClaimState.PUBLISHING  # type: ignore[union-attr]

        # Once the operator restores it, rerunning recovery reads the reference
        # and finds it exactly where the intent expected it, which is proof the
        # swap never happened.
        moved.rename(repository)

        payload = await live.handle(_request("task.recover", {}))

        assert payload["summary"] == {"failed_safe": 1}
        assert payload["blocking_task_ids"] == []
        assert live.store.get_claim(claim_id).state is ClaimState.RELEASED  # type: ignore[union-attr]
        settled = live.store.latest_publication_intent(task_id, 1)
        assert settled is not None
        assert settled.operation_state == "failed_safe"
        live.close()

    asyncio.run(scenario())


@dataclass
class _BlockingRunner:
    started: threading.Event
    release: threading.Event

    def execute(
        self,
        *,
        repository: RepositoryRecord,
        task: TaskRecord,
        claim: ClaimRecord,
        writes: tuple[FixtureWrite, ...],
    ) -> None:
        del repository, task, claim, writes
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("fixture runner was not released")


class _CheckpointingCrashDriver:
    """Leave valid harness state exactly as a killed worker would."""

    name = "coding_agent"
    capabilities = DriverCapabilities(
        tool_calling=True,
        resumable=True,
        enforces_scope=True,
    )

    def run(self, request: RunRequest, tools: object) -> RunResult:
        del tools
        assert request.checkpoint is not None
        request.checkpoint(
            {
                "version": 1,
                "provider": "anthropic",
                "model": "resumption-fixture",
                "session": {"messages": []},
                "deadline_at": 4_102_444_800.0,
                "phase": "opening",
                "turn": None,
                "tool_results": [],
                "in_flight_call": None,
                "tool_usage": {
                    "calls": 0,
                    "files_read": 0,
                    "bytes_read": 0,
                    "writes": 0,
                    "denied": 0,
                    "finished": False,
                    "summary": "",
                },
                "usage_total": {},
                "idle_turns": 0,
                "final_summary": None,
            }
        )
        raise KeyboardInterrupt


@dataclass
class _ResumeBlockingProvider:
    started: threading.Event
    release: threading.Event
    restored_state: Mapping[str, object] | None = None
    name: str = "anthropic"
    model: str = "resumption-fixture"

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> _ResumeBlockingProvider:
        del system, tools
        self.restored_state = state
        return self

    def snapshot(self) -> Mapping[str, object]:
        return {"messages": []}

    def send_user(self, text: str) -> ModelTurn:
        del text
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("resumed model was not released")
        return ModelTurn(
            text="",
            tool_calls=(
                ToolCallRequest(
                    call_id="finish",
                    name="finish_task",
                    arguments={"answer": "done", "summary": "done"},
                ),
            ),
            stop_reason="tool_use",
        )

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        del results
        return ModelTurn(text="done", stop_reason="end_turn")

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        del results


def test_shutdown_waits_for_a_worker_and_reports_what_outlives_it(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path, "boot-drain")
        service.initialize()
        started = threading.Event()
        release = threading.Event()
        service.fixture_runner = cast(
            FixtureWriteRunner, _BlockingRunner(started, release)
        )
        await service.handle(_request("repo.add", {"path": str(repository)}))
        await service.handle(
            _request(
                "task.run",
                {
                    "title": "slow fixture",
                    "path": str(repository),
                    "scopes": ["docs/"],
                    "task_id": "task-draining",
                    "fixture_writes": ["docs/guide.md=slow"],
                },
            )
        )
        assert await asyncio.to_thread(started.wait, 2)

        # A worker that will not finish inside the budget is reported, not
        # cancelled: cancelling the awaiting task would not stop its thread.
        abandoned = await service.drain(timeout_ms=50)
        assert abandoned == (("task-draining", 1),)
        events = await service.handle(
            _request("task.events", {"task_id": "task-draining"})
        )
        assert any(event["event_type"] == "execution.abandoned" for event in events)

        # A worker that does finish inside the budget drains cleanly.
        release.set()
        assert await service.drain(timeout_ms=5_000) == ()
        service.close()

    asyncio.run(scenario())


def test_startup_resumes_a_checkpointed_coding_agent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        dead = _service(tmp_path, "boot-dead")
        dead.initialize()
        await dead.handle(_request("repo.add", {"path": str(repository)}))
        registered = dead.store.list_repositories()[0]
        task = dead.store.create_task(
            repository_id=registered.repository_id,
            task_id="task-resume",
            title="resume me",
        )
        claim = dead.coordinator.request_claim(task.task_id, ["docs/"])
        dead.store.save_execution_launch(
            task_id=task.task_id,
            attempt=task.attempt,
            driver="coding_agent",
            instructions=task.title,
            interactive=False,
            parameters={"provider": "anthropic", "model": None},
        )
        with pytest.raises(KeyboardInterrupt):
            dead.runner.execute(
                repository=registered,
                task=task,
                claim=claim,
                driver=_CheckpointingCrashDriver(),
            )
        abandoned = dead.store.get_execution(task.task_id, task.attempt)
        assert abandoned is not None and abandoned.state == "running"
        assert dead.store.get_execution_checkpoint(abandoned.execution_id) is not None
        dead.close()

        started = threading.Event()
        release = threading.Event()
        provider = _ResumeBlockingProvider(started, release)
        live = _service(tmp_path, "boot-live")
        live.providers.replace("anthropic", lambda _model: provider)
        live.initialize()

        assert live.startup_recovery is not None
        assert live.startup_recovery.summary == {"resuming": 1}
        assert await asyncio.to_thread(started.wait, 2)
        assert provider.restored_state == {"messages": []}
        assert ("task-resume", 1) in live._background_tasks

        release.set()
        background = live._background_tasks[("task-resume", 1)]
        await asyncio.wait_for(background, timeout=2)
        # The scripted resumed turn intentionally leaves no changed files, so
        # normal trusted validation releases the work claim after proving that
        # the runner really did continue the saved harness execution.
        assert live.store.get_claim(claim.claim_id).state is ClaimState.RELEASED  # type: ignore[union-attr]
        live.close()

    asyncio.run(scenario())
