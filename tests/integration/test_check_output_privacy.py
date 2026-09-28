"""Subprocess output is screened before event emission or durable storage."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from test_shared_agent_execution import _finish, _tools
from test_verified_workflow import await_task, edit_provider, setup

from llm_cli.daemon.service import DaemonService
from llm_cli.providers.base import ToolCallRequest


@pytest.mark.parametrize(
    "unterminated", [False, True], ids=["complete-line", "eof-tail"]
)
def test_private_key_split_across_check_output_blocks_is_never_persisted(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    unterminated: bool,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        provider = edit_provider(
            _tools(ToolCallRequest("check", "run_check", {"name": "test"})), _finish()
        )
        credentials = await setup(
            service,
            root,
            provider,
            required=False,
            code=(
                "import sys,time; print('safe progress',flush=True); "
                "sys.stdout.write('-----BEGIN '); sys.stdout.flush(); "
                "time.sleep(0.15); "
                + (
                    "sys.stdout.write('PRIVATE KEY-----')"
                    if unterminated
                    else "print('PRIVATE KEY-----',flush=True); "
                    "print('unpublished-check-material',flush=True); "
                    "sys.stdout.write('unpublished-check-tail')"
                )
            ),
        )
        try:
            await await_task(service, root, credentials)
            with service.store.connection() as connection:
                row = connection.execute(
                    "SELECT state,output FROM check_runs WHERE task_id='task'"
                ).fetchone()
            assert row is not None
            assert row[0] == "error"
            assert "safe progress" in row[1]
            assert "secret material" in row[1]
            observed = repr(
                (
                    row[1],
                    service.store.list_task_events("task"),
                    provider.results,
                    provider.recorded_results,
                )
            )
            assert "-----BEGIN" not in observed
            assert "unpublished-check-material" not in observed
            assert "unpublished-check-tail" not in observed
        finally:
            service.close()

    asyncio.run(scenario())
