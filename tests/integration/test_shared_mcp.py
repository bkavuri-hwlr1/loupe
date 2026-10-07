"""Shared tasks start configured MCP servers and close them afterwards."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
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
