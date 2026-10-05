"""Shared tasks delegate read-only questions to exploration helpers."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from llm_cli.daemon.service import DaemonService
from llm_cli.protocol.envelopes import Request
from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult

_HELPER_PROMPT = "You are a read-only exploration helper"


class Session:
    def __init__(self, steps: list[ModelTurn], tools: tuple[str, ...]) -> None:
        self.steps = steps
        self.tools = tools
        self.history: list[object] = []
        self.results: list[tuple[ToolCallResult, ...]] = []

    def snapshot(self) -> Mapping[str, object]:
        return {"messages": copy.deepcopy(self.history)}

    def _next(self) -> ModelTurn:
        assert self.steps, "the harness requested an unexpected model turn"
        return self.steps.pop(0)

    def send_user(self, text: str) -> ModelTurn:
        self.history.append({"role": "user", "content": text})
        return self._next()

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        self.results.append(tuple(results))
        self.history.append({"role": "tool", "results": len(results)})
        return self._next()

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        self.history.append({"role": "tool", "results": len(results)})


class Provider:
    name = "scripted"

    def __init__(
        self, model: str, main: list[ModelTurn], helper: list[ModelTurn]
    ) -> None:
        self.model = model
        self.main = Session(main, ())
        self.helper = Session(helper, ())

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> Session:
        session = self.helper if system.startswith(_HELPER_PROMPT) else self.main
        session.tools = tuple(str(tool["name"]) for tool in tools)
        return session


def _tools(*calls: ToolCallRequest) -> ModelTurn:
    return ModelTurn(text="", tool_calls=calls, stop_reason="tool_use")


def _request(method: str, params: dict[str, Any]) -> Request:
    return Request.create(
        request_id=f"request_{method.replace('.', '_')}",
        method=method,
        params=params,
        profile_id="test",
    )


async def _run(
    service: DaemonService, repository: Path, provider: Provider, task_id: str
) -> None:
    secret = "resume-secret-for-explore"
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
                "mode": "normal",
            },
        )
    )
    await service.handle(
        _request(
            "session.ack", {**credentials, "sequence": opened["bootstrap_sequence"]}
        )
    )
    await service.handle(
        _request(
            "task.run",
            {
                **credentials,
                "title": "explain the guide",
                "path": str(repository),
                "scopes": ["docs/"],
                "task_id": task_id,
            },
        )
    )
    await asyncio.wait_for(service._background_tasks[(task_id, 1)], timeout=30)


def test_helper_sees_pending_edits_and_reports_through_the_daemon(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    task = "What does docs/guide.md say now?"
    provider = Provider(
        "explore-session",
        [
            _tools(
                ToolCallRequest("read", "read_file", {"path": "docs/guide.md"}),
                ToolCallRequest(
                    "write",
                    "write_file",
                    {"path": "docs/guide.md", "content": "edited\n"},
                ),
            ),
            _tools(ToolCallRequest("ask", "explore", {"task": task})),
            ModelTurn(text="The helper confirmed the edit."),
        ],
        [
            _tools(ToolCallRequest("look", "read_file", {"path": "docs/guide.md"})),
            ModelTurn(text="docs/guide.md:1 reads 'edited'."),
        ],
    )

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "original\n"})
        service = service_factory(tmp_path)
        service.providers.register("scripted", lambda model: provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        await _run(service, repository, provider, "explore")

        assert "explore" in provider.main.tools
        assert provider.helper.tools == (
            "list_files",
            "read_file",
            "search_text",
            "read_diff",
        )
        assert provider.helper.results[0][0].content == "edited\n"
        (report,) = provider.main.results[1]
        assert not report.is_error
        assert "docs/guide.md:1 reads 'edited'." in report.content
        # Normal mode holds the edit; the helper changed nothing on disk.
        assert (repository / "docs/guide.md").read_text() == "original\n"
        events = service.store.list_task_events("explore", limit=500)
        kinds = [event.event_type for event in events]
        assert "explore.started" in kinds
        finished = next(e.payload for e in events if e.event_type == "explore.finished")
        assert finished["state"] == "completed"
        assert finished["tool_calls"] == 1
        service.close()

    asyncio.run(scenario())


def test_explore_can_be_turned_off_in_settings(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    provider = Provider("no-explore-session", [ModelTurn(text="answer")], [])

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "original\n"})
        service = service_factory(tmp_path)
        service.settings = replace(service.settings, agent_explore=False)
        service.providers.register("scripted", lambda model: provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        await _run(service, repository, provider, "no-explore")

        assert provider.main.tools
        assert "explore" not in provider.main.tools
        service.close()

    asyncio.run(scenario())
