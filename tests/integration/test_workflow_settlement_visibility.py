"""Readers must never observe a held or cancelled task as failed during settlement."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from test_shared_agent_execution import (
    ScriptedProvider,
    _call,
    _finish,
    _open_session,
    _register,
    _request,
    _start,
    _tools,
)

from llm_cli.coordination.models import ReleaseResult
from llm_cli.daemon.service import DaemonService
from llm_cli.providers.base import ModelTurn, ToolCallResult


@pytest.mark.parametrize("cancelled", [False, True], ids=["review", "cancelled"])
def test_claim_release_commits_final_task_disposition_atomically(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "base\n"})
        service = service_factory(tmp_path)
        staged, finish = threading.Event(), threading.Event()

        def finish_when_released(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert all(not result.is_error for result in results)
            staged.set()
            assert finish.wait(5)
            return _finish()

        provider = ScriptedProvider(
            "writer",
            [
                _tools(
                    _call("read_file", path="a.py"),
                    _call("write_file", path="a.py", content="private proposal\n"),
                ),
                finish_when_released,
            ],
        )
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        session = await _open_session(service, root, provider)
        if not cancelled:
            await service.handle(
                _request("session.set_mode", {**session, "mode": "normal"})
            )
        settle = service.coordinator.settle_shared_execution
        observed: list[tuple[str, str, str | None]] = []

        def inspect_committed_settlement(
            execution_id: str, *, outcome: str, failure_code: str | None = None
        ) -> ReleaseResult:
            result = settle(execution_id, outcome=outcome, failure_code=failure_code)
            # Read through a separate connection at the exact boundary that
            # task.show/list watchers can sample, before hold() continues.
            with service.store.connection() as connection:
                row = connection.execute(
                    "SELECT state,coordination_state,failure_code FROM tasks "
                    "WHERE task_id='task'"
                ).fetchone()
            assert row is not None
            observed.append(
                (row["state"], row["coordination_state"], row["failure_code"])
            )
            return result

        monkeypatch.setattr(
            service.coordinator, "settle_shared_execution", inspect_committed_settlement
        )
        worker = await _start(service, root, session, "task", ("a.py",))
        try:
            assert await asyncio.to_thread(staged.wait, 5)
            if cancelled:
                await service.handle(_request("task.cancel", {"task_id": "task"}))
            finish.set()
            await asyncio.wait_for(worker, 10)
            expected = "cancelled" if cancelled else "reviewing"
            assert observed == [(expected, "released", None)]
            assert service._task_view("task")["state"] == (
                "cancelled" if cancelled else "awaiting_review"
            )
            assert (root / "a.py").read_text() == "base\n"
            assert "+private proposal" in service.workflow.inspect("task")["diff"]
        finally:
            finish.set()
            await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 10)
            service.close()

    asyncio.run(scenario())
