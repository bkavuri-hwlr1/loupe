"""Shared tasks run user hooks in a sandbox around the agent's edits."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from test_shared_explore import Provider, _request, _run, _tools

from llm_cli.config.models import HookConfig
from llm_cli.daemon.service import DaemonService
from llm_cli.execution.sandbox import available_sandbox
from llm_cli.providers.base import ModelTurn, ToolCallRequest

pytestmark = pytest.mark.skipif(
    available_sandbox() is None,
    reason="no working operating-system sandbox on this machine",
)
GUARD = (
    'grep -q \'"path": "docs/secret.md"\' "$LOUPE_HOOK_INPUT" '
    "&& { echo docs/secret.md is protected; exit 2; }; exit 0"
)
UPPERCASE = 'for f; do tr a-z A-Z < "$f" > "$f.t" && mv "$f.t" "$f"; done'


def test_hooks_block_a_write_and_rewrite_an_edit_for_review(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    hooks = (
        HookConfig("pre_tool", ("write_file",), ("sh", "-c", GUARD), 30),
        HookConfig(
            "post_edit", ("*.md",), ("sh", "-c", UPPERCASE, "hook", "{paths}"), 30
        ),
    )
    provider = Provider(
        "hooks-session",
        [
            _tools(ToolCallRequest("read", "read_file", {"path": "docs/guide.md"})),
            _tools(
                ToolCallRequest(
                    "edit",
                    "write_file",
                    {"path": "docs/guide.md", "content": "draft\n"},
                ),
                ToolCallRequest(
                    "secret",
                    "write_file",
                    {"path": "docs/secret.md", "content": "nope\n"},
                ),
            ),
            ModelTurn(text="Edited the guide."),
        ],
        [],
    )

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "original\n"})
        service = service_factory(tmp_path)
        service.shared_runner.hooks = hooks
        service.providers.register("scripted", lambda model: provider)
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            await _run(service, repository, provider, "hooks-task")

            _, (edited, blocked) = provider.main.results
            assert edited.content.endswith("read it again before patching it]")
            assert blocked.is_error
            assert "docs/secret.md is protected" in blocked.content
            diff = await service.handle(
                _request("task.diff", {"task_id": "hooks-task"})
            )
            assert diff["paths"] == ["docs/guide.md"]
            assert "+DRAFT" in diff["diff"]
            events = await service.handle(
                _request("task.events", {"task_id": "hooks-task"})
            )
            kinds = [event["event_type"] for event in events]
            assert "hook.blocked" in kinds and "hook.changed" in kinds
            # Edits wait for review; the checkout is untouched.
            assert (repository / "docs/guide.md").read_text() == "original\n"
        finally:
            service.close()

    asyncio.run(scenario())
