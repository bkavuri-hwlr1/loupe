"""A checkout admitted at the default limit remains usable across handoffs."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_shared_agent_execution import (
    ScriptedProvider,
    _finish,
    _register,
    _request,
    _start,
)

from llm_cli.config.models import DEFAULT_ISOLATED_SOURCE_MAX_PATHS
from llm_cli.daemon.service import DaemonService
from llm_cli.workspace import authority


@pytest.mark.admission_scale
@pytest.mark.parametrize("mode", ["shared", "isolated"])
def test_tasks_after_idle_release_and_restart(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    mode: str,
) -> None:
    count = DEFAULT_ISOLATED_SOURCE_MAX_PATHS
    root = repository_factory(
        tmp_path, {f"src/{n // 1000:03}/{n:05}.py": "pass\n" for n in range(count)}
    )
    provider = ScriptedProvider("handoff", [_finish() for _ in range(3)])
    credentials = {"session_id": "handoff", "resume_secret": "handoff-test-secret"}

    async def run() -> None:
        service = service_factory(tmp_path)
        try:
            _register(service, provider)
            service.initialize()
            await service.handle(_request("repo.add", {"path": str(root)}))
            opened = await service.handle(
                _request(
                    "session.open",
                    {
                        "path": str(root),
                        "session_id": "handoff",
                        "workspace": mode,
                        "resume_token_hash": hashlib.sha256(
                            b"handoff-test-secret"
                        ).hexdigest(),
                        "provider": provider.name,
                        "model": provider.model,
                    },
                )
            )
            await service.handle(
                _request(
                    "session.ack",
                    {**credentials, "sequence": opened["bootstrap_sequence"]},
                )
            )
            for stage in ("initial", "idle", "restart"):
                observed = ("src/000/00000.py", "src/000/00001.py", "new.py")
                if stage == "idle":
                    if mode == "shared":
                        for path in observed:
                            await service.handle(
                                _request(
                                    "workspace.read_file",
                                    {**credentials, "relative_path": path},
                                )
                            )
                    service.physical_authority.release_idle(force=True)
                    assert not service.physical_authority.is_owned(root)
                    if mode == "shared":
                        (root / observed[0]).write_text("changed\n")
                        (root / observed[1]).unlink()
                        (root / observed[2]).write_text("created\n")
                        with (
                            service.physical_authority.pin(root),
                            service.store.connection() as db,
                        ):
                            seen = {
                                row[0]
                                for row in db.execute(
                                    "SELECT json_extract(payload_json, '$.path') "
                                    "FROM checkout_events "
                                    "WHERE event_type='workspace.external_changed'"
                                )
                            }
                        assert seen == set(observed)
                if stage == "restart":
                    service.close()
                    service = service_factory(tmp_path)
                    _register(service, provider)
                    service.initialize()
                    resumed = await service.handle(
                        _request("session.resume", credentials)
                    )
                    assert (
                        resumed["session"]["workspace_id"]
                        == opened["workspace"]["workspace_id"]
                    )
                worker = await _start(
                    service, root, credentials, stage, ("src/000/00000.py",)
                )
                await asyncio.wait_for(worker, timeout=180)
                task = service.store.get_task(stage)
                assert task is not None and task.state == "completed", (
                    service.store.list_task_events(stage)
                )
        finally:
            service.close()

    asyncio.run(run())


@pytest.mark.parametrize("refusal", ["count", "changing"])
def test_startup_recovery_keeps_isolated_work_when_handoff_refuses(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
) -> None:
    """A refused handoff at startup is retried later, never quarantined."""
    root = repository_factory(tmp_path, {"a.py": "pass\n", "b.py": "pass\n"})
    provider = ScriptedProvider("startup", [_finish() for _ in range(2)])
    credentials = {"session_id": "startup", "resume_secret": "startup-test-secret"}

    def restart() -> DaemonService:
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        return service

    async def open_and_run(service: DaemonService) -> str:
        await service.handle(_request("repo.add", {"path": str(root)}))
        opened = await service.handle(
            _request(
                "session.open",
                {
                    "path": str(root),
                    "session_id": "startup",
                    "workspace": "isolated",
                    "resume_token_hash": hashlib.sha256(
                        b"startup-test-secret"
                    ).hexdigest(),
                    "provider": provider.name,
                    "model": provider.model,
                },
            )
        )
        await service.handle(
            _request(
                "session.ack",
                {**credentials, "sequence": opened["bootstrap_sequence"]},
            )
        )
        worker = await _start(service, root, credentials, "before", ("a.py",))
        await asyncio.wait_for(worker, timeout=60)
        return str(opened["workspace"]["workspace_id"])

    service = restart()
    try:
        workspace_id = asyncio.run(open_and_run(service))
    finally:
        service.close()

    with monkeypatch.context() as patch:
        if refusal == "count":
            patch.setattr(authority, "HANDOFF_MAX_PATHS", 1)
        else:
            # Source keeps changing between the handoff's two listings.
            real = authority.checkpoint_source_paths
            calls = 0

            def changing(path: Path, **options: Any) -> tuple[str, ...]:
                nonlocal calls
                calls += 1
                paths = real(path, **options)
                return paths if calls % 2 else (*paths, "arrived.py")

            patch.setattr(authority, "checkpoint_source_paths", changing)
        service = restart()
        try:
            workspace = service.store.get_workspace(workspace_id)
            assert workspace is not None and workspace.state != "quarantined"
        finally:
            service.close()

    async def resume_and_run(service: DaemonService) -> None:
        await service.handle(_request("session.resume", credentials))
        worker = await _start(service, root, credentials, "after", ("a.py",))
        await asyncio.wait_for(worker, timeout=60)
        task = service.store.get_task("after")
        assert task is not None and task.state == "completed"

    service = restart()
    try:
        asyncio.run(resume_and_run(service))
    finally:
        service.close()
