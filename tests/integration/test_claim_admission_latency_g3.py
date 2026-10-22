"""Six real clients must fit the normal RPC budget without redundant regime work."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from llm_cli.config.models import Settings
from llm_cli.daemon.service import DaemonService
from llm_cli.git.inspect import inspect_repository
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient
from llm_cli.protocol.server import RpcServer


@pytest.mark.admission_scale
def test_six_claim_only_rpc_requests_fit_default_timeout_with_slow_regime_observation(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disjoint exclusive claims mirror nonconflicting optimistic preparation.

    The model/provider seam is never invoked. Slowing only the new observation
    boundary makes quadratic redundant checks deterministic without imposing a
    benchmark on Git spawn speed or changing the five-second product timeout.
    """
    repository = repository_factory(tmp_path, {"source.txt": "base\n"})
    with tempfile.TemporaryDirectory(prefix="lg3-rpc-", dir="/tmp") as runtime:
        paths = AppPaths.resolve(
            "test",
            environ={
                "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
                "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
                "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
                "LLM_COORD_RUNTIME_DIR": str(Path(runtime).resolve()),
            },
            home=tmp_path,
        )
        service = DaemonService(
            paths,
            Settings(profile_id="test"),
            asyncio.Event(),
            boot_id="latency-probe",
            authority_root=tmp_path / "authority" / "v1",
        )
        service.initialize()
        info = inspect_repository(repository, profile_id="test")
        registered = service.store.register_repository(
            repo_key=info.repo_key,
            display_name="latency-probe",
            git_common_dir=str(info.common_git_dir),
            main_worktree_path=str(repository),
            target_ref=info.target_ref,
            profile_id="test",
        )
        checkout = service.store.ensure_checkout(
            repository=registered,
            canonical_path=str(repository),
            git_common_dir=str(info.common_git_dir),
        )
        with service.physical_authority.pin(repository):
            pass
        workspace = service.store.ensure_shared_workspace(checkout)
        credentials = [
            (f"session-{index}", f"synthetic-resume-{index}") for index in range(6)
        ]
        for session_id, secret in credentials:
            _, _, sequence = service.store.open_session(
                session_id=session_id,
                checkout_id=checkout.checkout_id,
                resume_token_hash=hashlib.sha256(secret.encode()).hexdigest(),
                provider="unused-offline",
                model="offline",
                workspace_id=workspace.workspace_id,
            )
            service.store.acknowledge_session(session_id, sequence=sequence)
        original = service.physical_authority.regime
        inspections = 0

        def slow_observation(path: Path) -> dict[str, Any]:
            nonlocal inspections
            inspections += 1
            time.sleep(0.075)
            return original(path)

        monkeypatch.setattr(service.physical_authority, "regime", slow_observation)
        clients = [DaemonClient(paths) for _ in range(6)]
        assert all(client.timeout_seconds == 5.0 for client in clients)
        barrier = threading.Barrier(len(clients))

        def submit(index: int) -> dict[str, Any]:
            barrier.wait(timeout=5)
            started = time.monotonic()
            try:
                response = clients[index].call(
                    "task.run",
                    {
                        "path": str(repository),
                        "task_id": f"latency-{index}",
                        "title": "synthetic",
                        "scopes": [f"file-{index}.txt"],
                        "claim_only": True,
                        "session_id": credentials[index][0],
                        "resume_secret": credentials[index][1],
                    },
                    autostart=False,
                )
                return {
                    "worker": index,
                    "ok": True,
                    "task_id": response["task"]["task_id"],
                    "seconds": round(time.monotonic() - started, 3),
                }
            except BaseException as error:
                return {
                    "worker": index,
                    "ok": False,
                    "error": type(error).__name__,
                    "code": str(getattr(error, "code", None)),
                    "allocated": service.store.get_task(f"latency-{index}") is not None,
                    "seconds": round(time.monotonic() - started, 3),
                }

        async def run() -> list[dict[str, Any]]:
            server = RpcServer(paths.socket, paths.profile_id, service.handle)
            await server.start()
            try:
                return list(
                    await asyncio.gather(
                        *(
                            asyncio.to_thread(submit, index)
                            for index in range(len(clients))
                        )
                    )
                )
            finally:
                await server.close()

        try:
            started = time.monotonic()
            outcomes = asyncio.run(run())
            evidence = {
                "regime_inspections": inspections,
                "outcomes": outcomes,
                "seconds": round(time.monotonic() - started, 3),
            }
            (tmp_path / "claim-admission-evidence.json").write_text(
                json.dumps(evidence, indent=2)
            )
            assert all(item["ok"] for item in outcomes), evidence
            assert len({item["task_id"] for item in outcomes}) == len(clients)
            assert all(
                task.current_claim_id is not None for task in service.store.list_tasks()
            )
        finally:
            service.close()
