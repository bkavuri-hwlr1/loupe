"""A checkout admitted at the default limit remains usable across handoffs."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path

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
