"""Optimistic sessions prepare overlapping work before serialized publication."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
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
from llm_cli.coordination.models import ClaimState
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.providers.base import ModelTurn, ToolCallResult
from llm_cli.workspace.broker import load_candidate_content


def _finish_after_release(
    staged: threading.Event, release: threading.Event
) -> Callable[[Sequence[ToolCallResult]], ModelTurn]:
    def finish(results: Sequence[ToolCallResult]) -> ModelTurn:
        assert results and all(not result.is_error for result in results)
        staged.set()
        assert release.wait(timeout=10), "the test did not release the staged agent"
        return _finish()

    return finish


def _assert_active_optimistic(
    service: DaemonService, task_id: str, session_id: str
) -> None:
    task = service.store.get_task(task_id)
    assert task is not None
    claim = service.store.get_claim(str(task.current_claim_id))
    session = service.store.get_session(session_id)
    assert claim is not None and session is not None
    assert claim.state is ClaimState.ACTIVE_WORK
    assert claim.scheduling_mode == "optimistic"
    assert claim.workspace_id == session.workspace_id
    assert not claim.blocking_claim_ids
    assert not any(
        event.event_type == "claim.queued"
        for event in service.store.list_task_events(task_id)
    )


def _assert_diverged_batch(
    service: DaemonService, task_id: str, expected: dict[str, bytes]
) -> None:
    task = service.store.get_task(task_id)
    assert task is not None and task.state == "failed"
    execution = service.store.get_execution(task_id, 1)
    assert execution is not None
    assert execution.failure_code == ErrorCode.PATH_BASE_MISMATCH
    claim = service.store.get_claim(str(task.current_claim_id))
    assert claim is not None and claim.state is ClaimState.RELEASED
    with service.store.connection() as connection:
        batch = connection.execute(
            "SELECT * FROM workspace_batches WHERE batch_id = ?",
            (execution.execution_id,),
        ).fetchone()
    assert batch is not None and batch["state"] == "diverged"
    assert batch["workspace_revision"] is None
    retained = {
        item["relative_path"]: load_candidate_content(
            service.paths.candidate_dir, item["content_hash"]
        )
        for item in json.loads(batch["request_json"])["files"]
    }
    assert retained == expected


def test_same_scope_agents_prepare_concurrently_and_preserve_the_stale_batch(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    git_run: Callable[..., str],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path,
            {
                "docs/shared.md": "base shared\n",
                "docs/first.md": "base first\n",
                "docs/second.md": "base second\n",
            },
        )
        service = service_factory(tmp_path)
        staged = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        providers = [
            ScriptedProvider(
                name,
                [
                    _tools(
                        _call("read_file", path="docs/shared.md"),
                        _call("read_file", path=f"docs/{name}.md"),
                        _call(
                            "write_file",
                            path="docs/shared.md",
                            content=f"{name} shared\n",
                        ),
                        _call(
                            "write_file",
                            path=f"docs/{name}.md",
                            content=f"{name} private\n",
                        ),
                    ),
                    _finish_after_release(staged[index], release[index]),
                ],
            )
            for index, name in enumerate(("first", "second"))
        ]
        _register(service, *providers)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        sessions = [
            await _open_session(service, repository, provider) for provider in providers
        ]
        refs = git_run(repository, "show-ref")
        index = git_run(repository, "write-tree")
        backgrounds: list[asyncio.Task[None]] = []
        try:
            for name, session in zip(("first", "second"), sessions, strict=True):
                backgrounds.append(
                    await _start(service, repository, session, name, ("docs/",))
                )
            for ready in staged:
                assert await asyncio.to_thread(ready.wait, 5)
            for name, session in zip(("first", "second"), sessions, strict=True):
                _assert_active_optimistic(service, name, session["session_id"])
            assert (repository / "docs/shared.md").read_text() == "base shared\n"
            assert (repository / "docs/first.md").read_text() == "base first\n"
            assert (repository / "docs/second.md").read_text() == "base second\n"

            # Both models saw the original bytes. Only the first is allowed to
            # finish now; the second must conflict against its retained base.
            release[0].set()
            await asyncio.wait_for(backgrounds[0], timeout=10)
            _assert_completed(service, "first")
            release[1].set()
            await asyncio.wait_for(backgrounds[1], timeout=10)
            _assert_diverged_batch(
                service,
                "second",
                {
                    "docs/shared.md": b"second shared\n",
                    "docs/second.md": b"second private\n",
                },
            )
            assert (repository / "docs/shared.md").read_text() == "first shared\n"
            assert (repository / "docs/first.md").read_text() == "first private\n"
            assert (repository / "docs/second.md").read_text() == "base second\n"
            winner = service.store.get_session(sessions[0]["session_id"])
            loser = service.store.get_session(sessions[1]["session_id"])
            assert winner is not None and winner.conversation_revision == 1
            assert loser is not None and loser.conversation_revision == 0
            workspace = service.store.get_workspace(str(winner.workspace_id))
            assert workspace is not None and workspace.workspace_revision == 1
            assert git_run(repository, "show-ref") == refs
            assert git_run(repository, "write-tree") == index
        finally:
            for signal in release:
                signal.set()
            await asyncio.wait_for(
                asyncio.gather(*backgrounds, return_exceptions=True), timeout=15
            )
            service.close()

    asyncio.run(scenario())


def test_same_scope_disjoint_edits_both_publish_and_followup_sees_both(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path, {"docs/a.md": "base a\n", "docs/b.md": "base b\n"}
        )
        service = service_factory(tmp_path)
        staged = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]

        def inspect_followup(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert all(not result.is_error for result in results)
            assert [result.content for result in results] == ["new a\n", "new b\n"]
            assert providers[0].restored_state is not None
            return _finish("both peers' changes are visible")

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
            for index, name in enumerate(("a", "b"))
        ]
        providers[0].steps.extend(
            [
                _tools(
                    _call("read_file", path="docs/a.md"),
                    _call("read_file", path="docs/b.md"),
                ),
                inspect_followup,
            ]
        )
        _register(service, *providers)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        sessions = [
            await _open_session(service, repository, provider) for provider in providers
        ]
        backgrounds: list[asyncio.Task[None]] = []
        try:
            for name, session in zip(("a", "b"), sessions, strict=True):
                backgrounds.append(
                    await _start(service, repository, session, name, ("docs/",))
                )
            for ready in staged:
                assert await asyncio.to_thread(ready.wait, 5)
            for name, session in zip(("a", "b"), sessions, strict=True):
                _assert_active_optimistic(service, name, session["session_id"])

            # Optimism does not permit concurrent continuations of one
            # conversation, even when their declared paths would be disjoint.
            with pytest.raises(LlmCoordError) as refusal:
                await service.handle(
                    _request(
                        "task.run",
                        {
                            **sessions[0],
                            "title": "concurrent continuation",
                            "path": str(repository),
                            "scopes": ["other/"],
                            "task_id": "too-early-followup",
                        },
                    )
                )
            assert refusal.value.code is ErrorCode.TASK_NOT_MUTABLE
            assert service.store.get_task("too-early-followup") is None
            for signal in release:
                signal.set()
            await asyncio.wait_for(asyncio.gather(*backgrounds), timeout=15)
            _assert_completed(service, "a")
            _assert_completed(service, "b")
            with service.store.connection() as connection:
                batches = connection.execute(
                    "SELECT state, workspace_revision FROM workspace_batches "
                    "ORDER BY workspace_revision"
                ).fetchall()
            assert [(row["state"], row["workspace_revision"]) for row in batches] == [
                ("published", 1),
                ("published", 2),
            ]
            followup = await _start(
                service, repository, sessions[0], "followup", ("docs/",)
            )
            backgrounds.append(followup)
            await asyncio.wait_for(followup, timeout=10)
            _assert_completed(service, "followup")
            session = service.store.get_session(sessions[0]["session_id"])
            assert session is not None and session.conversation_revision == 2
            workspace = service.store.get_workspace(str(session.workspace_id))
            assert workspace is not None and workspace.workspace_revision == 2
        finally:
            for signal in release:
                signal.set()
            await asyncio.wait_for(
                asyncio.gather(*backgrounds, return_exceptions=True), timeout=15
            )
            service.close()

    asyncio.run(scenario())


def test_restart_resumes_both_overlapping_claims_and_keeps_original_bases(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path,
            {"docs/shared.md": "base shared\n", "docs/extra.md": "base extra\n"},
        )
        dead = service_factory(tmp_path)

        def crash_after_staging(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert len(results) == 4 and all(not result.is_error for result in results)
            raise KeyboardInterrupt

        providers = [
            ScriptedProvider(
                name,
                [
                    _tools(
                        _call("read_file", path="docs/shared.md"),
                        _call("read_file", path="docs/extra.md"),
                        _call(
                            "write_file",
                            path="docs/shared.md",
                            content=f"{name} shared\n",
                        ),
                        _call(
                            "write_file",
                            path="docs/extra.md",
                            content=f"{name} extra\n",
                        ),
                    ),
                    crash_after_staging,
                ],
            )
            for name in ("first", "second")
        ]
        _register(dead, *providers)
        dead.initialize()
        await dead.handle(_request("repo.add", {"path": str(repository)}))
        sessions = [
            await _open_session(dead, repository, provider) for provider in providers
        ]
        registered = dead.store.list_repositories()[0]
        claims = []
        try:
            for provider, credentials in zip(providers, sessions, strict=True):
                task = dead.store.create_task(
                    repository_id=registered.repository_id,
                    session_id=credentials["session_id"],
                    task_id=provider.model,
                    title=f"restore {provider.model}",
                )
                dead.store.save_execution_launch(
                    task_id=task.task_id,
                    attempt=task.attempt,
                    driver="coding_agent",
                    instructions=task.title,
                    interactive=False,
                    parameters={"provider": provider.name, "model": provider.model},
                )
                claim = dead.coordinator.request_claim(
                    task.task_id,
                    ["docs/"],
                    scheduling_mode="optimistic",
                    optimistic_driver="coding_agent",
                )
                claims.append(claim)
                assert claim.state is ClaimState.ACTIVE_WORK
                # Process death escapes worker cleanup and leaves the actual
                # harness snapshot, completed tool ledger, and private bytes.
                with pytest.raises(KeyboardInterrupt):
                    dead._execute(
                        repository=registered,
                        task=task,
                        claim=claim,
                        driver=CodingAgentHarness(provider),
                        instructions=task.title,
                    )
                execution = dead.store.get_execution(task.task_id, 1)
                assert execution is not None and execution.workspace_id is not None
                checkpoint = dead.store.get_execution_checkpoint(execution.execution_id)
                assert checkpoint is not None
                assert checkpoint.checkpoint["phase"] == "tool_results"
            assert (repository / "docs/shared.md").read_text() == "base shared\n"
            assert (repository / "docs/extra.md").read_text() == "base extra\n"
        finally:
            dead.close()

        staged = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        resumed = [
            ScriptedProvider(
                name, [_finish_after_release(staged[index], release[index])]
            )
            for index, name in enumerate(("first", "second"))
        ]
        live = DaemonService(
            dead.paths,
            dead.settings,
            asyncio.Event(),
            boot_id="boot-resumed-optimistic",
        )
        _register(live, *resumed)
        backgrounds: list[asyncio.Task[None]] = []
        try:
            live.initialize()
            assert live.startup_recovery is not None
            assert live.startup_recovery.summary == {"resuming": 2}
            backgrounds = [
                live._background_tasks[(name, 1)] for name in ("first", "second")
            ]
            for ready in staged:
                assert await asyncio.to_thread(ready.wait, 5)
            for claim, credentials in zip(claims, sessions, strict=True):
                _assert_active_optimistic(
                    live, claim.task_id, credentials["session_id"]
                )
                restored_claim = live.store.get_claim(claim.claim_id)
                assert restored_claim is not None
                assert restored_claim.fencing_token == claim.fencing_token
                assert restored_claim.workspace_id == claim.workspace_id
            release[0].set()
            await asyncio.wait_for(backgrounds[0], timeout=10)
            _assert_completed(live, "first")
            release[1].set()
            await asyncio.wait_for(backgrounds[1], timeout=10)
            _assert_diverged_batch(
                live,
                "second",
                {
                    "docs/shared.md": b"second shared\n",
                    "docs/extra.md": b"second extra\n",
                },
            )
            assert (repository / "docs/shared.md").read_text() == "first shared\n"
            assert (repository / "docs/extra.md").read_text() == "first extra\n"
            for provider in resumed:
                assert provider.restored_state is not None
                assert len(provider.results) == 1
                assert len(provider.results[0]) == 4
                writes = [
                    event
                    for event in live.store.list_task_events(provider.model)
                    if event.event_type == "tool.called"
                    and event.payload.get("tool") == "write_file"
                ]
                assert len(writes) == 2
            assert (await live.handle(_request("task.recover", {})))["outcomes"] == []
        finally:
            for signal in release:
                signal.set()
            await asyncio.wait_for(
                asyncio.gather(*backgrounds, return_exceptions=True), timeout=15
            )
            live.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("legacy_kind", ["fixture", "sessionless", "claim-only"])
def test_legacy_requests_keep_exclusive_claims(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    legacy_kind: str,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        service = service_factory(tmp_path)
        provider = ScriptedProvider("legacy", [])
        _register(service, provider)
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            credentials = await _open_session(service, repository, provider)
            blocker = await service.handle(
                _request(
                    "task.run",
                    {
                        "title": "legacy reservation",
                        "path": str(repository),
                        "scopes": ["docs/"],
                        "task_id": "blocker",
                        "claim_only": True,
                    },
                )
            )
            assert blocker["claim"]["scheduling_mode"] == "exclusive"
            params: dict[str, Any] = {
                "title": "legacy request remains exclusive",
                "path": str(repository),
                "scopes": ["docs/"],
                "task_id": "legacy",
            }
            if legacy_kind == "sessionless":
                params.update(provider=provider.name, model=provider.model)
            else:
                params.update(credentials)
                if legacy_kind == "fixture":
                    params["fixture_writes"] = ["docs/guide.md=fixture"]
                else:
                    params["claim_only"] = True
            queued = await service.handle(_request("task.run", params))
            assert queued["execution"] == "queued"
            assert queued["claim"]["scheduling_mode"] == "exclusive"
            assert queued["claim"]["workspace_id"] is None
            assert queued["claim"]["blocking_claim_ids"] == (
                blocker["claim"]["claim_id"],
            )
            assert not service._background_tasks
        finally:
            service.close()

    asyncio.run(scenario())


def test_an_older_exclusive_waiter_prevents_optimistic_queue_bypass(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        service = service_factory(tmp_path)
        staged = threading.Event()
        release = threading.Event()
        first = ScriptedProvider(
            "first",
            [
                _tools(_call("read_file", path="docs/guide.md")),
                _finish_after_release(staged, release),
            ],
        )
        later = ScriptedProvider(
            "later", [_tools(_call("read_file", path="docs/guide.md")), _finish()]
        )
        _register(service, first, later)
        service.initialize()
        backgrounds: list[asyncio.Task[None]] = []
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            sessions = [
                await _open_session(service, repository, provider)
                for provider in (first, later)
            ]
            backgrounds.append(
                await _start(service, repository, sessions[0], "first", ("docs/",))
            )
            assert await asyncio.to_thread(staged.wait, 5)
            _assert_active_optimistic(service, "first", sessions[0]["session_id"])
            exclusive = await service.handle(
                _request(
                    "task.run",
                    {
                        "title": "older exclusive reservation",
                        "path": str(repository),
                        "scopes": ["docs/"],
                        "task_id": "exclusive",
                        "claim_only": True,
                    },
                )
            )
            assert exclusive["execution"] == "queued"
            assert exclusive["claim"]["scheduling_mode"] == "exclusive"
            queued = await service.handle(
                _request(
                    "task.run",
                    {
                        **sessions[1],
                        "title": "later optimistic session",
                        "path": str(repository),
                        "scopes": ["docs/"],
                        "task_id": "later",
                    },
                )
            )
            assert queued["execution"] == "queued"
            assert queued["claim"]["scheduling_mode"] == "optimistic"
            assert queued["claim"]["blocking_claim_ids"] == (
                exclusive["claim"]["claim_id"],
            )
            assert not later.history
            release.set()
            await asyncio.wait_for(backgrounds[0], timeout=10)
            _assert_completed(service, "first")
            exclusive_claim = service.store.get_claim(exclusive["claim"]["claim_id"])
            later_claim = service.store.get_claim(queued["claim"]["claim_id"])
            assert exclusive_claim is not None
            assert exclusive_claim.state is ClaimState.ACTIVE_WORK
            assert later_claim is not None and later_claim.state is ClaimState.QUEUED
            assert not later.history
            await service.handle(
                _request(
                    "claim.release",
                    {"claim_id": exclusive_claim.claim_id, "reason": "test release"},
                )
            )
            backgrounds.append(service._background_tasks[("later", 1)])
            await asyncio.wait_for(backgrounds[1], timeout=10)
            _assert_completed(service, "later")
        finally:
            release.set()
            await asyncio.wait_for(
                asyncio.gather(*backgrounds, return_exceptions=True), timeout=15
            )
            service.close()

    asyncio.run(scenario())
