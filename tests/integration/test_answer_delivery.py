"""The accepted answer survives daemon restart and event-cursor reattachment."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from pathlib import Path

from test_shared_agent_execution import (
    ScriptedProvider,
    _assert_completed,
    _call,
    _open_session,
    _register,
    _request,
    _start,
    _tools,
)

from llm_cli.daemon.service import DaemonService


def test_finished_answer_is_replayed_once_after_restart_and_checkpoint_retry(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"README.md": "Repository.\n"})
        answer = "Loupe coordinates coding-agent sessions. 🦊\n" * 1_000
        provider = ScriptedProvider(
            "answer-model",
            [
                _tools(
                    _call(
                        "finish_task", answer=answer, summary="Answered the question."
                    )
                )
            ],
        )
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            credentials = await _open_session(service, repository, provider)
            running = await _start(
                service, repository, credentials, "task-durable-answer", ("*",)
            )
            await asyncio.wait_for(running, timeout=10)
            _assert_completed(service, "task-durable-answer")
            execution = service.store.get_execution("task-durable-answer", 1)
            assert execution is not None
            checkpoint = service.store.get_execution_checkpoint(execution.execution_id)
            assert checkpoint is not None and checkpoint.terminal_event_persisted
            first_stream = await service.handle(
                _request("task.attach", {"task_id": execution.task_id})
            )
            first = [event async for event in first_stream]
            parts = [
                event for event in first if event["event_type"] == "model.finished"
            ]
            assert len(parts) > 1
            assert "".join(part["payload"]["answer"] for part in parts) == answer
            assert sum(part["payload"]["final"] for part in parts) == 1
        finally:
            service.close()

        restarted = service_factory(tmp_path)
        restarted.initialize()
        try:
            # A retry of the accepted checkpoint must not append a second answer.
            restarted.store.save_execution_checkpoint(
                execution_id=execution.execution_id,
                driver=execution.driver,
                checkpoint=checkpoint.checkpoint,
            )
            replay_stream = await restarted.handle(
                _request("task.attach", {"task_id": execution.task_id})
            )
            replay = [event async for event in replay_stream]
            assert replay == first
            cutoff = parts[0]["sequence"]
            resumed_stream = await restarted.handle(
                _request("task.attach", {"task_id": execution.task_id, "after": cutoff})
            )
            resumed = [event async for event in resumed_stream]
            assert resumed == [event for event in first if event["sequence"] > cutoff]
            combined = [
                parts[0],
                *[
                    event
                    for event in resumed
                    if event["event_type"] == "model.finished"
                ],
            ]
            assert "".join(part["payload"]["answer"] for part in combined) == answer
            assert len({part["sequence"] for part in combined}) == len(combined)
        finally:
            restarted.close()

    asyncio.run(scenario())


def test_arbitrary_task_id_gets_a_safe_durable_answer_identity(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"README.md": "Repository.\n"})
        task_id = "task/雪/" + "x" * 200
        answer = "This answer survives an unrestricted caller-supplied task ID."
        provider = ScriptedProvider(
            "answer-model",
            [_tools(_call("finish_task", answer=answer, summary="Answered."))],
        )
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            credentials = await _open_session(service, repository, provider)
            running = await _start(service, repository, credentials, task_id, ("*",))
            await asyncio.wait_for(running, timeout=10)
            _assert_completed(service, task_id)

            execution = service.store.get_execution(task_id, 1)
            assert execution is not None
            accepted = service.store.get_execution_answer(execution.execution_id)
            assert accepted is not None
            assert accepted.payload["answer"] == answer
            answer_id = accepted.answer_id
            assert re.fullmatch(r"answer:[a-f0-9]{64}", answer_id)
            events = service.store.list_task_events(task_id, limit=500)
            assert {
                event.payload["message_id"]
                for event in events
                if event.event_type == "model.finished"
            } == {answer_id}
        finally:
            service.close()

    asyncio.run(scenario())
