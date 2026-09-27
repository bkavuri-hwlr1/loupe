"""Session modes are durable policies, frozen independently for each task."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from test_shared_agent_execution import (
    ScriptedProvider,
    _call,
    _finish,
    _open_session,
    _register,
    _request,
    _start,
    _tools,
)

from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.providers.base import ModelTurn, ToolCallResult


async def _mode(
    service: DaemonService, credentials: dict[str, str], mode: str
) -> dict[str, Any]:
    return await service.handle(
        _request("session.set_mode", {**credentials, "mode": mode})
    )


@pytest.mark.parametrize("mode", ["plan", "normal", "auto"])
def test_modes_enforce_checkout_effects_and_freeze_task_policy(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    mode: str,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.txt": "original\n"})
        service = service_factory(tmp_path)
        provider = ScriptedProvider(
            "writer",
            [
                _tools(
                    _call("read_file", path="a.txt"),
                    _call("write_file", path="a.txt", content="proposed\n"),
                ),
                _finish("plan or edit complete"),
            ],
        )
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials = await _open_session(service, root, provider)
        changed = await _mode(service, credentials, mode)
        assert changed["session"]["agent_mode"] == mode
        if mode == "plan":
            await service.handle(
                _request(
                    "checks.configure",
                    {
                        "path": str(root),
                        "config": {
                            "checks": {
                                "must-not-run": {
                                    "argv": [sys.executable, "-c", "exit(1)"]
                                }
                            }
                        },
                    },
                )
            )
        worker = await _start(service, root, credentials, "mode-task", ("a.txt",))
        await asyncio.wait_for(worker, 5)
        task = service._task_view("mode-task")
        workflow = service.workflow.get("mode-task")
        launch = service.store.get_execution_launch("mode-task", 1)
        assert workflow and workflow["agent_mode"] == mode
        assert launch and launch.parameters["agent_mode"] == mode
        assert task["agent_mode"] == mode
        assert task["state"] == ("awaiting_review" if mode == "normal" else "completed")
        assert (root / "a.txt").read_text() == (
            "proposed\n" if mode == "auto" else "original\n"
        )
        if mode == "plan":
            assert "write_file" not in provider.offered_tools
            assert "run_check" not in provider.offered_tools
            assert provider.results[0][1].is_error
            assert service.workflow.checks("mode-task") == []
            assert service.workflow.inspect("mode-task")["paths"] == []
        elif mode == "normal":
            assert "+proposed" in service.workflow.inspect("mode-task")["diff"]
        # The settled task keeps its original policy after the next mode switch.
        await _mode(service, credentials, "auto" if mode != "auto" else "plan")
        assert service.workflow.ensure("mode-task")["agent_mode"] == mode
        if mode == "normal":
            result = await service.handle(
                _request("task.apply", {"task_id": "mode-task"})
            )
            assert result["state"] == "published"
            assert (root / "a.txt").read_text() == "proposed\n"

    asyncio.run(scenario())


def test_mode_change_is_refused_while_active_without_changing_frozen_policy(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.txt": "original\n"})
        service = service_factory(tmp_path)
        ready, release = threading.Event(), threading.Event()

        def wait(results: Sequence[ToolCallResult]) -> ModelTurn:
            ready.set()
            assert release.wait(5)
            return _finish()

        provider = ScriptedProvider(
            "busy", [_tools(_call("read_file", path="a.txt")), wait]
        )
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials = await _open_session(service, root, provider)
        worker = await _start(service, root, credentials, "busy-task", ("a.txt",))
        try:
            assert await asyncio.to_thread(ready.wait, 5)
            with pytest.raises(LlmCoordError, match="before changing mode") as caught:
                await _mode(service, credentials, "plan")
            assert caught.value.code is ErrorCode.TASK_NOT_MUTABLE
            assert (
                service.store.get_session(credentials["session_id"]).agent_mode
                == "auto"
            )
            assert service.workflow.get("busy-task")["agent_mode"] == "auto"
        finally:
            release.set()
            await worker
        assert (await _mode(service, credentials, "plan"))["session"][
            "agent_mode"
        ] == "plan"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("policy", "expected"),
    [({}, "auto"), ({"publish": "review"}, "normal"), ({"mode": "plan"}, "plan")],
)
def test_open_modes_are_durable_and_legacy_defaults_are_preserved(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    policy: dict[str, str],
    expected: str,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.txt": "original\n"})
        service = service_factory(tmp_path)
        provider = ScriptedProvider("mode", [])
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        opened = await service.handle(
            _request(
                "session.open",
                {
                    "session_id": "mode",
                    "path": str(root),
                    "provider": "scripted",
                    "model": "mode",
                    "resume_token_hash": hashlib.sha256(b"secret").hexdigest(),
                    **policy,
                },
            )
        )
        credentials = {"session_id": "mode", "resume_secret": "secret"}
        await service.handle(
            _request(
                "session.ack",
                {
                    **credentials,
                    "sequence": opened["bootstrap_sequence"],
                },
            )
        )
        assert opened["session"]["agent_mode"] == expected
        service.store.disconnect_active_sessions()
        resumed = await service.handle(_request("session.resume", credentials))
        assert resumed["session"]["agent_mode"] == expected
        shown = await service.handle(_request("session.show", {"session_id": "mode"}))
        assert shown["session"]["agent_mode"] == expected

    asyncio.run(scenario())


def test_normal_finished_conversation_survives_hold_and_later_apply(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.txt": "original\n"})
        service = service_factory(tmp_path)
        provider = ScriptedProvider(
            "normal",
            [
                _tools(
                    _call("read_file", path="a.txt"),
                    _call("write_file", path="a.txt", content="proposed\n"),
                ),
                _finish("prepared the agreed change"),
            ],
        )
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials = await _open_session(service, root, provider)
        await _mode(service, credentials, "normal")
        await (await _start(service, root, credentials, "first", ("a.txt",)))
        saved = service.store.session_conversation("normal")
        assert saved is not None
        before = service.store.get_session("normal").conversation_revision
        provider.steps.extend([_tools(_call("read_file", path="a.txt")), _finish()])
        await (await _start(service, root, credentials, "second", ("a.txt",)))
        assert provider.restored_state == saved[2]["session"]
        assert provider.results[-1][0].content.startswith("original\n")
        assert service.store.get_session("normal").conversation_revision == before + 1
        await service.handle(_request("task.apply", {"task_id": "first"}))
        assert service.store.get_session("normal").conversation_revision == before + 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "params", [{"mode": "auto"}, {"fixture_writes": ["a.txt=bad"]}]
)
def test_plan_session_rejects_policy_bypasses_before_creating_task(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    params: dict[str, Any],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.txt": "original\n"})
        service = service_factory(tmp_path)
        provider = ScriptedProvider("plan", [])
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials = await _open_session(service, root, provider)
        await _mode(service, credentials, "plan")
        with pytest.raises(LlmCoordError):
            await service.handle(
                _request(
                    "task.run",
                    {
                        **credentials,
                        "task_id": "refused",
                        "title": "do not edit",
                        "path": str(root),
                        "scopes": ["a.txt"],
                        **params,
                    },
                )
            )
        assert service.store.get_task("refused") is None
        assert (root / "a.txt").read_text() == "original\n"

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["normal", "auto"])
def test_workflow_reconstruction_uses_frozen_launch_not_current_session_mode(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    mode: str,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.txt": "original\n"})
        service = service_factory(tmp_path)
        provider = ScriptedProvider("frozen", [_finish()])
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials = await _open_session(service, root, provider)
        await _mode(service, credentials, mode)
        await (await _start(service, root, credentials, "frozen", ("a.txt",)))
        await _mode(service, credentials, "auto" if mode == "normal" else "normal")
        # Reconstruct the policy from its durable launch as after an interrupted
        # workflow insert. The mutable session is deliberately on another mode.
        with service.store.connection() as connection:
            connection.execute("DELETE FROM task_workflows WHERE task_id='frozen'")
        reconstructed = service.workflow.ensure("frozen")
        assert reconstructed["agent_mode"] == mode
        assert reconstructed["publish_mode"] == (
            "review" if mode == "normal" else "auto"
        )

    asyncio.run(scenario())
