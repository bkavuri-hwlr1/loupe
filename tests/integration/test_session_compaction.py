"""A session's saved conversation can be summarized between tasks."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.protocol.envelopes import Request
from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult

type Step = ModelTurn | Callable[[], ModelTurn]


class SummarizingProvider:
    name = "scripted"

    def __init__(self, model: str, steps: Sequence[Step]) -> None:
        self.model = model
        self.steps = list(steps)
        self.history: list[object] = []
        self.restored: list[object] = []
        self.summaries = 0

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> SummarizingProvider:
        history = state.get("messages", []) if state is not None else []
        assert isinstance(history, list)
        self.history = copy.deepcopy(history)
        self.restored = copy.deepcopy(history)
        return self

    def snapshot(self) -> Mapping[str, object]:
        return {"messages": copy.deepcopy(self.history)}

    def send_user(self, text: str) -> ModelTurn:
        assert self.steps, "unexpected model turn"
        step = self.steps.pop(0)
        turn = step() if callable(step) else step
        self.history.extend([text, turn.text])
        return turn

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        raise AssertionError("no tools expected")

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        raise AssertionError("no tools expected")

    def summarize(
        self, instruction: str, *, max_tool_text: int | None = None
    ) -> ModelTurn:
        self.summaries += 1
        return ModelTurn(text="SUMMARY of the first task")

    def replace_history(self, summary: str) -> None:
        self.history = [summary]


def _request(method: str, params: dict[str, Any]) -> Request:
    return Request.create(
        request_id=f"request_{method.replace('.', '_')}",
        method=method,
        params=params,
        profile_id="test",
    )


async def _open(
    service: DaemonService, repository: Path, provider: SummarizingProvider
) -> dict[str, str]:
    secret = "resume-secret-for-compaction"
    credentials = {"session_id": provider.model, "resume_secret": secret}
    opened = await service.handle(
        _request(
            "session.open",
            {
                "session_id": credentials["session_id"],
                "path": str(repository),
                "resume_token_hash": hashlib.sha256(secret.encode()).hexdigest(),
                "provider": provider.name,
                "model": provider.model,
                "workspace": "shared",
            },
        )
    )
    await service.handle(
        _request(
            "session.ack", {**credentials, "sequence": opened["bootstrap_sequence"]}
        )
    )
    return credentials


async def _run(
    service: DaemonService,
    repository: Path,
    credentials: dict[str, str],
    task_id: str,
) -> asyncio.Task[None]:
    await service.handle(
        _request(
            "task.run",
            {
                **credentials,
                "title": f"answer {task_id}",
                "path": str(repository),
                "scopes": ["*"],
                "task_id": task_id,
            },
        )
    )
    return service._background_tasks[(task_id, 1)]


def _service(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    provider: SummarizingProvider,
) -> tuple[DaemonService, Path]:
    repository = repository_factory(tmp_path, {"README.md": "# Example\n"})
    service = service_factory(tmp_path)
    service.providers.register("scripted", lambda model: provider)
    service.initialize()
    return service, repository


def test_compact_replaces_the_saved_conversation_used_by_the_next_task(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        provider = SummarizingProvider(
            "compact-session",
            [
                ModelTurn(text="first answer", context_tokens=150_000),
                ModelTurn(text="second answer", context_tokens=3_000),
            ],
        )
        service, repository = _service(
            tmp_path, repository_factory, service_factory, provider
        )
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open(service, repository, provider)

        empty = await service.handle(_request("session.compact", credentials))
        assert empty == {"compacted": False}

        await asyncio.wait_for(
            await _run(service, repository, credentials, "first"), timeout=20
        )
        saved = service.store.session_conversation(credentials["session_id"])
        assert saved is not None
        assert saved[2]["context_tokens"] == 150_000

        result = await service.handle(_request("session.compact", credentials))

        assert result["compacted"] is True
        assert result["context_tokens"] == 150_000
        assert 0 < result["summary_tokens"] < 150_000
        saved = service.store.session_conversation(credentials["session_id"])
        assert saved is not None
        messages = saved[2]["session"]["messages"]  # type: ignore[index]
        assert len(messages) == 1
        assert "SUMMARY of the first task" in messages[0]

        await asyncio.wait_for(
            await _run(service, repository, credentials, "second"), timeout=20
        )
        assert len(provider.restored) == 1
        assert "SUMMARY of the first task" in str(provider.restored[0])
        assert provider.summaries == 1
        service.close()

    asyncio.run(scenario())


def test_compact_is_refused_while_a_task_is_running(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_answer() -> ModelTurn:
        started.set()
        assert release.wait(20)
        return ModelTurn(text="answer")

    async def scenario() -> None:
        provider = SummarizingProvider("busy-session", [slow_answer])
        service, repository = _service(
            tmp_path, repository_factory, service_factory, provider
        )
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open(service, repository, provider)
        background = await _run(service, repository, credentials, "busy")
        await asyncio.to_thread(started.wait, 20)

        with pytest.raises(LlmCoordError) as failure:
            await service.handle(_request("session.compact", credentials))

        assert failure.value.code is ErrorCode.TASK_NOT_MUTABLE
        release.set()
        await asyncio.wait_for(background, timeout=20)
        assert provider.summaries == 0
        service.close()

    asyncio.run(scenario())


def test_usage_totals_task_runs_and_reports_the_context_size(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        provider = SummarizingProvider(
            "usage-session",
            [
                ModelTurn(
                    text="first answer",
                    usage={
                        "prompt_tokens": 1_000,
                        "cache_read_input_tokens": 400,
                        "output_tokens": 50,
                    },
                    context_tokens=1_050,
                ),
                ModelTurn(
                    text="second answer",
                    usage={"prompt_tokens": 1_200, "output_tokens": 70},
                    context_tokens=1_270,
                ),
            ],
        )
        provider.input_token_budget = 240_000  # type: ignore[attr-defined]
        service, repository = _service(
            tmp_path, repository_factory, service_factory, provider
        )
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open(service, repository, provider)

        empty = await service.handle(_request("session.usage", credentials))
        assert empty == {
            "task_runs": 0,
            "usage": {},
            "context_tokens": None,
            "context_budget": 240_000,
        }

        for task_id in ("first", "second"):
            await asyncio.wait_for(
                await _run(service, repository, credentials, task_id), timeout=20
            )
        result = await service.handle(_request("session.usage", credentials))

        assert result == {
            "task_runs": 2,
            "usage": {
                "prompt_tokens": 2_200,
                "cache_read_input_tokens": 400,
                "output_tokens": 120,
            },
            "context_tokens": 1_270,
            "context_budget": 240_000,
        }
        service.close()

    asyncio.run(scenario())


def test_usage_counts_a_failed_run_from_its_checkpoint(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        # The tool turn is checkpointed, then sending its results fails.
        provider = SummarizingProvider(
            "failed-usage-session",
            [
                ModelTurn(
                    text="",
                    tool_calls=(ToolCallRequest("list", "list_files", {"path": "."}),),
                    stop_reason="tool_use",
                    usage={"prompt_tokens": 900, "output_tokens": 30},
                    context_tokens=930,
                )
            ],
        )
        service, repository = _service(
            tmp_path, repository_factory, service_factory, provider
        )
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open(service, repository, provider)
        await asyncio.wait_for(
            await _run(service, repository, credentials, "fails"), timeout=20
        )
        task = service.store.get_task("fails")
        assert task is not None and task.state == "failed"

        result = await service.handle(_request("session.usage", credentials))

        assert result["task_runs"] == 1
        assert result["usage"] == {"prompt_tokens": 900, "output_tokens": 30}
        # A failed run's history is never promoted to the conversation.
        assert result["context_tokens"] is None
        service.close()

    asyncio.run(scenario())
