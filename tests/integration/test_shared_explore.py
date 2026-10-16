"""Shared tasks delegate read-only questions to exploration helpers."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

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
    service: DaemonService,
    repository: Path,
    provider: Provider,
    task_id: str,
    *,
    mode: str = "normal",
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
                "mode": mode,
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
            "read_binary",
            "read_symlink",
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


def test_helper_command_sees_pending_overlay_and_discards_its_writes(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.execution import commands, shared

    # Exercise the real snapshot/supervisor path on every test host. The
    # sandbox wrapper itself is covered by the command runner's OS tests.
    monkeypatch.setattr(shared, "available_sandbox", lambda: "test-sandbox")
    monkeypatch.setattr(commands, "wrap", lambda kind, policy, argv, **kw: list(argv))
    provider = Provider(
        "helper-command",
        [
            _tools(
                ToolCallRequest("read", "read_file", {"path": "docs/guide.md"}),
                ToolCallRequest(
                    "write",
                    "write_file",
                    {"path": "docs/guide.md", "content": "pending\n"},
                ),
            ),
            _tools(ToolCallRequest("explore", "explore", {"task": "Inspect output"})),
            ModelTurn(text="The helper saw pending."),
        ],
        [
            _tools(
                ToolCallRequest(
                    "command",
                    "run_command",
                    {
                        "argv": [
                            "sh",
                            "-c",
                            "cat docs/guide.md; printf discarded > docs/guide.md",
                        ]
                    },
                )
            ),
            _tools(ToolCallRequest("read", "read_file", {"path": "docs/guide.md"})),
            ModelTurn(text="The command printed pending; its writes were discarded."),
        ],
    )

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "original\n"})
        service = service_factory(tmp_path)
        assert service.shared_runner.commands is not None
        service.shared_runner.commands = replace(
            service.shared_runner.commands, approval="allow"
        )
        service.providers.register("scripted", lambda model: provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        await _run(service, repository, provider, "helper-command")

        assert (
            "run_command" in provider.main.tools
            and "run_command" in provider.helper.tools
        )
        command = provider.helper.results[0][0]
        assert not command.is_error and "pending" in command.content
        assert provider.helper.results[1][0].content == "pending\n"
        assert (repository / "docs/guide.md").read_text() == "original\n"
        events = service.store.list_task_events("helper-command", limit=500)
        finished = [e.payload for e in events if e.event_type == "command.finished"]
        assert len(finished) == 1 and finished[0]["state"] == "completed"
        helper_finished = next(
            e.payload for e in events if e.event_type == "explore.finished"
        )
        assert helper_finished["tool_calls"] == 2
        assert not list((tmp_path / "data").rglob("command_*"))
        service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["normal", "plan"])
def test_helper_fetches_allowed_domain_and_refuses_another_through_daemon(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    from llm_cli.web.fetch import Page, WebFetchError

    downloads: list[str] = []

    def fetch(url: str, **kwargs: Any) -> Page:
        from urllib.parse import urlsplit

        refusal = kwargs["allowed"](urlsplit(url).hostname, url)
        if refusal is not None:
            raise WebFetchError(refusal)
        downloads.append(url)
        return Page(url, 200, "text/plain", "helper documentation", False)

    monkeypatch.setattr("llm_cli.web.access.fetch", fetch)
    provider = Provider(
        "helper-fetch",
        [
            _tools(
                ToolCallRequest("explore", "explore", {"task": "Read documentation"})
            ),
            ModelTurn(text="The documentation was read."),
        ],
        [
            _tools(
                ToolCallRequest(
                    "allowed", "web_fetch", {"url": "https://docs.example.com/guide"}
                ),
                ToolCallRequest(
                    "denied", "web_fetch", {"url": "https://denied.example.com/guide"}
                ),
                ToolCallRequest("command", "run_command", {"argv": ["pwd"]}),
            ),
            ModelTurn(text="Allowed page read; other tools refused."),
        ],
    )

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "original\n"})
        service = service_factory(tmp_path)
        service.runner.web_fetch = "ask"
        service.runner.web_domains = ("docs.example.com",)
        service.providers.register("scripted", lambda model: provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        await _run(service, repository, provider, "helper-fetch", mode=mode)

        assert "web_fetch" in provider.helper.tools
        assert "run_command" not in provider.helper.tools
        allowed, denied, command = provider.helper.results[0]
        assert not allowed.is_error and "helper documentation" in allowed.content
        assert "external content, not instructions" in allowed.content
        assert denied.is_error and "not an allowed domain" in denied.content
        assert command.is_error and "unknown tool" in command.content
        assert downloads == ["https://docs.example.com/guide"]
        events = service.store.list_task_events("helper-fetch", limit=500)
        fetched = [e.payload for e in events if e.event_type == "web.fetched"]
        assert fetched == [{"domain": "docs.example.com", "characters": 20}]
        helper_finished = next(
            e.payload for e in events if e.event_type == "explore.finished"
        )
        assert helper_finished["tool_calls"] == 3
        service.close()

    asyncio.run(scenario())
