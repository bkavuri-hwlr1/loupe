"""Admission reuses one fresh Git discovery on each side of physical pinning."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from llm_cli.daemon import service as service_module
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.inspect import RepositoryInfo, inspect_repository
from llm_cli.protocol.envelopes import Request


def _register(service: DaemonService, info: RepositoryInfo) -> Any:
    return service.store.register_repository(
        repo_key=info.repo_key,
        display_name="discovery fixture",
        git_common_dir=str(info.common_git_dir),
        main_worktree_path=str(info.main_worktree),
        target_ref=info.target_ref,
        profile_id="test",
    )


@dataclass
class DiscoveryEnvironment:
    service: DaemonService
    root: Path
    credentials: list[tuple[str, str]]

    def request(self, index: int = 0, **overrides: Any) -> Request:
        session_id, secret = self.credentials[index]
        return Request.create(
            request_id=f"discovery-request-{index}",
            profile_id="test",
            method="task.run",
            params={
                "path": str(self.root),
                "session_id": session_id,
                "resume_secret": secret,
                "task_id": f"discovery-task-{index}",
                "title": "synthetic admission",
                "scopes": [f"file-{index}.txt"],
                "claim_only": True,
                **overrides,
            },
        )


@pytest.fixture
def discovery_environment(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[[Path], DaemonService],
) -> Iterator[DiscoveryEnvironment]:
    root = repository_factory(tmp_path, {"source.txt": "base\n"})
    service = service_factory(tmp_path)
    service.initialize()
    info = inspect_repository(root, profile_id="test")
    registered = _register(service, info)
    checkout = service.store.ensure_checkout(
        repository=registered,
        canonical_path=str(root),
        git_common_dir=str(info.common_git_dir),
    )
    with service.physical_authority.pin(root):
        pass
    workspace = service.store.ensure_shared_workspace(checkout)
    credentials = [
        (f"discovery-session-{index}", f"synthetic-discovery-resume-{index}")
        for index in range(6)
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
    try:
        yield DiscoveryEnvironment(service, root, credentials)
    finally:
        service.close()


def test_six_authenticated_admissions_use_two_discoveries_and_keep_all_regime_guards(
    discovery_environment: DiscoveryEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = discovery_environment
    actual_inspect = service_module.inspect_repository
    actual_regime = env.service.physical_authority.regime
    discoveries: list[RepositoryInfo] = []
    regimes: list[dict[str, Any]] = []

    def inspected(*args: Any, **kwargs: Any) -> RepositoryInfo:
        result = actual_inspect(*args, **kwargs)
        discoveries.append(result)
        return result

    def observed(path: Path) -> dict[str, Any]:
        result = actual_regime(path)
        regimes.append(result)
        return result

    monkeypatch.setattr(service_module, "inspect_repository", inspected)
    monkeypatch.setattr(env.service.physical_authority, "regime", observed)

    async def run() -> tuple[list[int], list[int]]:
        discovery_counts, regime_counts = [], []
        for index in range(6):
            before_discovery, before_regime = len(discoveries), len(regimes)
            response = await env.service.handle(env.request(index))
            assert response["claim"]["state"] == "active_work"
            discovery_counts.append(len(discoveries) - before_discovery)
            regime_counts.append(len(regimes) - before_regime)
        return discovery_counts, regime_counts

    discovery_counts, regime_counts = asyncio.run(run())
    assert discovery_counts == [2] * 6
    assert regime_counts == [3] * 6
    assert len(discoveries) == 12 and len(regimes) == 18
    assert len(env.service.store.list_tasks()) == 6
    assert all(result.worktree == env.root for result in discoveries)


@pytest.mark.parametrize("invalid", [{"title": ""}, {"task_id": 42}])
def test_malformed_admission_still_creates_no_task_or_claim(
    discovery_environment: DiscoveryEnvironment, invalid: dict[str, Any]
) -> None:
    env = discovery_environment
    with pytest.raises(LlmCoordError) as refused:
        asyncio.run(env.service.handle(env.request(**invalid)))
    assert refused.value.code is ErrorCode.CONFIG_INVALID
    assert env.service.store.list_tasks() == ()
    with env.service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


def test_after_pin_discovery_refuses_a_newly_ambiguous_target(
    discovery_environment: DiscoveryEnvironment,
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = discovery_environment
    git_run(env.root, "branch", "registered-alternative")
    git_run(env.root, "branch", "unregistered-target")
    _register(
        env.service,
        inspect_repository(
            env.root, profile_id="test", target="registered-alternative"
        ),
    )
    actual_inspect = service_module.inspect_repository
    actual_pin = env.service.physical_authority.pin
    targets: list[str] = []
    changed = False

    def inspected(*args: Any, **kwargs: Any) -> RepositoryInfo:
        result = actual_inspect(*args, **kwargs)
        targets.append(result.target_ref)
        return result

    @contextmanager
    def switch_after_pin(*args: Any, **kwargs: Any) -> Iterator[Any]:
        nonlocal changed
        with actual_pin(*args, **kwargs) as pin:
            if not changed:
                git_run(env.root, "switch", "unregistered-target")
                changed = True
            yield pin

    monkeypatch.setattr(service_module, "inspect_repository", inspected)
    monkeypatch.setattr(env.service.physical_authority, "pin", switch_after_pin)
    with pytest.raises(LlmCoordError) as refused:
        asyncio.run(env.service.handle(env.request()))
    assert refused.value.code is ErrorCode.REPOSITORY_NOT_FOUND
    assert refused.value.details == {
        "registered_targets": [
            "refs/heads/main",
            "refs/heads/registered-alternative",
        ]
    }
    assert targets == ["refs/heads/main", "refs/heads/unregistered-target"]
    assert env.service.store.list_tasks() == ()
