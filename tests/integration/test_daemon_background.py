from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from llm_cli.config.models import Settings
from llm_cli.coordination.models import ClaimRecord, RepositoryRecord, TaskRecord
from llm_cli.daemon.service import DaemonService
from llm_cli.execution.runner import FixtureWrite, FixtureWriteRunner
from llm_cli.paths import AppPaths
from llm_cli.protocol.envelopes import Request


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


def _request(method: str, params: dict[str, Any]) -> Request:
    return Request.create(
        request_id=f"request_{method.replace('.', '_')}",
        method=method,
        params=params,
        profile_id="test",
    )


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


@dataclass
class _PerTaskBlockingRunner:
    starts: dict[str, threading.Event]
    release: threading.Event

    def execute(
        self,
        *,
        repository: RepositoryRecord,
        task: TaskRecord,
        claim: ClaimRecord,
        writes: tuple[FixtureWrite, ...],
    ) -> None:
        del repository, claim, writes
        self.starts[task.task_id].set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("fixture runner was not released")


def test_run_schedules_background_work_and_exposes_events_to_other_sessions(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        paths = AppPaths.resolve(
            "test",
            environ={
                "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
                "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
                "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
                "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
            },
            home=tmp_path,
        )
        shutdown = asyncio.Event()
        service = DaemonService(
            paths, Settings(profile_id="test"), shutdown, boot_id="boot"
        )
        service.initialize()
        started = threading.Event()
        release = threading.Event()
        service.fixture_runner = cast(
            FixtureWriteRunner, _BlockingRunner(started, release)
        )
        await service.handle(_request("repo.add", {"path": str(repository)}))

        accepted = await service.handle(
            _request(
                "task.run",
                {
                    "title": "background fixture",
                    "path": str(repository),
                    "scopes": ["docs/"],
                    "task_id": "task-background",
                    "fixture_writes": ["docs/guide.md=hello"],
                },
            )
        )
        assert accepted["execution"] == "scheduled"
        assert await asyncio.to_thread(started.wait, 2)

        # This emulates another CLI session. It must not wait for the worker's
        # thread, which remains deliberately blocked below.
        events = await asyncio.wait_for(
            service.handle(_request("task.events", {"task_id": "task-background"})),
            timeout=0.2,
        )
        assert any(event["event_type"] == "execution.scheduled" for event in events)

        background = service._background_tasks[("task-background", 1)]
        release.set()
        await asyncio.wait_for(background, timeout=2)
        service.close()

    asyncio.run(scenario())


def test_blocked_model_does_not_starve_control_requests(
    tmp_path: Path,
    service_factory: Callable[[Path], DaemonService],
    repository_factory: Callable[..., Path],
) -> None:
    async def scenario() -> None:
        # Even a one-thread control pool must remain usable during model work.
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=1)
        )
        repository = repository_factory(tmp_path, {"README.md": "base\n"})
        service = service_factory(tmp_path)
        service.initialize()
        started, release = threading.Event(), threading.Event()
        service.fixture_runner = cast(
            FixtureWriteRunner, _BlockingRunner(started, release)
        )
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            await service.handle(
                _request(
                    "task.run",
                    {
                        "title": "blocked model",
                        "path": str(repository),
                        "scopes": ["docs/"],
                        "task_id": "blocked-model",
                        "fixture_writes": ["docs/guide.md=hello"],
                    },
                )
            )
            async with asyncio.timeout(2):
                while not started.is_set():
                    await asyncio.sleep(0.01)
            # Repository inspection uses the default pool under the RPC lock.
            await asyncio.wait_for(
                service.handle(_request("repo.add", {"path": str(repository)})),
                timeout=1,
            )
            assert not release.is_set()
        finally:
            release.set()
            await service.drain(timeout_ms=2000)
            service.close()

    asyncio.run(scenario())


def test_daemon_starts_a_queued_fixture_when_its_claim_activates(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        paths = AppPaths.resolve(
            "test",
            environ={
                "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
                "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
                "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
                "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
            },
            home=tmp_path,
        )
        service = DaemonService(
            paths, Settings(profile_id="test"), asyncio.Event(), boot_id="boot"
        )
        service.initialize()
        release = threading.Event()
        starts = {"task-first": threading.Event(), "task-second": threading.Event()}
        service.fixture_runner = cast(
            FixtureWriteRunner, _PerTaskBlockingRunner(starts, release)
        )
        await service.handle(_request("repo.add", {"path": str(repository)}))

        first = await service.handle(
            _request(
                "task.run",
                {
                    "title": "first fixture",
                    "path": str(repository),
                    "scopes": ["docs/"],
                    "task_id": "task-first",
                    "fixture_writes": ["docs/first.md=first"],
                },
            )
        )
        assert first["execution"] == "scheduled"
        assert await asyncio.to_thread(starts["task-first"].wait, 2)

        second = await service.handle(
            _request(
                "task.run",
                {
                    "title": "second fixture",
                    "path": str(repository),
                    "scopes": ["docs/"],
                    "task_id": "task-second",
                    "fixture_writes": ["docs/second.md=second"],
                },
            )
        )
        assert second["execution"] == "queued"

        # The release atomically activates the second claim. The daemon scans
        # the durable launch records in the same request and starts it; no
        # chat/session process re-submits task-second.
        await service.handle(
            _request(
                "claim.release",
                {"claim_id": first["claim"]["claim_id"], "reason": "test release"},
            )
        )
        assert await asyncio.to_thread(starts["task-second"].wait, 2)
        assert ("task-second", 1) in service._background_tasks
        first_background = service._background_tasks[("task-first", 1)]
        second_background = service._background_tasks[("task-second", 1)]

        release.set()
        await asyncio.wait_for(first_background, timeout=2)
        await asyncio.wait_for(second_background, timeout=2)
        service.close()

    asyncio.run(scenario())
