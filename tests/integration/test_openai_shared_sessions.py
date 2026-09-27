"""The real OpenAI adapter drives durable shared sessions without network calls."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_optimistic_shared_sessions import (
    _assert_active_optimistic,
    _assert_diverged_batch,
)
from test_shared_agent_execution import _assert_completed, _request, _start

from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.daemon.service import DaemonService
from llm_cli.providers.openai_provider import DEFAULT_MODEL, OpenAIProvider

type ResponseStep = (
    list[dict[str, Any]] | Callable[[dict[str, Any]], list[dict[str, Any]]]
)


class ResponseStream:
    def __init__(self, output: list[dict[str, Any]]) -> None:
        self.output = output

    def __enter__(self) -> ResponseStream:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def get_final_response(self) -> SimpleNamespace:
        return SimpleNamespace(
            id="resp_integration",
            status="completed",
            output=self.output,
            usage=SimpleNamespace(
                input_tokens=11,
                output_tokens=7,
                input_tokens_details=SimpleNamespace(cached_tokens=3),
                output_tokens_details=SimpleNamespace(reasoning_tokens=5),
            ),
        )


class ResponsesClient:
    """Only the SDK boundary is faked; native item validity is checked here."""

    def __init__(self, steps: Sequence[ResponseStep]) -> None:
        self.responses = self
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []

    def stream(self, **arguments: Any) -> ResponseStream:
        request = copy.deepcopy(arguments)
        self.requests.append(request)
        inputs = request["input"]
        calls = [
            item["call_id"] for item in inputs if item.get("type") == "function_call"
        ]
        results = [
            item["call_id"]
            for item in inputs
            if item.get("type") == "function_call_output"
        ]
        # Sending a subsequent prompt while finish_task remains unanswered is
        # rejected by the real API, even though a scripted neutral model works.
        assert calls == results, "native calls must have one ordered tool result each"
        assert len(set(calls)) == len(calls)
        assert self.steps, "unexpected OpenAI Responses request"
        step = self.steps.pop(0)
        return ResponseStream(step(request) if callable(step) else step)


def _function(call_id: str, name: str, **arguments: object) -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
        "status": "completed",
    }


def _reasoning(identifier: str) -> dict[str, Any]:
    return {
        "type": "reasoning",
        "id": f"rs_{identifier}",
        "summary": [{"type": "summary_text", "text": "Inspect the requested files."}],
        "encrypted_content": f"opaque-encrypted-reasoning-{identifier}",
        "status": "completed",
    }


def _finish(call_id: str = "finish") -> list[dict[str, Any]]:
    return [
        _function(
            call_id,
            "finish_task",
            answer="completed requested edits",
            summary="completed requested edits",
        )
    ]


def _install_client(service: DaemonService, client: ResponsesClient) -> OpenAIProvider:
    provider = OpenAIProvider(client=client)
    assert provider.model == DEFAULT_MODEL
    assert "openai" in service.providers.names
    service.providers.replace(
        "openai",
        lambda model: OpenAIProvider(model=model or DEFAULT_MODEL, client=client),
    )
    return provider


async def _open_openai_session(
    service: DaemonService,
    repository: Path,
    session_id: str,
    *,
    model: str | None = None,
) -> dict[str, str]:
    secret = f"resume-secret-for-{session_id}"
    credentials = {"session_id": session_id, "resume_secret": secret}
    opened = await service.handle(
        _request(
            "session.open",
            {
                "session_id": session_id,
                "path": str(repository),
                "resume_token_hash": hashlib.sha256(secret.encode()).hexdigest(),
                "provider": "openai",
                **({"model": model} if model is not None else {}),
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


def test_openai_shared_followup_preserves_native_history_and_reads_latest_files(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    git_run: Callable[..., str],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        service = service_factory(tmp_path)
        reasoning = _reasoning("first")
        first_calls = [
            _function("read-first", "read_file", path="docs/guide.md"),
            _function(
                "write-first", "write_file", path="docs/guide.md", content="first\n"
            ),
        ]

        def finish_private(request: dict[str, Any]) -> list[dict[str, Any]]:
            assert (repository / "docs/guide.md").read_text() == "base\n"
            assert reasoning in request["input"]
            assert all(item in request["input"] for item in first_calls)
            return [
                _function("read-terminal", "read_file", path="docs/guide.md"),
                *_finish("finish-first"),
            ]

        def finish_followup(request: dict[str, Any]) -> list[dict[str, Any]]:
            latest = request["input"][-1]
            assert latest["type"] == "function_call_output"
            assert latest["call_id"] == "read-followup"
            assert json.loads(latest["output"]) == {
                "content": "first\n",
                "is_error": False,
            }
            return _finish("finish-followup")

        client = ResponsesClient(
            [
                [reasoning, *first_calls],
                finish_private,
                [_function("read-followup", "read_file", path="docs/guide.md")],
                finish_followup,
            ]
        )
        _install_client(service, client)
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            credentials = await _open_openai_session(service, repository, "openai-chat")
            refs = git_run(repository, "show-ref")
            index = git_run(repository, "write-tree")
            first = await _start(service, repository, credentials, "first", ("docs/",))
            await asyncio.wait_for(first, timeout=20)
            _assert_completed(service, "first")
            assert (repository / "docs/guide.md").read_text() == "first\n"
            stored = service.store.session_conversation(credentials["session_id"])
            assert stored is not None and stored[:2] == ("openai", DEFAULT_MODEL)
            native = stored[2]["session"]["input"]
            assert reasoning in native
            assert any(
                item.get("type") == "function_call_output"
                and item["call_id"] == "finish-first"
                for item in native
            )
            terminal_read = next(
                item
                for item in native
                if item.get("type") == "function_call_output"
                and item["call_id"] == "read-terminal"
            )
            assert json.loads(terminal_read["output"]) == {
                "content": "first\n",
                "is_error": False,
            }

            followup = await _start(
                service, repository, credentials, "followup", ("docs/",)
            )
            await asyncio.wait_for(followup, timeout=20)
            _assert_completed(service, "followup")
            assert client.requests[2]["input"][:-1] == native
            assert client.requests[2]["input"][-1]["role"] == "user"
            assert "complete followup" in client.requests[2]["input"][-1]["content"]
            assert all(request["model"] == DEFAULT_MODEL for request in client.requests)
            assert all(request["store"] is False for request in client.requests)
            assert all(
                "reasoning.encrypted_content" in request["include"]
                for request in client.requests
            )
            offered = {tool["name"] for tool in client.requests[0]["tools"]}
            assert {"read_file", "write_file", "finish_task"} <= offered
            assert "run_command" not in offered
            assert git_run(repository, "show-ref") == refs
            assert git_run(repository, "write-tree") == index
            assert not client.steps
        finally:
            service.close()

    asyncio.run(scenario())


def test_openai_restart_restores_native_calls_and_private_candidate_without_replay(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        dead = service_factory(tmp_path)
        reasoning = _reasoning("restart")

        def interrupted(request: dict[str, Any]) -> list[dict[str, Any]]:
            assert request["input"][-1]["call_id"] == "write"
            assert (repository / "docs/guide.md").read_text() == "base\n"
            raise KeyboardInterrupt

        interrupted_client = ResponsesClient(
            [
                [
                    reasoning,
                    _function("read", "read_file", path="docs/guide.md"),
                    _function(
                        "write", "write_file", path="docs/guide.md", content="resumed\n"
                    ),
                ],
                interrupted,
            ]
        )
        provider = _install_client(dead, interrupted_client)
        dead.initialize()
        await dead.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open_openai_session(dead, repository, "resumable")
        registered = dead.store.list_repositories()[0]
        task = dead.store.create_task(
            repository_id=registered.repository_id,
            session_id=credentials["session_id"],
            task_id="resume-openai",
            title="resume private OpenAI edits",
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
            assert reasoning in saved["session"]["input"]
            assert [item["call_id"] for item in saved["tool_results"]] == [
                "read",
                "write",
            ]
            assert (repository / "docs/guide.md").read_text() == "base\n"
        finally:
            dead.close()

        resumed_client = ResponsesClient([_finish("finish-resumed")])
        live = DaemonService(
            dead.paths, dead.settings, asyncio.Event(), boot_id="boot-openai-restarted"
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
            replayed = resumed_client.requests[0]["input"]
            interrupted_input = interrupted_client.requests[-1]["input"]
            # Native history and completed tool outcomes survive exactly. The
            # resumed request may add fresh coordination facts (including the
            # daemon's session-disconnected event) to its final real result.
            assert replayed[:-1] == interrupted_input[:-1]
            assert replayed[-1]["call_id"] == interrupted_input[-1]["call_id"]
            before = json.loads(interrupted_input[-1]["output"])
            after = json.loads(replayed[-1]["output"])
            assert after["is_error"] == before["is_error"]
            marker = "\n\n[Coordination update: advisory checkout facts]\n"
            assert (
                after["content"].partition(marker)[0]
                == before["content"].partition(marker)[0]
            )
            assert after["content"].count(marker) == 1
            assert (repository / "docs/guide.md").read_text() == "resumed\n"
            writes = [
                event
                for event in live.store.list_task_events(task.task_id)
                if event.event_type == "tool.called"
                and event.payload.get("tool") == "write_file"
            ]
            assert len(writes) == 1
            assert (await live.handle(_request("task.recover", {})))["outcomes"] == []
        finally:
            live.close()

    asyncio.run(scenario())


def test_openai_sessions_prepare_overlapping_edits_and_preserve_the_losing_batch(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path,
            {
                "docs/shared.md": "base\n",
                "docs/first.md": "base\n",
                "docs/second.md": "base\n",
            },
        )
        service = service_factory(tmp_path)
        staged = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]

        def pause(index: int) -> Callable[[dict[str, Any]], list[dict[str, Any]]]:
            def complete(request: dict[str, Any]) -> list[dict[str, Any]]:
                assert (
                    len(
                        [
                            i
                            for i in request["input"]
                            if i.get("type") == "function_call_output"
                        ]
                    )
                    == 4
                )
                staged[index].set()
                assert release[index].wait(timeout=10), "staged model was not released"
                return _finish(f"finish-{index}")

            return complete

        clients = [
            ResponsesClient(
                [
                    [
                        _reasoning(name),
                        _function("read-shared", "read_file", path="docs/shared.md"),
                        _function("read-private", "read_file", path=f"docs/{name}.md"),
                        _function(
                            "write-shared",
                            "write_file",
                            path="docs/shared.md",
                            content=f"{name}\n",
                        ),
                        _function(
                            "write-private",
                            "write_file",
                            path=f"docs/{name}.md",
                            content=f"{name}\n",
                        ),
                    ],
                    pause(index),
                ]
            )
            for index, name in enumerate(("first", "second"))
        ]
        # Distinct model IDs select two injected clients while every task still
        # travels through the registered OpenAI provider and coding harness.
        service.providers.replace(
            "openai",
            lambda model: OpenAIProvider(
                model=model or DEFAULT_MODEL,
                client=clients[0 if model == "codex-first" else 1],
            ),
        )
        service.initialize()
        backgrounds: list[asyncio.Task[None]] = []
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            sessions = []
            for name in ("first", "second"):
                credentials = await _open_openai_session(
                    service, repository, name, model=f"codex-{name}"
                )
                sessions.append(credentials)
                backgrounds.append(
                    await _start(service, repository, credentials, name, ("docs/",))
                )
            for ready in staged:
                assert await asyncio.to_thread(ready.wait, 5)
            for session in sessions:
                _assert_active_optimistic(
                    service, session["session_id"], session["session_id"]
                )
            assert (repository / "docs/shared.md").read_text() == "base\n"
            release[0].set()
            await asyncio.wait_for(backgrounds[0], timeout=10)
            _assert_completed(service, "first")
            release[1].set()
            await asyncio.wait_for(backgrounds[1], timeout=10)
            _assert_diverged_batch(
                service,
                "second",
                {"docs/shared.md": b"second\n", "docs/second.md": b"second\n"},
            )
            assert (repository / "docs/shared.md").read_text() == "first\n"
            assert (repository / "docs/first.md").read_text() == "first\n"
            assert (repository / "docs/second.md").read_text() == "base\n"
            assert all(len(client.requests) == 2 for client in clients)
        finally:
            for signal in release:
                signal.set()
            await asyncio.wait_for(
                asyncio.gather(*backgrounds, return_exceptions=True), timeout=15
            )
            service.close()

    asyncio.run(scenario())


def test_explicit_openai_provider_uses_its_own_default_or_explicit_model(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        service = service_factory(tmp_path)
        service.settings = replace(
            service.settings, agent_provider="anthropic", agent_model="claude-custom"
        )
        client = ResponsesClient(
            [
                _finish("default"),
                _finish("custom"),
                [
                    _function("background-read", "read_file", path="docs/guide.md"),
                    _function(
                        "background-write",
                        "write_file",
                        path="docs/guide.md",
                        content="background proposal\n",
                    ),
                    *_finish("background"),
                ],
            ]
        )
        _install_client(service, client)
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            for task_id, requested_model, expected_model in (
                ("default", None, DEFAULT_MODEL),
                ("custom", "codex-explicit-model", "codex-explicit-model"),
            ):
                credentials = await _open_openai_session(
                    service, repository, task_id, model=requested_model
                )
                session = service.store.get_session(task_id)
                assert session is not None and session.model == expected_model
                background = await _start(
                    service, repository, credentials, task_id, ("docs/",)
                )
                await asyncio.wait_for(background, timeout=20)
                _assert_completed(service, task_id)
                assert client.requests[-1]["model"] == expected_model

            accepted = await service.handle(
                _request(
                    "task.run",
                    {
                        "title": "complete background task",
                        "path": str(repository),
                        "scopes": ["docs/"],
                        "task_id": "background",
                        "provider": "openai",
                    },
                )
            )
            assert accepted["execution"] == "scheduled"
            await asyncio.wait_for(
                service._background_tasks[("background", 1)], timeout=20
            )
            task = service.store.get_task("background")
            assert task is not None and task.state == "ready_for_integration", (
                service.store.list_task_events("background")
            )
            assert client.requests[-1]["model"] == DEFAULT_MODEL
            assert not client.steps
        finally:
            service.close()

    asyncio.run(scenario())
