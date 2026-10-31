"""Real isolated flows obey configured count limits at every source boundary."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from llm_cli.config.models import Settings
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.envelopes import Request
from llm_cli.workspace.hydration import HydrationBudgets


def service_at(
    tmp_path: Path, *, source: int | None = None, entries: int | None = None
) -> DaemonService:
    paths = AppPaths.resolve(
        "admission-test",
        environ={
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home=tmp_path,
    )
    limits: dict[str, int] = {}
    if source is not None:
        limits["isolated_source_max_paths"] = source
    if entries is not None:
        limits["isolated_hydration_max_entries"] = entries
    service = DaemonService(
        paths,
        Settings(profile_id=paths.profile_id, **limits),
        asyncio.Event(),
        boot_id="admission-settings",
        authority_root=tmp_path / "authority" / "v1",
    )
    service.providers.register(
        "unused-scripted",
        lambda model: SimpleNamespace(name="unused-scripted", model=model),
    )
    service.initialize()
    return service


async def rpc(service: DaemonService, method: str, **params: Any) -> Any:
    return await service.handle(
        Request.create(
            request_id="admission-settings-" + method,
            method=method,
            params=params,
            profile_id=service.paths.profile_id,
        )
    )


async def open_isolated(
    service: DaemonService, root: Path, *, approved: tuple[str, ...] = ()
) -> Any:
    await rpc(service, "repo.add", path=str(root))
    result = await rpc(
        service,
        "session.open",
        path=str(root),
        session_id="isolated",
        resume_token_hash=hashlib.sha256(b"admission-settings-resume").hexdigest(),
        provider="unused-scripted",
        model="unused",
        workspace="isolated",
        approved_runtime_files=list(approved),
    )
    await rpc(
        service,
        "session.ack",
        session_id="isolated",
        resume_secret="admission-settings-resume",
        sequence=result["bootstrap_sequence"],
    )
    return result


@pytest.mark.parametrize("count", [2, 3])
def test_real_open_uses_the_configured_source_count_boundary(
    tmp_path: Path, repository_factory: Callable[..., Path], count: int
) -> None:
    root = repository_factory(tmp_path, {f"{n}.py": "pass\n" for n in range(count)})
    service = service_at(tmp_path, source=2, entries=20)
    try:
        if count == 3:
            with pytest.raises(LlmCoordError, match=r"isolated\.source_max_paths=2"):
                asyncio.run(open_isolated(service, root))
            with service.store.connection() as connection:
                assert (
                    connection.execute(
                        "SELECT count(*) FROM isolated_workspaces"
                    ).fetchone()[0]
                    == 0
                )
        else:
            opened = asyncio.run(open_isolated(service, root))
            workspace_id = opened["session"]["workspace_id"]
            service.shared_runner._prepare_isolated(workspace_id)
            report = service.shared_runner.isolated_sync.synchronize(workspace_id)
            assert not report.deferred and not report.conflicted
            assert (
                len(service.isolated_workspaces.get(workspace_id).source_manifest) == 2
            )
    finally:
        service.close()


def test_real_open_names_the_configured_hydration_entry_boundary(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root = repository_factory(tmp_path, {"a.py": "pass\n", "b.py": "pass\n"})
    service = service_at(tmp_path, source=2, entries=2)
    try:
        with pytest.raises(LlmCoordError, match=r"isolated\.hydration_max_entries=2"):
            asyncio.run(open_isolated(service, root))
    finally:
        service.close()


def test_refresh_and_switch_preserve_the_configured_admission_limits(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.workspace import runtime_refresh, switching

    root = repository_factory(
        tmp_path,
        {"a.py": "pass\n", "b.py": "pass\n", ".gitignore": "runtime/\n"},
    )
    (root / "runtime").mkdir()
    (root / "runtime/cache").write_text("runtime-before\n")
    service = service_at(tmp_path, source=3, entries=20)
    try:
        opened = asyncio.run(open_isolated(service, root))
        workspace_id = opened["session"]["workspace_id"]
        private = Path(opened["workspace"]["canonical_path"])
        service.shared_runner._prepare_isolated(workspace_id)
        # Model a generic caller's smaller fallback. The configured budget must
        # override it; omitting either propagation produces a real refusal.
        monkeypatch.setattr(
            runtime_refresh,
            "plan_hydration",
            partial(
                runtime_refresh.plan_hydration, budgets=HydrationBudgets(max_entries=1)
            ),
        )
        (root / "runtime/cache").write_text("runtime-after\n")
        notices = service.isolated_workspaces.runtime.refresh(workspace_id)
        assert "Runtime refreshed: runtime/cache" in notices
        assert (private / "runtime/cache").read_text() == "runtime-after\n"
        monkeypatch.setattr(
            switching, "capture_source", partial(switching.capture_source, max_paths=1)
        )
        result = service.workspace_switches.request(
            "isolated", "admission-switch-back", "shared", disposition="discard"
        )
        assert result["state"] == "confirming"
        done = service.workspace_switches.confirm(
            "isolated", "admission-switch-back", result["bootstrap_sequence"]
        )
        assert done["session"]["workspace_mode"] == "shared"
    finally:
        service.close()


@pytest.mark.admission_scale
@pytest.mark.parametrize("unknown", [False, True])
def test_large_process_workspace_scrubs_private_values_after_restart(
    tmp_path: Path, repository_factory: Callable[..., Path], unknown: bool
) -> None:
    import json

    from llm_cli.agent.source_policy import (
        contains_secret_material,
        forget_source_private_values,
    )
    from llm_cli.workspace.isolated import IsolatedWorkspaceManager

    canary = "private violet maple fixture 483526"
    assert not contains_secret_material(canary)
    count = Settings().isolated_source_max_paths
    root = repository_factory(
        tmp_path,
        {
            ".gitignore": ".env\nbuild/\n",
            **{
                f"source/{n // 1000:03}/{n:05}.py": "pass\n"
                for n in range(count - 1)
            },
        },
    )
    (root / ".env").write_text(canary)
    service = service_at(tmp_path)
    try:
        before_open = time.perf_counter()
        opened = asyncio.run(open_isolated(service, root, approved=(".env",)))
        open_seconds = time.perf_counter() - before_open
        workspace_id = opened["session"]["workspace_id"]
        workspace = service.store.get_workspace(workspace_id)
        assert workspace is not None and workspace.git_dir is not None
        private = Path(workspace.canonical_path)
        admin = Path(workspace.git_dir)
        manager = service.isolated_workspaces
        state = manager.get(workspace_id)
        assert state is not None and len(state.source_manifest) == count
        service.shared_runner._prepare_isolated(workspace_id)
        safe_samples: list[float] = []
        for _ in range(6):
            before_safe = time.perf_counter()
            report = service.shared_runner.isolated_sync.synchronize(workspace_id)
            safe_samples.append(time.perf_counter() - before_safe)
            assert not report.deferred and not report.conflicted
        manager.remember_process_policy(workspace)
        policy = json.loads((admin / "loupe-process-policy.json").read_text())
        # The optional generated-output classifier deliberately retains its 20k
        # bound. That fallback cannot prevent required private-value scrubbing.
        assert policy["eligible"] is False
        manager.remember_process_values(workspace, (canary,))
        private_copy = private / f"source/{(count - 2) // 1000:03}/generated-private.py"
        private_copy.write_text(canary)
        if unknown:
            (private / "build").mkdir()
            (private / "build/ordinary.out").write_text("preserve unclassified output")
        forget_source_private_values(private)
        restarted = IsolatedWorkspaceManager(
            service.store,
            manager.managed_root,
            manager.candidate_dir,
            manager.lock,
            lambda _: None,
            source_max_paths=Settings().isolated_source_max_paths,
            hydration_max_entries=Settings().isolated_hydration_max_entries,
        )
        restarted.validate(workspace_id)
        service.store.close_session("isolated")
        before_close = time.perf_counter()
        notices = restarted.cleanup_closed("isolated")
        close_seconds = time.perf_counter() - before_close
        assert not (private / ".env").exists()
        assert not private_copy.exists()
        assert not (admin / "loupe-process-private").exists(), notices
        if unknown:
            assert (
                private / "build/ordinary.out"
            ).read_text() == "preserve unclassified output"
        else:
            assert not private.exists()
        restarted.recover()
        assert not (private / ".env").exists()
        assert not (admin / "loupe-process-private").exists(), notices
        assert all(
            canary.encode() not in path.read_bytes()
            for path in service.paths.data_dir.glob("*.sqlite3*")
        )
        print(
            json.dumps(
                {
                    "default_source_paths": count,
                    "default_hydration_entries": (
                        Settings().isolated_hydration_max_entries
                    ),
                    "preserved_unknown_output": unknown,
                    "open_seconds": open_seconds,
                    "safe_point_cold_seconds": safe_samples[0],
                    "safe_point_warm_seconds": safe_samples[1:],
                    "close_seconds": close_seconds,
                    "private_values_retired": not (
                        admin / "loupe-process-private"
                    ).exists(),
                    "private_root_removed": not private.exists(),
                }
            ),
            flush=True,
        )
    finally:
        service.close()


@pytest.mark.admission_scale
def test_one_source_path_above_the_default_is_refused_with_its_setting(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    count = Settings().isolated_source_max_paths
    root = repository_factory(
        tmp_path,
        {f"source/{n // 1000:03}/{n:05}.py": "pass\n" for n in range(count + 1)},
    )
    service = service_at(tmp_path)
    try:
        with pytest.raises(
            LlmCoordError, match=rf"isolated\.source_max_paths={count}"
        ):
            asyncio.run(open_isolated(service, root))
        with service.store.connection() as connection:
            assert connection.execute(
                "SELECT count(*) FROM isolated_workspaces"
            ).fetchone()[0] == 0
    finally:
        service.close()
