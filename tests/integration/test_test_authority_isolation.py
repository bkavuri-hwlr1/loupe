"""Negative control for missing constructor injection, using a temporary account."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from authority_guard import AuthorityRootGuard
from authority_helpers import isolated_autostart_popen, isolated_cli_arguments

from llm_cli.config.models import Settings
from llm_cli.daemon.service import DaemonService
from llm_cli.paths import AppPaths
from llm_cli.protocol.envelopes import Request
from llm_cli.workspace import authority


def test_missing_injection_is_detected_without_touching_the_real_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository_factory: Any
) -> None:
    account = tmp_path / "test-account"
    account.mkdir()
    monkeypatch.setattr(
        authority.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(account))
    )
    watched = authority.account_authority_root()
    guard = AuthorityRootGuard.capture(watched)
    repository = repository_factory(tmp_path, {"README.md": "base\n"})
    paths = AppPaths.resolve(
        "missing-injection",
        environ={
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home=tmp_path,
    )
    # Deliberately omit authority_root, exactly like a daemon that missed the
    # test bootstrap. Only the account lookup is substituted in this control.
    service = DaemonService(
        paths, Settings(profile_id=paths.profile_id), asyncio.Event(), boot_id="guard"
    )
    try:
        service.initialize()
        assert service.physical_authority.root == watched
        asyncio.run(
            service.handle(
                Request.create(
                    request_id="register-negative-control",
                    method="repo.add",
                    profile_id=paths.profile_id,
                    params={"path": str(repository)},
                )
            )
        )
        asyncio.run(
            service.handle(
                Request.create(
                    request_id="missing-injection",
                    method="task.run",
                    profile_id=paths.profile_id,
                    params={
                        "path": str(repository),
                        "title": "guard negative control",
                        "scopes": ["README.md"],
                        "claim_only": True,
                    },
                )
            )
        )
        assert any((watched / "checkouts").iterdir())
        with pytest.raises(AssertionError, match="real account authority root"):
            guard.assert_unchanged()
    finally:
        service.close()


def test_autostart_adapter_preserves_isolation_and_unrelated_commands() -> None:
    seen: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
    sentinel = object()

    def start(command: Any, *args: Any, **kwargs: Any) -> object:
        seen.append((command, args, kwargs))
        return sentinel

    adapted = isolated_autostart_popen(start)
    options = {"cwd": "private-runtime", "env": {"PYTHONPATH": "untrusted"}}
    command = ["python", "-I", "-u", "-m", "llm_cli.daemon.main", "--profile", "test"]
    assert adapted(command, **options) is sentinel
    routed, args, kwargs = seen[-1]
    assert routed[:3] == ["python", "-I", "-u"]
    assert Path(routed[3]).is_absolute()
    assert Path(routed[3]).name == "authority_helpers.py"
    assert routed[4:] == ["--profile", "test"]
    assert command[3:5] == ["-m", "llm_cli.daemon.main"]
    assert args == () and kwargs == options
    unrelated = ["python", "-I", "-m", "another.module"]
    adapted(unrelated, **options)
    assert seen[-1][0] is unrelated


def test_cli_bootstrap_covers_fallback_autostart_and_preserves_daemon_seams() -> None:
    arguments = ["-m", "llm_cli", "--profile", "test", "chat"]
    routed = isolated_cli_arguments(arguments)
    assert routed[0] == "-I"
    assert Path(routed[1]).is_absolute()
    assert routed[2:] == ["--test-cli", "--profile", "test", "chat"]
    daemon = ["-c", "from test_cli_modes import _daemon_entry; _daemon_entry()"]
    assert isolated_cli_arguments(daemon) is daemon
