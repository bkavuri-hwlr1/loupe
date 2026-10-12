"""Auto-mode tasks catch up with changes published after they finished."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_shared_agent_execution import _assert_completed, _finish
from test_verified_workflow import await_task, edit_provider, setup

from llm_cli.daemon.service import DaemonService
from llm_cli.workspace.workflow import decode_files


def _publish_after_finishing(
    service: DaemonService, monkeypatch: pytest.MonkeyPatch, change: Callable[[], Any]
) -> None:
    """Change the checkout once, as another session would, right after the
    agent finished and before this task publishes."""

    workflow = service.shared_runner.workflow
    retain = workflow.retain
    pending = [change]

    def retain_then_change(*args: Any, **kwargs: Any) -> None:
        retain(*args, **kwargs)
        if pending:
            pending.pop()()

    monkeypatch.setattr(workflow, "retain", retain_then_change)


# Lines far enough apart for edits to each to merge.
_LINES = "value = {}\n\nunrelated = True\n\nname = {!r}\n"


def _checks(service: DaemonService) -> list[str]:
    return [
        str(event.payload["state"])
        for event in service.store.list_task_events("task")
        if event.event_type == "check.finished"
    ]


def test_checks_rerun_when_another_session_published_after_they_ran(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n", "b.py": "b\n"})
        service = service_factory(tmp_path)
        creds = await setup(service, root, edit_provider(_finish()), code="pass")
        _publish_after_finishing(
            service, monkeypatch, lambda: (root / "b.py").write_text("b2\n")
        )
        await await_task(service, root, creds)

        # Its checks went stale, so they ran again on the combined checkout
        # instead of the task being held for review.
        _assert_completed(service, "task")
        assert (root / "a.py").read_text() == "value = 2\n"
        assert (root / "b.py").read_text() == "b2\n"
        assert _checks(service) == ["passed", "passed"]

    asyncio.run(scenario())


def test_edits_merge_onto_a_change_published_after_the_agent_finished(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": _LINES.format(1, "a")})
        service = service_factory(tmp_path)
        provider = edit_provider(_finish())
        provider.steps[0].tool_calls[1].arguments["content"] = (  # type: ignore[index]
            _LINES.format(2, "a")
        )
        creds = await setup(service, root, provider)
        _publish_after_finishing(
            service,
            monkeypatch,
            lambda: (root / "a.py").write_text(_LINES.format(1, "b")),
        )
        await await_task(service, root, creds)
        _assert_completed(service, "task")
        assert (root / "a.py").read_text() == _LINES.format(2, "b")
        reconciled = [
            event.payload
            for event in service.store.list_task_events("task")
            if event.event_type == "edits.reconciled"
        ]
        assert [payload["merged"] for payload in reconciled] == [["a.py"]]

    asyncio.run(scenario())


def test_a_conflict_found_while_publishing_holds_the_agents_own_edits(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        creds = await setup(service, root, edit_provider(_finish()), code="pass")
        _publish_after_finishing(
            service, monkeypatch, lambda: (root / "a.py").write_text("value = 9\n")
        )
        await await_task(service, root, creds)

        # No agent is left to resolve the conflict, so nothing is merged: the
        # proposal is the agent's own edit, held for review.
        assert service._task_view("task")["state"] == "awaiting_review"
        assert (root / "a.py").read_text() == "value = 9\n"
        workflow = service.shared_runner.workflow.get("task", 1)
        assert workflow is not None
        (held,) = decode_files(workflow["proposal_json"])
        assert held.content == b"value = 2\n"
        assert _checks(service) == ["passed"]
        assert not any(
            event.event_type == "edits.reconciled"
            for event in service.store.list_task_events("task")
        )

    asyncio.run(scenario())


def test_normal_mode_holds_for_review_without_catching_up(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n", "b.py": "b\n"})
        service = service_factory(tmp_path)
        creds = await setup(
            service, root, edit_provider(_finish()), review=True, code="pass"
        )
        _publish_after_finishing(
            service, monkeypatch, lambda: (root / "b.py").write_text("b2\n")
        )
        await await_task(service, root, creds)

        assert service._task_view("task")["state"] == "awaiting_review"
        assert _checks(service) == ["passed"]

    asyncio.run(scenario())
