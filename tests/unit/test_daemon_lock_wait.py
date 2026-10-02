"""Restart and autostart wait for a stopping daemon instead of racing its lock."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

import pytest

from llm_cli.cli import app
from llm_cli.daemon.main import SingletonLock
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol import client as client_module
from llm_cli.protocol.client import DaemonClient, _lock_held


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    result = AppPaths(
        "test",
        tmp_path / "config",
        tmp_path / "data",
        tmp_path / "state",
        tmp_path / "run",
    )
    result.ensure()
    return result


def test_lock_probe_sees_a_live_owner_without_taking_the_lock(paths: AppPaths) -> None:
    assert not _lock_held(paths.lock_file)
    owner = SingletonLock(paths.lock_file)
    owner.acquire()
    try:
        assert _lock_held(paths.lock_file)
        assert _lock_held(paths.lock_file)
    finally:
        owner.close()
    assert not _lock_held(paths.lock_file)
    successor = SingletonLock(paths.lock_file)
    successor.acquire()
    successor.close()


def test_wait_until_stopped_returns_when_the_old_daemon_exits(paths: AppPaths) -> None:
    owner = SingletonLock(paths.lock_file)
    owner.acquire()
    client = DaemonClient(paths)
    assert not client.wait_until_stopped(0.2)
    timer = threading.Timer(0.3, owner.close)
    timer.start()
    try:
        assert client.wait_until_stopped(5)
    finally:
        timer.join()


def test_autostart_does_not_spawn_while_a_stopping_daemon_owns_the_profile(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_spawn(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a successor would be refused by the singleton lock")

    monkeypatch.setattr(client_module.subprocess, "Popen", no_spawn)
    owner = SingletonLock(paths.lock_file)
    owner.acquire()
    try:
        with pytest.raises(LlmCoordError, match="still stopping") as raised:
            DaemonClient(paths, timeout_seconds=0.2).call("system.ping")
        assert raised.value.code is ErrorCode.DAEMON_UNAVAILABLE
    finally:
        owner.close()


class RestartClient:
    def __init__(self, paths: AppPaths, *, stops: bool) -> None:
        self.paths = paths
        self.stops = stops
        self.calls: list[str] = []
        self.waited: list[float] = []

    def call(self, method: str, params: object = None, **kwargs: Any) -> Any:
        self.calls.append(method)
        return {"ready": True} if method == "system.ping" else {"stopping": True}

    def wait_until_stopped(self, timeout_seconds: float) -> bool:
        self.waited.append(timeout_seconds)
        self.calls.append("wait")
        return self.stops


def test_restart_waits_for_the_old_daemon_before_starting_a_new_one(
    paths: AppPaths,
) -> None:
    client = RestartClient(paths, stops=True)
    args = app.build_parser().parse_args(["daemon", "restart"])
    status = app.dispatch(args, cast(DaemonClient, client))
    assert client.calls == ["system.shutdown", "wait", "system.ping"]
    # Long enough for the default shutdown drain of running tasks.
    assert client.waited[0] > 10
    assert status["ready"] is True


def test_restart_reports_an_old_daemon_that_does_not_exit(paths: AppPaths) -> None:
    client = RestartClient(paths, stops=False)
    args = app.build_parser().parse_args(["daemon", "restart"])
    with pytest.raises(LlmCoordError, match="has not exited yet"):
        app.dispatch(args, cast(DaemonClient, client))
    assert client.calls == ["system.shutdown", "wait"]
