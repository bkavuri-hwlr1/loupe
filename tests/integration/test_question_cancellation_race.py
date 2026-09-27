"""Cancellation at the question-registration boundary must not strand a worker."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from test_shared_agent_execution import (
    ScriptedProvider,
    _assert_completed,
    _call,
    _finish,
    _open_session,
    _register,
    _request,
    _start,
    _tools,
)

from llm_cli.daemon.service import DaemonService


async def _until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    "timing", ["before_registration", "after_registration", "answer"]
)
def test_question_cancellation_does_not_strand_task_or_block_peer(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    monkeypatch: pytest.MonkeyPatch,
    timing: str,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path, {"a.txt": "original a\n", "b.txt": "original b\n"}
        )
        service = service_factory(tmp_path)
        questioner = ScriptedProvider(
            "questioner",
            [
                _tools(
                    _call("read_file", path="a.txt"),
                    _call("write_file", path="a.txt", content="proposed a\n"),
                ),
                _tools(_call("ask_user", question="Publish this change?")),
                _finish(),
            ],
        )
        peer = ScriptedProvider(
            "peer",
            [
                _tools(
                    _call("read_file", path="b.txt"),
                    _call("write_file", path="b.txt", content="peer b\n"),
                ),
                _finish(),
            ],
        )
        _register(service, questioner, peer)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open_session(service, repository, questioner)
        peer_credentials = await _open_session(service, repository, peer)
        entered, release = threading.Event(), threading.Event()
        original_ask = service._ask_operator

        def pause_before_registration(task_id: str, question: str) -> str:
            entered.set()
            assert release.wait(5), "test did not release the question boundary"
            return original_ask(task_id, question)

        if timing == "before_registration":
            monkeypatch.setattr(service, "_ask_operator", pause_before_registration)
        await service.handle(
            _request(
                "task.run",
                {
                    **credentials,
                    "task_id": "question-task",
                    "title": "edit and ask",
                    "path": str(repository),
                    "scopes": ["a.txt"],
                    "interactive": True,
                },
            )
        )
        worker = service._background_tasks[("question-task", 1)]
        try:
            if timing == "before_registration":
                await _until(entered.is_set)
                assert not service._task_question("question-task")["pending"]
            else:
                await _until(lambda: service._task_question("question-task")["pending"])
            # An unanswered question in one terminal leaves its peer usable.
            peer_worker = await _start(
                service, repository, peer_credentials, "peer-task", ("b.txt",)
            )
            await asyncio.wait_for(asyncio.shield(peer_worker), 5)
            _assert_completed(service, "peer-task")
            assert (repository / "b.txt").read_text() == "peer b\n"

            if timing == "answer":
                question = service._task_question("question-task")
                answer = await service.handle(
                    _request(
                        "task.answer",
                        {
                            "task_id": "question-task",
                            "question_id": question["question_id"],
                            "answer": "yes, publish",
                        },
                    )
                )
                assert answer["delivered"]
            else:
                cancelled = await service.handle(
                    _request("task.cancel", {"task_id": "question-task"})
                )
                assert cancelled["state"] == "stopping"
            release.set()
            done, _ = await asyncio.wait({worker}, timeout=1)
            assert worker in done, "cancellation lost the question-registration race"
            await worker
            assert not service._task_question("question-task")["pending"]
            if timing == "answer":
                _assert_completed(service, "question-task")
                assert (repository / "a.txt").read_text() == "proposed a\n"
                assert questioner.results[-1][0].content.startswith("yes, publish")
            else:
                assert service._task_view("question-task")["state"] == "cancelled"
                assert (repository / "a.txt").read_text() == "original a\n"
                proposal = service.workflow.inspect("question-task")
                assert proposal["status"] == "cancelled"
                assert "+proposed a" in proposal["diff"]
        finally:
            release.set()
            # Also clean up against the buggy implementation, without waiting
            # for the production operator-answer timeout of ten minutes.
            for _ in range(100):
                if worker.done():
                    break
                with service._questions_lock:
                    pending = service._questions.get("question-task")
                    if pending is not None:
                        pending.ready.set()
                await asyncio.sleep(0.01)
            await asyncio.wait_for(asyncio.shield(worker), 5)
            service.close()

    asyncio.run(scenario())
