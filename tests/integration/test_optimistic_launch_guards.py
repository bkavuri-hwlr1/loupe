"""Durable optimistic authority never falls back to an incompatible runner."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from test_optimistic_shared_sessions import _finish_after_release
from test_shared_agent_execution import (
    ScriptedProvider,
    _assert_completed,
    _call,
    _finish,
    _open_session,
    _register,
    _request,
    _tools,
)

from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.registry import DriverRegistry
from llm_cli.coordination.models import ClaimState
from llm_cli.daemon.service import DaemonService
from llm_cli.providers.base import ModelTurn, ToolCallResult


async def _blocker(service: DaemonService, repository: Path) -> str:
    held = await service.handle(
        _request(
            "task.run",
            {
                "title": "exclusive blocker",
                "path": str(repository),
                "scopes": ["docs/"],
                "task_id": "blocker",
                "claim_only": True,
            },
        )
    )
    assert held["claim"]["state"] == "active_work"
    return str(held["claim"]["claim_id"])


async def _release_and_drain(service: DaemonService, claim_id: str) -> None:
    await service.handle(
        _request("claim.release", {"claim_id": claim_id, "reason": "test release"})
    )
    await asyncio.wait_for(
        asyncio.gather(*list(service._background_tasks.values())), timeout=10
    )


@pytest.mark.parametrize(
    "capability", ["shared_workspace", "resumable", "enforces_scope"]
)
def test_driver_downgrade_cannot_move_an_optimistic_claim_to_another_runner(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    git_run: Callable[..., str],
    capability: str,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        service = service_factory(tmp_path)
        provider = ScriptedProvider("downgrade", [])
        _register(service, provider)
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            credentials = await _open_session(service, repository, provider)
            blocker = await _blocker(service, repository)
            queued = await service.handle(
                _request(
                    "task.run",
                    {
                        **credentials,
                        "title": "accepted before driver downgrade",
                        "path": str(repository),
                        "scopes": ["docs/"],
                        "task_id": "downgrade",
                    },
                )
            )
            assert queued["execution"] == "queued"
            assert queued["claim"]["scheduling_mode"] == "optimistic"
            refs = git_run(repository, "show-ref")
            worktrees = git_run(repository, "worktree", "list", "--porcelain")

            # Simulate a changed adapter registration after durable acceptance.
            # Registry consistency still holds, but the old claim must retain
            # its stricter execution requirements at worker dispatch.
            downgraded = CodingAgentHarness(provider)
            downgraded.capabilities = replace(
                CodingAgentHarness.capabilities, **{capability: False}
            )
            service.drivers = DriverRegistry()
            service.drivers.register(
                "coding_agent",
                capabilities=downgraded.capabilities,
                factory=lambda parameters: downgraded,
            )
            await _release_and_drain(service, blocker)
            task = service.store.get_task("downgrade")
            assert task is not None and task.state == "failed"
            claim = service.store.get_claim(queued["claim"]["claim_id"])
            assert claim is not None and claim.state is ClaimState.RELEASED
            assert claim.scheduling_mode == "optimistic"
            assert service.store.get_execution("downgrade", 1) is None
            assert not provider.history
            assert (repository / "docs/guide.md").read_text() == "base\n"
            assert git_run(repository, "show-ref") == refs
            assert git_run(repository, "worktree", "list", "--porcelain") == worktrees
        finally:
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("unavailable", ["missing", "import-error", "runtime-error"])
def test_missing_provider_retires_checkpoint_without_starving_a_healthy_launch(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    unavailable: str,
) -> None:
    class RemovedProvider(ScriptedProvider):
        name = "removed"

    async def scenario() -> None:
        repository = repository_factory(
            tmp_path,
            {"docs/shared.md": "base shared\n", "docs/healthy.md": "base healthy\n"},
        )
        dead = service_factory(tmp_path)

        def crash_after_staging(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert len(results) == 2 and all(not result.is_error for result in results)
            raise KeyboardInterrupt

        removed = RemovedProvider(
            "removed-model",
            [
                _tools(
                    _call("read_file", path="docs/shared.md"),
                    _call(
                        "write_file", path="docs/shared.md", content="private removed\n"
                    ),
                ),
                crash_after_staging,
            ],
        )
        healthy = ScriptedProvider(
            "healthy-model",
            [
                _tools(
                    _call("read_file", path="docs/healthy.md"),
                    _call(
                        "write_file", path="docs/healthy.md", content="healthy result\n"
                    ),
                ),
                _finish(),
            ],
        )
        dead.providers.register(removed.name, lambda model: removed)
        _register(dead, healthy)
        dead.initialize()
        try:
            await dead.handle(_request("repo.add", {"path": str(repository)}))
            registered = dead.store.list_repositories()[0]
            for provider in (removed, healthy):
                credentials = await _open_session(dead, repository, provider)
                task = dead.store.create_task(
                    repository_id=registered.repository_id,
                    session_id=credentials["session_id"],
                    task_id=provider.model,
                    title=f"resume {provider.model}",
                )
                dead.store.save_execution_launch(
                    task_id=task.task_id,
                    attempt=1,
                    driver="coding_agent",
                    instructions=task.title,
                    interactive=False,
                    parameters={"provider": provider.name, "model": provider.model},
                )
                claim = dead.coordinator.request_claim(
                    task.task_id,
                    ["docs/"],
                    scheduling_mode="optimistic",
                    optimistic_driver="coding_agent",
                )
                assert claim.state is ClaimState.ACTIVE_WORK
                if provider is removed:
                    with pytest.raises(KeyboardInterrupt):
                        dead._execute(
                            repository=registered,
                            task=task,
                            claim=claim,
                            driver=CodingAgentHarness(provider),
                            instructions=task.title,
                        )
            abandoned = dead.store.get_execution(removed.model, 1)
            assert abandoned is not None
            checkpoint = dead.store.get_execution_checkpoint(abandoned.execution_id)
            assert checkpoint is not None
            assert checkpoint.checkpoint["phase"] == "tool_results"
            assert dead.store.get_execution(healthy.model, 1) is None
        finally:
            dead.close()

        live = DaemonService(
            dead.paths, dead.settings, asyncio.Event(), boot_id="boot-without-provider"
        )
        _register(live, healthy)
        if unavailable != "missing":

            def unavailable_factory(model: str | None) -> RemovedProvider:
                del model
                if unavailable == "import-error":
                    raise ImportError("the provider's optional SDK is unavailable")
                raise RuntimeError("the provider could not initialize")

            live.providers.register(removed.name, unavailable_factory)
        try:
            live.initialize()
            assert live.startup_recovery is not None
            assert live.startup_recovery.summary == {"failed_safe": 1}
            background = live._background_tasks[(healthy.model, 1)]
            await asyncio.wait_for(background, timeout=10)
            _assert_completed(live, healthy.model)
            failed = live.store.get_task(removed.model)
            assert failed is not None and failed.state == "failed"
            failed_execution = live.store.get_execution(removed.model, 1)
            assert failed_execution is not None and failed_execution.state == "failed"
            failed_claim = live.store.get_claim(str(failed.current_claim_id))
            assert (
                failed_claim is not None and failed_claim.state is ClaimState.RELEASED
            )
            assert (
                live.store.get_execution_checkpoint(abandoned.execution_id)
                == checkpoint
            )
            assert (repository / "docs/shared.md").read_text() == "base shared\n"
            assert (repository / "docs/healthy.md").read_text() == "healthy result\n"
            assert healthy.history
            assert (await live.handle(_request("task.recover", {})))["outcomes"] == []
        finally:
            await asyncio.wait_for(
                asyncio.gather(
                    *list(live._background_tasks.values()), return_exceptions=True
                ),
                timeout=10,
            )
            live.close()

    asyncio.run(scenario())


def test_closed_session_queued_launch_fails_without_invoking_the_model(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        service = service_factory(tmp_path)
        provider = ScriptedProvider("closed-before-launch", [])
        _register(service, provider)
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            credentials = await _open_session(service, repository, provider)
            blocker = await _blocker(service, repository)
            queued = await service.handle(
                _request(
                    "task.run",
                    {
                        **credentials,
                        "title": "close before claim is granted",
                        "path": str(repository),
                        "scopes": ["docs/"],
                        "task_id": "closed",
                    },
                )
            )
            assert queued["execution"] == "queued"
            assert queued["claim"]["scheduling_mode"] == "optimistic"
            await service.handle(_request("session.close", credentials))
            await _release_and_drain(service, blocker)
            task = service.store.get_task("closed")
            assert task is not None and task.state == "failed"
            claim = service.store.get_claim(queued["claim"]["claim_id"])
            assert claim is not None and claim.state is ClaimState.RELEASED
            assert service.store.get_execution("closed", 1) is None
            assert not provider.history
            assert (repository / "docs/guide.md").read_text() == "base\n"
        finally:
            service.close()

    asyncio.run(scenario())


def test_existing_exclusive_claim_is_not_upgraded_when_its_launch_is_added(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        service = service_factory(tmp_path)
        staged = threading.Event()
        release = threading.Event()
        provider = ScriptedProvider(
            "existing-exclusive",
            [
                _tools(_call("read_file", path="docs/guide.md")),
                _finish_after_release(staged, release),
            ],
        )
        _register(service, provider)
        service.initialize()
        backgrounds: list[asyncio.Task[None]] = []
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            credentials = await _open_session(service, repository, provider)
            params = {
                **credentials,
                "title": "legacy reservation receives a launch",
                "path": str(repository),
                "scopes": ["docs/"],
                "task_id": "existing",
            }
            original = await service.handle(
                _request("task.run", {**params, "claim_only": True})
            )
            assert original["claim"]["scheduling_mode"] == "exclusive"
            started = await service.handle(_request("task.run", params))
            assert started["execution"] == "scheduled"
            backgrounds.append(service._background_tasks[("existing", 1)])
            assert await asyncio.to_thread(staged.wait, 5)
            assert started["claim"]["claim_id"] == original["claim"]["claim_id"]
            claim = service.store.get_claim(started["claim"]["claim_id"])
            assert claim is not None and claim.state is ClaimState.ACTIVE_WORK
            assert claim.scheduling_mode == "exclusive"
            assert claim.workspace_id is None
            release.set()
            await asyncio.wait_for(backgrounds[0], timeout=10)
            _assert_completed(service, "existing")
        finally:
            release.set()
            await asyncio.wait_for(
                asyncio.gather(*backgrounds, return_exceptions=True), timeout=10
            )
            service.close()

    asyncio.run(scenario())
