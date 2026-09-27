"""Live advisory context reaches models without weakening shared publication."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from test_openai_shared_sessions import (
    ResponsesClient,
    _function,
    _install_client,
    _open_openai_session,
    _reasoning,
)
from test_openai_shared_sessions import _finish as _openai_finish
from test_optimistic_shared_sessions import _assert_diverged_batch
from test_shared_agent_execution import (
    ScriptedProvider,
    _assert_completed,
    _call,
    _finish,
    _open_session,
    _register,
    _request,
    _start,
    _tools,
)

from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.daemon.service import DaemonService
from llm_cli.providers.base import ModelTurn, ToolCallResult
from llm_cli.providers.openai_provider import DEFAULT_MODEL

_START = "\n\n[Coordination update: advisory checkout facts]\n"
_END = "\n[End coordination update]"


def _packet(content: str) -> dict[str, Any]:
    assert content.count(_START) == 1
    _, encoded = content.rsplit(_START, 1)
    assert encoded.endswith(_END)
    decoded = json.loads(encoded.removesuffix(_END))
    assert decoded["type"] == "shared_coordination"
    return decoded


@pytest.mark.parametrize("overlapping", [True, False], ids=["stale", "disjoint"])
def test_mid_turn_peer_publication_refreshes_context_and_keeps_cas_authority(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    overlapping: bool,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path,
            {
                "docs/a.md": "base a\n",
                "docs/b.md": "base b\n",
                "docs/private.md": "base private\n",
            },
        )
        service = service_factory(tmp_path)
        staged = threading.Event()
        release = threading.Event()
        target = "docs/a.md" if overlapping else "docs/b.md"
        seen: list[dict[str, Any]] = []

        def wait_for_peer(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert len(results) == 4 and all(not item.is_error for item in results)
            staged.set()
            assert release.wait(timeout=15), "peer publication was not released"
            return _tools(
                _call("read_file", path="docs/private.md"),
                _call("validate_changes"),
            )

        def inspect_update(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert len(results) == 2
            # The update belongs to the last real result only. The broker's
            # stale-base refusal, call ids, and first result remain intact.
            assert results[0] == ToolCallResult(
                call_id="read_file:docs/private.md",
                content="b private\n",
                is_error=False,
            )
            assert results[1].call_id == "validate_changes:"
            assert results[1].is_error is overlapping
            if overlapping:
                assert results[1].content.startswith(
                    "shared paths changed since their base read"
                )
            packet = _packet(results[1].content)
            seen.append(packet)
            assert packet["workspace"]["revision"] == 1
            publication = next(
                item
                for item in packet["changes"]
                if item["session_id"] == "peer-a"
                and item.get("workspace_revision") == 1
            )
            assert publication["own_session"] is False
            assert publication["paths"] == [
                {"path": "docs/a.md", "overlaps_scope": overlapping}
            ]
            return _finish("finished after observing the peer publication")

        a = ScriptedProvider(
            "peer-a",
            [
                _tools(
                    _call("read_file", path="docs/a.md"),
                    _call("write_file", path="docs/a.md", content="a published\n"),
                ),
                _finish(),
            ],
        )
        b = ScriptedProvider(
            "peer-b",
            [
                _tools(
                    _call("read_file", path=target),
                    _call("read_file", path="docs/private.md"),
                    _call("write_file", path=target, content="b candidate\n"),
                    _call("write_file", path="docs/private.md", content="b private\n"),
                ),
                wait_for_peer,
                inspect_update,
            ],
        )
        _register(service, a, b)
        service.initialize()
        backgrounds: list[asyncio.Task[None]] = []
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            a_session = await _open_session(service, repository, a)
            b_session = await _open_session(service, repository, b)
            b_background = await _start(
                service, repository, b_session, "b-task", (target, "docs/private.md")
            )
            backgrounds.append(b_background)
            assert await asyncio.to_thread(staged.wait, 5)
            a_background = await _start(
                service, repository, a_session, "a-task", ("docs/a.md",)
            )
            backgrounds.append(a_background)
            await asyncio.wait_for(a_background, timeout=15)
            _assert_completed(service, "a-task")
            assert (repository / "docs/a.md").read_text() == "a published\n"
            release.set()
            await asyncio.wait_for(b_background, timeout=15)
            assert len(seen) == 1
            assert not a.steps and not b.steps

            if overlapping:
                _assert_diverged_batch(
                    service,
                    "b-task",
                    {"docs/a.md": b"b candidate\n", "docs/private.md": b"b private\n"},
                )
                assert (repository / "docs/private.md").read_text() == "base private\n"
            else:
                _assert_completed(service, "b-task")
                assert (repository / "docs/b.md").read_text() == "b candidate\n"
                assert (repository / "docs/private.md").read_text() == "b private\n"
                conversation = service.store.session_conversation("peer-b")
                assert conversation is not None
                consumed_sequence = conversation[2]["coordination_sequence"]
                assert consumed_sequence == seen[0]["sequence"]

                def inspect_followup(results: Sequence[ToolCallResult]) -> ModelTurn:
                    assert not results
                    packet = _packet(str(b.history[-1]["content"]))
                    assert packet["after_sequence"] == consumed_sequence
                    assert packet["workspace"]["revision"] == 2
                    publications = [
                        item
                        for item in packet["changes"]
                        if "workspace_revision" in item
                    ]
                    assert len(publications) == 1
                    assert publications[0]["session_id"] == "peer-b"
                    assert publications[0]["own_session"] is True
                    assert publications[0]["workspace_revision"] == 2
                    return _finish("follow-up has the latest publication")

                b.steps.append(inspect_followup)
                followup = await _start(
                    service,
                    repository,
                    b_session,
                    "b-followup",
                    (target, "docs/private.md"),
                )
                backgrounds.append(followup)
                await asyncio.wait_for(followup, timeout=15)
                _assert_completed(service, "b-followup")
                assert not b.steps
        finally:
            release.set()
            await asyncio.wait_for(
                asyncio.gather(*backgrounds, return_exceptions=True), timeout=20
            )
            service.close()

    asyncio.run(scenario())


def test_restart_preserves_consumed_cursor_and_retries_undecorated_pending_results(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        dead = service_factory(tmp_path)
        reasoning = _reasoning("context-restart")

        def prepare_edit(request: dict[str, Any]) -> list[dict[str, Any]]:
            _packet(request["input"][-1]["content"])
            dead.store.set_session_intent("context-peer", paths=("docs/first.md",))
            return [
                reasoning,
                _function("read", "read_file", path="docs/guide.md"),
                _function(
                    "write", "write_file", path="docs/guide.md", content="resumed\n"
                ),
            ]

        def inspect_private(request: dict[str, Any]) -> list[dict[str, Any]]:
            _packet(json.loads(request["input"][-1]["output"])["content"])
            dead.store.set_session_intent("context-peer", paths=("docs/revised.md",))
            return [_function("read-private", "read_file", path="docs/guide.md")]

        def interrupt(request: dict[str, Any]) -> list[dict[str, Any]]:
            assert request["input"][-1]["call_id"] == "read-private"
            _packet(json.loads(request["input"][-1]["output"])["content"])
            raise KeyboardInterrupt

        first_client = ResponsesClient([prepare_edit, inspect_private, interrupt])
        provider = _install_client(dead, first_client)
        dead.initialize()
        await dead.handle(_request("repo.add", {"path": str(repository)}))
        await _open_openai_session(dead, repository, "context-peer")
        credentials = await _open_openai_session(dead, repository, "context-restart")
        registered = dead.store.list_repositories()[0]
        task = dead.store.create_task(
            repository_id=registered.repository_id,
            session_id=credentials["session_id"],
            task_id="resume-context",
            title="resume context and private edits",
        )
        dead.store.save_execution_launch(
            task_id=task.task_id,
            attempt=task.attempt,
            driver="coding_agent",
            instructions=task.title,
            interactive=False,
            parameters={"provider": "openai", "model": DEFAULT_MODEL},
        )
        claim = dead.coordinator.request_claim(
            task.task_id,
            ["docs/"],
            scheduling_mode="optimistic",
            optimistic_driver="coding_agent",
        )
        try:
            with pytest.raises(KeyboardInterrupt):
                dead._execute(
                    repository=registered,
                    task=task,
                    claim=claim,
                    driver=CodingAgentHarness(provider),
                    instructions=task.title,
                )
            execution = dead.store.get_execution(task.task_id, 1)
            assert execution is not None
            checkpoint = dead.store.get_execution_checkpoint(execution.execution_id)
            assert checkpoint is not None
            saved = checkpoint.checkpoint
            assert saved["phase"] == "tool_results"
            assert saved["tool_results"] == [
                {"call_id": "read-private", "content": "resumed\n", "is_error": False}
            ]
            prior_packet = _packet(
                json.loads(first_client.requests[1]["input"][-1]["output"])["content"]
            )
            attempted_packet = _packet(
                json.loads(first_client.requests[2]["input"][-1]["output"])["content"]
            )
            assert saved["coordination_sequence"] == prior_packet["sequence"]
            assert attempted_packet["sequence"] > saved["coordination_sequence"]
            assert saved["session"]["input"][-1]["call_id"] == "read-private"
            assert saved["session"]["input"][-1]["type"] == "function_call"
            assert reasoning in saved["session"]["input"]
            assert (repository / "docs/guide.md").read_text() == "base\n"
        finally:
            dead.close()

        def inspect_resume(request: dict[str, Any]) -> list[dict[str, Any]]:
            assert request["input"][:-1] == saved["session"]["input"]
            pending = request["input"][-1]
            assert pending["call_id"] == "read-private"
            output = json.loads(pending["output"])
            assert output["is_error"] is False
            assert output["content"].startswith("resumed\n" + _START)
            packet = _packet(output["content"])
            assert packet["after_sequence"] == saved["coordination_sequence"]
            assert packet["sequence"] >= attempted_packet["sequence"]
            assert packet["intents"] == [
                {
                    "session_id": "context-peer",
                    "paths": [{"path": "docs/revised.md", "overlaps_scope": True}],
                }
            ]
            return _openai_finish("finish-resumed")

        resumed_client = ResponsesClient([inspect_resume])
        live = DaemonService(
            dead.paths, dead.settings, asyncio.Event(), boot_id="boot-context-restart"
        )
        _install_client(live, resumed_client)
        live.initialize()
        try:
            assert live.startup_recovery is not None
            assert live.startup_recovery.summary == {"resuming": 1}
            await asyncio.wait_for(
                live._background_tasks[(task.task_id, 1)], timeout=20
            )
            _assert_completed(live, task.task_id)
            assert len(resumed_client.requests) == 1
            assert (repository / "docs/guide.md").read_text() == "resumed\n"
            writes = [
                event
                for event in live.store.list_task_events(task.task_id)
                if event.event_type == "tool.called"
                and event.payload.get("tool") == "write_file"
            ]
            assert len(writes) == 1
            conversation = live.store.session_conversation(credentials["session_id"])
            assert conversation is not None
            final_packet = _packet(
                json.loads(resumed_client.requests[0]["input"][-1]["output"])["content"]
            )
            assert conversation[2]["coordination_sequence"] == final_packet["sequence"]
            assert (await live.handle(_request("task.recover", {})))["outcomes"] == []
        finally:
            live.close()

    asyncio.run(scenario())
