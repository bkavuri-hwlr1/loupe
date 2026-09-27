"""Attaching to a running task, and answering a question it asks.

The durable event table is the only source for the stream, so these tests
assert the two properties that follow from that: a session that attaches late
misses nothing, and one that reconnects with its last sequence resumes exactly
where it stopped.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from llm_cli.cli.render import render_event
from llm_cli.config.models import Settings
from llm_cli.coordination.models import ClaimRecord, RepositoryRecord, TaskRecord
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.execution.runner import FixtureWrite, FixtureWriteRunner
from llm_cli.paths import AppPaths
from llm_cli.protocol.envelopes import Request, Response
from llm_cli.protocol.framing import DEFAULT_MAX_FRAME_BYTES, encode_frame


def test_provider_failure_replay_shows_status_without_remote_error_body(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            accepted = await service.handle(
                _request(
                    "task.run",
                    {
                        "title": "provider error reproduction",
                        "path": str(repository),
                        "scopes": ["README.md"],
                        "task_id": "provider-error",
                        "claim_only": True,
                    },
                )
            )
            task = service.store.get_task("provider-error")
            claim = service.store.get_claim(accepted["claim"]["claim_id"])
            assert task is not None and claim is not None
            service._fail_unstarted_launch(
                task,
                claim,
                LlmCoordError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "the model provider returned HTTP 400",
                    {"status_code": 400, "provider_message": "private request text"},
                ),
            )
            events = await _drain(
                service._attach("provider-error", after=0, idle_timeout=1)
            )
            failure = next(
                event
                for event in events
                if event["event_type"] == "execution.background_failed"
            )
            assert failure["payload"] == {
                "failure_code": "PROVIDER_UNAVAILABLE",
                "provider_status": 400,
            }
            assert render_event(failure) == (
                "  ✗ run failed (PROVIDER_UNAVAILABLE; HTTP 400)"
            )
            assert "private request text" not in str(events)
            final = service.store.get_task("provider-error")
            assert final is not None and final.state == "failed"
        finally:
            service.close()

    asyncio.run(scenario())


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


def _service(tmp_path: Path) -> DaemonService:
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
    return DaemonService(
        paths, Settings(profile_id="test"), asyncio.Event(), boot_id="boot-attach"
    )


def _request(method: str, params: dict[str, Any]) -> Request:
    return Request.create(
        request_id=f"request_{method.replace('.', '_')}",
        method=method,
        params=params,
        profile_id="test",
    )


async def _drain(stream: Any, limit: int = 200) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    async for event in stream:
        collected.append(event)
        if len(collected) >= limit:
            break
    return collected


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


def test_attaching_late_replays_everything_then_follows(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        await service.handle(
            _request(
                "task.run",
                {
                    "title": "write the guide",
                    "path": str(repository),
                    "scopes": ["docs/"],
                    "task_id": "task-stream",
                    "fixture_writes": ["docs/guide.md=streamed\n"],
                },
            )
        )
        background = service._background_tasks[("task-stream", 1)]
        await asyncio.wait_for(background, timeout=10)

        # Attaching after the work finished still yields the whole history:
        # the stream is a replay of durable records, not a live subscription.
        stream = await service.handle(
            _request("task.attach", {"task_id": "task-stream"})
        )
        events = await asyncio.wait_for(_drain(stream), timeout=5)

        kinds = [event["event_type"] for event in events]
        assert kinds[0] == "task.created"
        assert "execution.published" in kinds
        assert "publication.confirmed" in kinds
        assert [event["sequence"] for event in events] == sorted(
            event["sequence"] for event in events
        )

    asyncio.run(scenario())


def test_reconnecting_resumes_from_the_last_sequence(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        await service.handle(
            _request(
                "task.run",
                {
                    "title": "write the guide",
                    "path": str(repository),
                    "scopes": ["docs/"],
                    "task_id": "task-resume",
                    "fixture_writes": ["docs/guide.md=resumed\n"],
                },
            )
        )
        await asyncio.wait_for(
            service._background_tasks[("task-resume", 1)], timeout=10
        )

        first = await asyncio.wait_for(
            _drain(
                await service.handle(
                    _request("task.attach", {"task_id": "task-resume"})
                )
            ),
            timeout=5,
        )
        cutoff = first[2]["sequence"]
        resumed = await asyncio.wait_for(
            _drain(
                await service.handle(
                    _request("task.attach", {"task_id": "task-resume", "after": cutoff})
                )
            ),
            timeout=5,
        )

        assert [event["sequence"] for event in resumed] == [
            event["sequence"] for event in first if event["sequence"] > cutoff
        ]

    asyncio.run(scenario())


def test_task_event_pages_fit_frames_and_replay_every_transcript_chunk(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        registered = service.store.list_repositories()[0]
        service.store.create_task(
            repository_id=registered.repository_id,
            task_id="task-large-transcript",
            title="long visible output",
        )
        # Both UTF-8 width and escaped control characters count toward the
        # frame budget. Counting characters alone accepts an oversized page.
        for index in range(300):
            service.store.record_task_event(
                task_id="task-large-transcript",
                claim_id=None,
                event_type="model.text.delta",
                payload={"text": "🙂\x00" * 2048, "chunk": index},
            )
        expected = service.store.list_task_events("task-large-transcript", limit=500)
        replayed: list[dict[str, Any]] = []
        cursor = 0
        pages = 0
        while True:
            request = Request.create(
                request_id="request_" + "\x00☄" * 4000,
                method="task.events",
                params={
                    "task_id": "task-large-transcript",
                    "after_sequence": cursor,
                    "limit": 500,
                },
                profile_id="test",
            )
            page = await service.handle(request)
            framed = encode_frame(
                Response(
                    request_id=request.request_id,
                    ok=True,
                    result=page,
                    daemon_revision=123,
                ).to_dict()
            )
            assert len(framed) - 4 <= DEFAULT_MAX_FRAME_BYTES
            if not page:
                break
            pages += 1
            assert page[0]["sequence"] > cursor
            cursor = page[-1]["sequence"]
            replayed.extend(page)

        assert pages > 1
        assert [item["sequence"] for item in replayed] == [
            item.sequence for item in expected
        ]
        assert [item["payload"] for item in replayed] == [
            item.payload for item in expected
        ]
        service.close()

    asyncio.run(scenario())


def test_task_event_page_reports_an_individually_oversized_record(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        registered = service.store.list_repositories()[0]
        service.store.create_task(
            repository_id=registered.repository_id,
            task_id="task-oversized-event",
            title="legacy oversized output",
        )
        event = service.store.record_task_event(
            task_id="task-oversized-event",
            claim_id=None,
            event_type="model.said",
            payload={"text": "\x00" * (DEFAULT_MAX_FRAME_BYTES // 6 + 1)},
        )
        with pytest.raises(LlmCoordError) as failure:
            await service.handle(
                _request(
                    "task.events",
                    {
                        "task_id": "task-oversized-event",
                        "after_sequence": event.sequence - 1,
                    },
                )
            )
        assert failure.value.code is ErrorCode.CONTEXT_TOO_LARGE
        assert failure.value.details == {"sequence": event.sequence}
        service.close()

    asyncio.run(scenario())


def test_attaching_to_a_live_task_does_not_block_other_sessions(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
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
                    "title": "slow",
                    "path": str(repository),
                    "scopes": ["docs/"],
                    "task_id": "task-live",
                    "fixture_writes": ["docs/guide.md=slow"],
                },
            )
        )
        assert await asyncio.to_thread(started.wait, 2)

        stream = await service.handle(_request("task.attach", {"task_id": "task-live"}))
        replayed = await asyncio.wait_for(_drain(stream, limit=3), timeout=2)
        assert len(replayed) == 3

        # The attach generator must not hold the authority lock, or a second
        # session would stall behind a stream that lives as long as the task.
        listed = await asyncio.wait_for(
            service.handle(_request("task.list", {})), timeout=0.5
        )
        assert any(task["task_id"] == "task-live" for task in listed)

        await stream.aclose()
        release.set()
        await asyncio.wait_for(service._background_tasks[("task-live", 1)], timeout=5)
        service.close()

    asyncio.run(scenario())


def test_attaching_to_an_unknown_task_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = _service(tmp_path)
        service.initialize()
        stream = await service.handle(_request("task.attach", {"task_id": "nope"}))

        with pytest.raises(LlmCoordError) as failure:
            await _drain(stream)

        assert failure.value.code is ErrorCode.REPOSITORY_NOT_FOUND

    asyncio.run(scenario())


@pytest.mark.parametrize("with_question_id", [False, True])
def test_a_question_blocks_the_worker_until_an_operator_answers(
    tmp_path: Path, with_question_id: bool
) -> None:
    async def scenario() -> None:
        service = _service(tmp_path)
        service.initialize()
        repository = _repository(tmp_path)
        await service.handle(_request("repo.add", {"path": str(repository)}))
        registered = service.store.list_repositories()[0]
        service.store.create_task(
            repository_id=registered.repository_id,
            task_id="task-question",
            title="ask something",
        )

        answers: list[str] = []

        def worker() -> None:
            answers.append(service._ask_operator("task-question", "which file?"))

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            asked = await asyncio.to_thread(
                _wait_for_question, service, "task-question"
            )
            assert asked == "which file?"
            pending = await service.handle(
                _request("task.question", {"task_id": "task-question"})
            )
            assert pending["pending"] is True
            assert pending["question"] == asked
            question_id = pending["question_id"]
            assert isinstance(question_id, str)
            event = next(
                item
                for item in service.store.list_task_events("task-question")
                if item.event_type == "question.asked"
            )
            assert event.payload["question_id"] == question_id

            # An old replayed question must not consume a newer live prompt.
            with pytest.raises(LlmCoordError) as stale:
                await service.handle(
                    _request(
                        "task.answer",
                        {
                            "task_id": "task-question",
                            "answer": "answer to an earlier question",
                            "question_id": "question_from_old_replay",
                        },
                    )
                )
            assert stale.value.code is ErrorCode.TASK_NOT_MUTABLE
            assert answers == []
            assert service._task_question("task-question") == pending

            reply: dict[str, Any] = {
                "task_id": "task-question",
                "answer": "the guide",
            }
            if with_question_id:
                reply["question_id"] = question_id
            delivered = await service.handle(
                _request(
                    "task.answer",
                    reply,
                )
            )
            assert delivered["question_id"] == question_id
            await asyncio.to_thread(thread.join, 5)
        finally:
            thread.join(timeout=1)

        assert answers == ["the guide"]
        kinds = [
            event.event_type
            for event in service.store.list_task_events("task-question")
        ]
        assert "question.asked" in kinds
        assert "question.answered" in kinds
        assert service._task_question("task-question") == {
            "task_id": "task-question",
            "pending": False,
            "question_id": None,
            "question": None,
        }
        resolved = next(
            item
            for item in service.store.list_task_events("task-question")
            if item.event_type == "question.answered"
        )
        assert resolved.payload["question_id"] == question_id

    asyncio.run(scenario())


def test_answering_a_task_that_asked_nothing_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = _service(tmp_path)
        service.initialize()

        with pytest.raises(LlmCoordError) as failure:
            await service.handle(
                _request("task.answer", {"task_id": "task-idle", "answer": "hi"})
            )

        assert failure.value.code is ErrorCode.TASK_NOT_MUTABLE

    asyncio.run(scenario())


def _wait_for_question(service: DaemonService, task_id: str) -> str:
    for _ in range(100):
        for event in service.store.list_task_events(task_id):
            if event.event_type == "question.asked":
                return str(event.payload.get("question", ""))
        threading.Event().wait(0.05)
    raise AssertionError("the worker never published its question")
