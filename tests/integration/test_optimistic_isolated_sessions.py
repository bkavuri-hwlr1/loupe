"""Isolated and mixed sessions prepare concurrently with private claim bindings."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from test_optimistic_shared_sessions import (
    _assert_active_optimistic,
    _finish_after_release,
)
from test_shared_agent_execution import (
    ScriptedProvider,
    _assert_completed,
    _call,
    _register,
    _request,
    _start,
    _tools,
)

from llm_cli.daemon.service import DaemonService


async def _wait_prepared(
    service: DaemonService,
    staged: dict[str, threading.Event],
    backgrounds: list[asyncio.Task[None]],
    *,
    timeout: float = 30,
) -> None:
    """A single barrier deadline preserves the real failure before cleanup."""
    deadline = time.monotonic() + timeout
    while pending := [
        task_id for task_id, ready in staged.items() if not ready.is_set()
    ]:
        if any(task.done() for task in backgrounds) or time.monotonic() >= deadline:
            evidence = {
                task_id: {
                    "task": service.store.get_task(task_id),
                    "events": service.store.list_task_events(task_id)[-12:],
                }
                for task_id in staged
            }
            outcomes = [
                repr(task.exception())
                if task.done() and not task.cancelled()
                else "cancelled"
                if task.cancelled()
                else "running"
                for task in backgrounds
            ]
            raise AssertionError(
                f"Preparation did not reach its barrier: {pending}; "
                f"backgrounds={outcomes}; evidence={evidence}"
            )
        await asyncio.sleep(0.02)


@pytest.mark.parametrize("modes", [("isolated", "isolated"), ("shared", "isolated")])
def test_same_scope_isolated_and_mixed_preparation_overlaps_before_publication(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    git_run: Callable[..., str],
    modes: tuple[str, str],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path,
            {"docs/first.md": "base first\n", "docs/second.md": "base second\n"},
        )
        service = service_factory(tmp_path)
        staged = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        providers = [
            ScriptedProvider(
                name,
                [
                    _tools(
                        _call("read_file", path=f"docs/{name}.md"),
                        _call(
                            "write_file",
                            path=f"docs/{name}.md",
                            content=f"new {name}\n",
                        ),
                    ),
                    _finish_after_release(staged[index], release[index]),
                ],
            )
            for index, name in enumerate(("first", "second"))
        ]
        _register(service, *providers)
        service.initialize()
        backgrounds: list[asyncio.Task[None]] = []
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            credentials = []
            for provider, mode in zip(providers, modes, strict=True):
                secret = f"resume-{provider.model}"
                credential = {"session_id": provider.model, "resume_secret": secret}
                opened = await service.handle(
                    _request(
                        "session.open",
                        {
                            "path": str(repository),
                            "session_id": provider.model,
                            "resume_token_hash": hashlib.sha256(
                                secret.encode()
                            ).hexdigest(),
                            "provider": provider.name,
                            "model": provider.model,
                            "workspace": mode,
                        },
                    )
                )
                await service.handle(
                    _request(
                        "session.ack",
                        {
                            **credential,
                            "sequence": opened["bootstrap_sequence"],
                        },
                    )
                )
                credentials.append(credential)
            refs = git_run(repository, "show-ref")
            index = git_run(repository, "write-tree")
            for provider, credential in zip(providers, credentials, strict=True):
                backgrounds.append(
                    await _start(
                        service, repository, credential, provider.model, ("docs/",)
                    )
                )
            # Neither task may need the other's publication to begin preparation.
            await _wait_prepared(
                service,
                {
                    provider.model: ready
                    for provider, ready in zip(providers, staged, strict=True)
                },
                backgrounds,
            )
            for provider in providers:
                _assert_active_optimistic(service, provider.model, provider.model)
            assert (repository / "docs/first.md").read_text() == "base first\n"
            assert (repository / "docs/second.md").read_text() == "base second\n"
            for number, provider in enumerate(providers):
                release[number].set()
                await asyncio.wait_for(backgrounds[number], timeout=10)
                _assert_completed(service, provider.model)
                execution = service.store.get_execution(provider.model, 1)
                session = service.store.get_session(provider.model)
                assert execution is not None and session is not None
                assert execution.workspace_id == session.workspace_id
            assert (repository / "docs/first.md").read_text() == "new first\n"
            assert (repository / "docs/second.md").read_text() == "new second\n"
            assert git_run(repository, "show-ref") == refs
            assert git_run(repository, "write-tree") == index
        finally:
            for signal in release:
                signal.set()
            await asyncio.wait_for(
                asyncio.gather(*backgrounds, return_exceptions=True), 15
            )
            service.close()

    asyncio.run(scenario())
