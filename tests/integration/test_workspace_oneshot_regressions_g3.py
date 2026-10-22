"""One-shot allocation/replay privacy and stable same-session exclusion."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest
from test_shared_agent_execution import ScriptedProvider
from test_workspace_cutover_acceptance import _cutover, _request, _service

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.providers.base import ModelTurn


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"title": 3},
        {"title": ""},
        {"title": "valid", "task_id": 42},
        {"title": "valid", "task_id": ""},
        {"title": "valid", "fixture_writes": {}},
    ],
    ids=[
        "missing-title",
        "nonstring-title",
        "empty-title",
        "nonstring-id",
        "empty-id",
        "falsey-invalid-fixture-writes",
    ],
)
def test_refused_one_shot_request_creates_no_session_or_environment(
    invalid: dict[str, Any],
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"README.md": "base\n"})
        service = _service(tmp_path, "session")
        service.providers.register(
            "scripted",
            lambda _model: ScriptedProvider("workspace-model", []),
        )
        service.initialize()
        try:
            await service.handle(_request("repo.add", path=str(repository)))
            await _cutover(service, repository)
            params = {
                "path": str(repository),
                "scopes": ["README.md"],
                "command_environment": {"PROJECT_FLAG": "ephemeral-only"},
                **invalid,
            }
            with pytest.raises(LlmCoordError) as refused:
                await service.handle(_request("task.run", **params))
            assert refused.value.code is ErrorCode.CONFIG_INVALID
            with service.store.connection() as connection:
                assert (
                    connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                    == 0
                )
                assert (
                    connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
                )
            assert service.session_environments._sessions == {}
        finally:
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("include_environment", [False, True])
def test_completed_one_shot_replay_does_not_require_or_recapture_environment(
    include_environment: bool,
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"README.md": "base\n"})
        service = _service(tmp_path, "session")
        provider = ScriptedProvider(
            "workspace-model", [ModelTurn(text="Read-only answer.")]
        )
        service.providers.register("scripted", lambda _model: provider)
        service.initialize()
        try:
            await service.handle(_request("repo.add", path=str(repository)))
            await _cutover(service, repository)
            params: dict[str, Any] = {
                "path": str(repository),
                "task_id": "replay-task",
                "title": "explain",
                "scopes": ["README.md"],
                "command_environment": {"PROJECT_FLAG": "original ephemeral map"},
            }
            await service.handle(_request("task.run", **params))
            await asyncio.wait_for(
                asyncio.shield(service._background_tasks[("replay-task", 1)]),
                30,
            )
            task = service.store.get_task("replay-task")
            assert task is not None and task.session_id is not None
            assert service.store.get_session(task.session_id).state == "closed"
            assert (
                service.session_environments.plan(task.session_id)["state"]
                == "awaiting_session_environment"
            )
            if include_environment:
                params["command_environment"] = {
                    "PROJECT_FLAG": "must not be recaptured"
                }
            else:
                params.pop("command_environment")
            result = await service.handle(_request("task.run", **params))
            assert result["claim"] is None and result["execution"] == "published"
            assert service.store.get_session(task.session_id).state == "closed"
            assert (
                service.session_environments.plan(task.session_id)["state"]
                == "awaiting_session_environment"
            )
            assert not provider.steps
            other = repository_factory(
                tmp_path / "other-checkout", {"README.md": "another checkout\n"}
            )
            await service.handle(_request("repo.add", path=str(other)))
            plan = await service.handle(
                _request("workspace.cutover.plan", path=str(other))
            )
            await service.handle(
                _request(
                    "workspace.cutover.apply",
                    path=str(other),
                    checkpoint=plan["checkpoint"],
                    intent_id="another-checkout-cutover",
                    dispositions={},
                )
            )
            with pytest.raises(LlmCoordError) as wrong_checkout:
                await service.handle(
                    _request("task.run", **{**params, "path": str(other)})
                )
            assert wrong_checkout.value.code is ErrorCode.CONFIG_INVALID
            assert service.session_environments._sessions == {}
            with service.store.connection() as connection:
                assert (
                    connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                    == 1
                )
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM workspace_executions"
                    ).fetchone()[0]
                    == 1
                )
        finally:
            service.close()

    asyncio.run(scenario())


def test_interactive_session_busy_refusal_includes_a_live_claim_free_execution(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
) -> None:
    ready, release = threading.Event(), threading.Event()

    class BlockedProvider(ScriptedProvider):
        def send_user(self, text: str) -> ModelTurn:
            ready.set()
            assert release.wait(15), "test must release the scripted model"
            return super().send_user(text)

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"README.md": "base\n"})
        service = _service(tmp_path)
        provider = BlockedProvider(
            "workspace-model", [ModelTurn(text="Read-only answer.")]
        )
        service.providers.register("scripted", lambda _model: provider)
        service.initialize()
        worker: asyncio.Task[None] | None = None
        try:
            await service.handle(_request("repo.add", path=str(repository)))
            await _cutover(service, repository)
            secret = "shared-session-resume-fixture"
            opened = await service.handle(
                _request(
                    "session.open",
                    path=str(repository),
                    session_id="shared-session",
                    resume_token_hash=hashlib.sha256(secret.encode()).hexdigest(),
                    provider="scripted",
                    model="workspace-model",
                    workspace="shared",
                    mode="auto",
                )
            )
            await service.handle(
                _request(
                    "session.ack",
                    session_id="shared-session",
                    resume_secret=secret,
                    sequence=opened["bootstrap_sequence"],
                )
            )
            params = {
                "path": str(repository),
                "session_id": "shared-session",
                "resume_secret": secret,
                "scopes": ["README.md"],
            }
            await service.handle(
                _request(
                    "task.run",
                    **params,
                    task_id="first-workspace-task",
                    title="first",
                )
            )
            worker = service._background_tasks[("first-workspace-task", 1)]
            assert await asyncio.to_thread(ready.wait, 10)
            with pytest.raises(LlmCoordError) as busy:
                await service.handle(
                    _request(
                        "task.run",
                        **params,
                        task_id="second-workspace-task",
                        title="second",
                    )
                )
            assert busy.value.code is ErrorCode.TASK_NOT_MUTABLE
            assert service.store.get_task("second-workspace-task") is None
            assert not worker.done()
        finally:
            release.set()
            if worker is not None:
                await asyncio.wait_for(asyncio.shield(worker), 30)
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["cancel", "close"])
@pytest.mark.parametrize("one_shot", [False, True])
def test_preparing_workspace_environment_waiter_can_be_cancelled_or_closed(
    action: str,
    one_shot: bool,
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"README.md": "base\n"})
        service = _service(tmp_path, "session")
        provider = ScriptedProvider("workspace-model", [])
        service.providers.register("scripted", lambda _model: provider)
        service.initialize()
        try:
            await service.handle(_request("repo.add", path=str(repository)))
            await _cutover(service, repository)
            secret = "preparing-session-resume-fixture"
            opened = await service.handle(
                _request(
                    "session.open",
                    path=str(repository),
                    session_id="preparing-session",
                    resume_token_hash=hashlib.sha256(secret.encode()).hexdigest(),
                    provider="scripted",
                    model="workspace-model",
                    workspace="shared",
                    mode="auto",
                    command_environment={"PROJECT_FLAG": "opening-terminal"},
                )
            )
            await service.handle(
                _request(
                    "session.ack",
                    session_id="preparing-session",
                    resume_secret=secret,
                    sequence=opened["bootstrap_sequence"],
                )
            )
            session = service.store.get_session("preparing-session")
            assert session is not None and session.workspace_id is not None
            checkout = service.store.get_checkout(session.checkout_id)
            workspace = service.store.get_workspace(session.workspace_id)
            assert checkout is not None and workspace is not None
            task = service.store.create_task(
                repository_id=checkout.repository_id,
                session_id=session.session_id,
                title="durably prepared before interrupted launch",
                task_id="preparing-workspace-task",
            )
            service.store.save_execution_launch(
                task_id=task.task_id,
                attempt=task.attempt,
                driver="coding_agent",
                instructions=task.title,
                interactive=False,
                parameters={
                    "provider": "scripted",
                    "model": "workspace-model",
                    **({"one_shot_session": True} if one_shot else {}),
                },
            )
            regime = service.physical_authority.regime(repository)
            execution = service.store.create_workspace_execution(
                task_id=task.task_id,
                attempt=task.attempt,
                session_id=session.session_id,
                workspace_id=workspace.workspace_id,
                scopes=("README.md",),
                physical_key=regime["physical_key"],
                regime_generation=regime["generation"],
                driver="coding_agent",
                worktree_path=workspace.canonical_path,
                base_oid=(
                    f"workspace:{workspace.workspace_epoch}:"
                    f"{workspace.workspace_revision}"
                ),
                boot_id=service.boot_id,
            )
            service.workflow.ensure(task.task_id)
            # A daemon restart loses the opening terminal's in-memory map,
            # after durable preparation and before any checkpoint/worker.
            service.session_environments.discard(session.session_id)
            service._schedule_ready_launches()
            assert service._task_view(task.task_id)["state"] == (
                "awaiting_session_environment"
            )
            assert (
                service.store.get_execution_checkpoint(execution.execution_id) is None
            )
            assert (task.task_id, task.attempt) not in service._background_tasks
            if action == "cancel":
                await service.handle(_request("task.cancel", task_id=task.task_id))
            else:
                await service.handle(
                    _request(
                        "session.close",
                        session_id=session.session_id,
                        resume_secret=secret,
                    )
                )
                assert service.store.get_session(session.session_id).state == "closed"
            service._schedule_ready_launches()
            service._schedule_ready_launches()
            settled = service.store.get_execution_by_id(execution.execution_id)
            assert settled is not None and settled.state == "failed"
            assert settled.failure_code == "CANCELLED"
            assert service._task_view(task.task_id)["state"] == "cancelled"
            assert service.store.get_task(task.task_id).coordination_state == "released"
            assert service.workflow.get(task.task_id)["status"] == "cancelled"
            assert task.task_id not in service._environment_waiters
            assert (task.task_id, task.attempt) not in service._background_tasks
            assert service.store.get_session(session.session_id).state == (
                "closed" if one_shot or action == "close" else "active"
            )
            assert not provider.steps
        finally:
            service.close()

    asyncio.run(scenario())


def test_cancelled_async_waiter_does_not_duplicate_or_close_the_actual_one_shot_worker(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready, release = threading.Event(), threading.Event()
    provider_calls: list[str] = []

    class BlockedProvider(ScriptedProvider):
        def send_user(self, text: str) -> ModelTurn:
            provider_calls.append(text)
            ready.set()
            assert release.wait(20), "test must release the actual scripted worker"
            return super().send_user(text)

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"README.md": "base\n"})
        service = _service(tmp_path, "session")
        provider = BlockedProvider(
            "workspace-model", [ModelTurn(text="Read-only answer.")]
        )
        service.providers.register("scripted", lambda _model: provider)
        submitted: list[Future[Any]] = []
        actual_submit = service._execution_pool.submit

        def track_submit(
            fn: Callable[..., Any], *args: Any, **kwargs: Any
        ) -> Future[Any]:
            future = actual_submit(fn, *args, **kwargs)
            submitted.append(future)
            return future

        monkeypatch.setattr(service._execution_pool, "submit", track_submit)
        service.initialize()
        waiter: asyncio.Task[None] | None = None
        try:
            await service.handle(_request("repo.add", path=str(repository)))
            await _cutover(service, repository)
            task_id = "actual-worker-one-shot"
            await service.handle(
                _request(
                    "task.run",
                    path=str(repository),
                    task_id=task_id,
                    title="explain while the caller disconnects",
                    scopes=["README.md"],
                    command_environment={"PROJECT_FLAG": "opening terminal only"},
                )
            )
            key = (task_id, 1)
            waiter = service._background_tasks[key]
            assert await asyncio.to_thread(ready.wait, 10)
            assert len(submitted) == 1 and submitted[0].running()
            task = service.store.get_task(task_id)
            assert task is not None and task.session_id is not None
            before = service.store.get_execution(task_id, 1)
            assert before is not None and before.state == "running"
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            # Forgetting the asyncio waiter must not make a live worker look
            # runnable again, even after repeated durable scheduler scans.
            service._schedule_ready_launches()
            service._schedule_ready_launches()
            await asyncio.sleep(0)
            assert key not in service._background_tasks
            assert len(submitted) == 1 and submitted[0].running()
            assert len(provider_calls) == 1
            assert service.store.get_execution(task_id, 1).state == "running"
            assert service.store.get_session(task.session_id).state == "active"
            assert (
                service.session_environments.plan(task.session_id)["state"] == "ready"
            )
            with service.store.connection() as connection:
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM workspace_executions WHERE task_id=?",
                        (task_id,),
                    ).fetchone()[0]
                    == 1
                )
            release.set()
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(submitted[0])), 30
            )
            assert submitted[0].done() and not submitted[0].cancelled()
            assert service.store.get_execution(task_id, 1).state == "published"
            assert service.store.get_task(task_id).state == "completed"
            assert service.store.get_session(task.session_id).state == "closed"
            assert service.session_environments.plan(task.session_id)["state"] == (
                "awaiting_session_environment"
            )
            assert len(provider_calls) == 1
            assert not provider.steps
        finally:
            release.set()
            if waiter is not None and not waiter.done():
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
            for future in submitted:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), 30)
            service.close()

    asyncio.run(scenario())
