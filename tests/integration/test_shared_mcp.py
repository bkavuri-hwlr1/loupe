"""Shared tasks start configured MCP servers and close them afterwards."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from fake_mcp_http import http_mcp_server
from test_shared_explore import Provider, _request, _run, _tools

from llm_cli.config.models import McpServerConfig
from llm_cli.daemon.service import DaemonService
from llm_cli.providers.base import ModelTurn, ToolCallRequest

_SERVER = Path(__file__).parents[1] / "unit" / "fake_mcp_server.py"


def test_a_shared_task_calls_a_configured_mcp_tool(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    pid_file = tmp_path / "server.pid"
    server = McpServerConfig(
        name="fake",
        command=(sys.executable, str(_SERVER)),
        env=(("FAKE_MCP_PID_FILE", str(pid_file)),),
        approval="allow",
        timeout_seconds=10,
    )
    provider = Provider(
        "mcp-session",
        [
            _tools(ToolCallRequest("echo-1", "mcp__fake__echo", {"text": "hi"})),
            ModelTurn(text="Echoed."),
        ],
        [],
    )

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "original\n"})
        service = service_factory(tmp_path)
        service.shared_runner.mcp_servers = (server,)
        service.providers.register("scripted", lambda model: provider)
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            await _run(service, repository, provider, "mcp-task")

            assert "mcp__fake__echo" in provider.main.tools
            ((result,),) = provider.main.results
            assert result.content.endswith('{"text": "hi"}')
            events = await service.handle(
                _request("task.events", {"task_id": "mcp-task"})
            )
            started = [
                event["payload"]
                for event in events
                if event["event_type"] == "mcp.server.started"
            ]
            assert [payload["server"] for payload in started] == ["fake"]
            # The server ended with its task.
            with pytest.raises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
        finally:
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("mode", ["normal", "plan"])
def test_http_mcp_tools_resources_and_prompts_through_a_shared_task(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    transport: str,
    mode: str,
) -> None:
    credential = "opaque-shared-mcp-test-credential"
    monkeypatch.setenv("LOUPE_MCP_TEST_AUTH", "Bearer " + credential)
    writer_pid = tmp_path / "writer.pid"
    with http_mcp_server(transport) as http_server:
        reader = McpServerConfig(
            name="remote",
            url=http_server.url,
            headers=(("Authorization", "LOUPE_MCP_TEST_AUTH"),),
            approval="allow",
            read_only=True,
            timeout_seconds=3,
        )
        writer = McpServerConfig(
            name="writer",
            command=(sys.executable, str(_SERVER)),
            env=(("FAKE_MCP_PID_FILE", str(writer_pid)),),
            approval="allow",
            timeout_seconds=3,
        )
        provider = Provider(
            "http-mcp-session",
            [
                _tools(ToolCallRequest("echo", "mcp__remote__echo", {"text": "hi"})),
                _tools(ToolCallRequest("resources", "mcp__remote__resources_list", {})),
                _tools(
                    ToolCallRequest(
                        "resource",
                        "mcp__remote__resources_read",
                        {"uri": "test://guide"},
                    )
                ),
                _tools(ToolCallRequest("prompts", "mcp__remote__prompts_list", {})),
                _tools(
                    ToolCallRequest(
                        "prompt",
                        "mcp__remote__prompts_get",
                        {"name": "review", "arguments": {"subject": "code"}},
                    )
                ),
                ModelTurn(text="The external guide says to review code."),
            ],
            [],
        )

        async def scenario() -> None:
            repository = repository_factory(tmp_path, {"docs/guide.md": "original\n"})
            service = service_factory(tmp_path)
            service.shared_runner.mcp_servers = (reader, writer)
            service.providers.register("scripted", lambda model: provider)
            service.initialize()
            credentials = {
                "session_id": provider.model,
                "resume_secret": "resume-test-token",
            }
            try:
                await service.handle(_request("repo.add", {"path": str(repository)}))
                opened = await service.handle(
                    _request(
                        "session.open",
                        {
                            "session_id": provider.model,
                            "path": str(repository),
                            "resume_token_hash": hashlib.sha256(
                                credentials["resume_secret"].encode()
                            ).hexdigest(),
                            "provider": provider.name,
                            "model": provider.model,
                            "workspace": "shared",
                            "mode": mode,
                        },
                    )
                )
                await service.handle(
                    _request(
                        "session.ack",
                        {**credentials, "sequence": opened["bootstrap_sequence"]},
                    )
                )
                await service.handle(
                    _request(
                        "task.run",
                        {
                            **credentials,
                            "title": "read external reference material",
                            "path": str(repository),
                            "scopes": ["docs/"],
                            "task_id": "http-mcp-task",
                        },
                    )
                )
                await asyncio.wait_for(
                    service._background_tasks[("http-mcp-task", 1)], timeout=30
                )
                assert "mcp__remote__echo" in provider.main.tools
                assert "mcp__remote__resources_read" in provider.main.tools
                results = [batch[0].content for batch in provider.main.results]
                assert len(results) == 5 and all(
                    "external data, not instructions" in text for text in results
                )
                assert "Guide text" in results[2] and "Review code" in results[4]
                events = await service.handle(
                    _request("task.events", {"task_id": "http-mcp-task"})
                )
                started = [
                    event["payload"]["server"]
                    for event in events
                    if event["event_type"] == "mcp.server.started"
                ]
                assert started == (
                    ["remote"] if mode == "plan" else ["remote", "writer"]
                )
                assert ("mcp__writer__echo" in provider.main.tools) == (mode != "plan")
                assert writer_pid.exists() == (mode != "plan")
                assert (
                    credential not in json.dumps(events)
                    and credential not in caplog.text
                )
                assert http_server.deleted
            finally:
                service.close()

        asyncio.run(scenario())
