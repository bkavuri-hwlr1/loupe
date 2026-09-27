"""Session effort survives detached waiters, daemon boots, and replay."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_openai_shared_sessions import ResponsesClient, _finish

from llm_cli.daemon.service import DaemonService
from llm_cli.errors import LlmCoordError
from llm_cli.providers.openai_provider import DEFAULT_MODEL, OpenAIProvider


def test_queued_sessions_keep_distinct_efforts_after_restart(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    request_factory: Callable[[str, dict[str, Any]], Any],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        dead = service_factory(tmp_path)
        dead.initialize()
        await dead.handle(request_factory("repo.add", {"path": str(repository)}))
        credentials = {}
        for effort in ("low", "high"):
            secret = f"resume-secret-{effort}"
            credentials[effort] = {
                "session_id": f"session-{effort}",
                "resume_secret": secret,
            }
            opened = await dead.handle(
                request_factory(
                    "session.open",
                    {
                        "session_id": f"session-{effort}",
                        "path": str(repository),
                        "provider": "openai",
                        "model": DEFAULT_MODEL,
                        "effort": effort,
                        "resume_token_hash": hashlib.sha256(
                            secret.encode()
                        ).hexdigest(),
                    },
                )
            )
            assert opened["session"]["effort"] == effort
            await dead.handle(
                request_factory(
                    "session.ack",
                    {**credentials[effort], "sequence": opened["bootstrap_sequence"]},
                )
            )

        blocker = await dead.handle(
            request_factory(
                "task.run",
                {
                    "path": str(repository),
                    "task_id": "blocker",
                    "title": "hold",
                    "scopes": ["*"],
                    "claim_only": True,
                },
            )
        )
        for effort in ("low", "high"):
            accepted = await dead.handle(
                request_factory(
                    "task.run",
                    {
                        **credentials[effort],
                        "path": str(repository),
                        "task_id": f"task-{effort}",
                        "title": "Read the docs",
                        "scopes": ["docs/"],
                    },
                )
            )
            assert accepted["execution"] == "queued"
            launch = dead.store.get_execution_launch(f"task-{effort}", 1)
            assert launch is not None and launch.parameters["effort"] == effort
        dead.close()

        clients = {
            effort: ResponsesClient([_finish(f"finished-{effort}")])
            for effort in ("low", "high")
        }
        live = service_factory(tmp_path)
        live.boot_id = "boot-after-effort-restart"
        live.settings = replace(live.settings, agent_provider="anthropic")

        def provider(model: str | None, *, effort: str | None = None):
            assert effort in clients
            return OpenAIProvider(
                model=model or DEFAULT_MODEL, effort=effort, client=clients[effort]
            )

        live.providers.replace("openai", provider)
        live.initialize()
        try:
            for effort in ("low", "high"):
                resumed = await live.handle(
                    request_factory("session.resume", credentials[effort])
                )
                assert resumed["session"]["effort"] == effort
            await live.handle(
                request_factory(
                    "claim.release",
                    {
                        "claim_id": blocker["claim"]["claim_id"],
                        "reason": "test release",
                    },
                )
            )
            assert await live.drain(timeout_ms=10000) == ()
            for effort in ("low", "high"):
                assert clients[effort].requests[0]["reasoning"]["effort"] == effort
                task = live.store.get_task(f"task-{effort}")
                assert task is not None and task.state == "completed"
                saved = live.store.get_session(f"session-{effort}")
                assert saved is not None and saved.effort == effort
        finally:
            live.close()

    asyncio.run(scenario())


def test_session_effort_cannot_be_overridden_by_task_or_duplicate_open(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    request_factory: Callable[[str, dict[str, Any]], Any],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        service = service_factory(tmp_path)
        service.initialize()
        await service.handle(request_factory("repo.add", {"path": str(repository)}))
        secret = "effort-bound-resume-secret"
        parameters = {
            "session_id": "bound-session",
            "path": str(repository),
            "provider": "openai",
            "model": DEFAULT_MODEL,
            "effort": "low",
            "resume_token_hash": hashlib.sha256(secret.encode()).hexdigest(),
        }
        try:
            opened = await service.handle(request_factory("session.open", parameters))
            with pytest.raises(LlmCoordError, match="already bound"):
                await service.handle(
                    request_factory("session.open", {**parameters, "effort": "high"})
                )
            credentials = {"session_id": "bound-session", "resume_secret": secret}
            await service.handle(
                request_factory(
                    "session.ack",
                    {
                        **credentials,
                        "sequence": opened["bootstrap_sequence"],
                    },
                )
            )
            for override in ("high", None):
                with pytest.raises(LlmCoordError, match="including its effort"):
                    await service.handle(
                        request_factory(
                            "task.run",
                            {
                                **credentials,
                                "path": str(repository),
                                "scopes": ["docs/"],
                                "title": "Run this task",
                                "effort": override,
                            },
                        )
                    )
            assert not service.store.list_execution_launches()
            assert not service.store.list_tasks()
        finally:
            service.close()

    asyncio.run(scenario())
