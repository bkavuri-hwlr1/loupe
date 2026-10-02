"""Restarting while a model request hangs replaces the daemon promptly.

A hung provider request used to keep the stopped daemon's process alive, still
writing task state, while `loupe daemon restart` raced its singleton lock and
failed. Only the model is scripted; the daemon, CLI, and RPC are real processes.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient, _lock_held

_HANG_SECONDS = 30
_DRAIN_MS = 300


class _HangingModel:
    name = "hang"

    def __init__(self, model: str) -> None:
        self.model = model

    def session(self, **kwargs: object) -> _HangingModel:
        return self

    def snapshot(self) -> dict[str, object]:
        return {}

    def send_user(self, text: str) -> Any:
        Path(os.environ["HANG_STARTED"]).touch()
        time.sleep(_HANG_SECONDS)
        raise RuntimeError("the provider request timed out")

    def send_tool_results(self, results: Sequence[Any]) -> Any:
        return self.send_user("")

    def record_tool_results(self, results: Sequence[Any]) -> None:
        pass


def _daemon_entry() -> None:
    """Install the hanging model and a short drain, then run the real daemon."""
    from llm_cli.config.models import Settings
    from llm_cli.daemon import main

    original = main.DaemonService

    class Service(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.providers.register("hang", lambda model: _HangingModel(model))

    main.DaemonService = Service
    main.load_settings = lambda path, *, profile_id: Settings(
        profile_id=profile_id, shutdown_drain_ms=_DRAIN_MS
    )
    main.main(["--profile", "test"])


@pytest.fixture
def environment(tmp_path: Path) -> Iterator[dict[str, str]]:
    # Keep the Unix socket below the platform's short pathname limit.
    with tempfile.TemporaryDirectory(prefix="lc-restart-", dir="/tmp") as runtime:
        yield {
            **os.environ,
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(Path(runtime).resolve()),
            "HANG_STARTED": str(tmp_path / "hang-started"),
            "PYTHONPATH": os.pathsep.join(
                (str(Path(__file__).parent), str(Path(__file__).parents[2] / "src"))
            ),
            "PYTHONUNBUFFERED": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }


def _wait(predicate: Any, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_restart_during_a_hung_model_request_replaces_the_daemon(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["commit", "-q", "--allow-empty", "-m", "base"],
    ):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
            cwd=repository,
            env=environment,
            check=True,
        )
    paths = AppPaths.resolve("test", environ=environment)
    client = DaemonClient(paths)
    processes: list[subprocess.Popen[str]] = []

    def spawn(*arguments: str) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, *arguments],
            env=environment,
            cwd=repository,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        processes.append(process)
        return process

    old = spawn("-c", "from test_daemon_restart import _daemon_entry; _daemon_entry()")
    try:
        _wait(paths.socket.exists)
        chat = spawn(
            "-m",
            "llm_cli",
            "--profile",
            "test",
            "chat",
            "--plain",
            "--repo",
            str(repository),
            "--provider",
            "hang",
            "--model",
            "m",
        )
        assert chat.stdin is not None
        chat.stdin.write("What does this repo do\n")
        chat.stdin.flush()
        _wait(Path(environment["HANG_STARTED"]).exists)

        started = time.monotonic()
        restart = subprocess.run(
            [
                sys.executable,
                "-m",
                "llm_cli",
                "--profile",
                "test",
                "--json",
                "daemon",
                "restart",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert restart.returncode == 0, restart.stdout + restart.stderr
        status = json.loads(restart.stdout)
        assert status["ready"] is True and status["matches_this_cli"] is True
        # The stopped daemon exits after its drain, not when the request ends.
        _wait(lambda: old.poll() is not None, timeout=10)
        assert time.monotonic() - started < _HANG_SECONDS / 2

        events = [
            json.loads(line)["event"]
            for line in paths.log_file.read_text().splitlines()
        ]
        assert events.index("daemon.abandoned_executions") < events.index(
            "daemon.stopped"
        )
        assert events.count("daemon.ready") == 2
        assert _lock_held(paths.lock_file)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        with contextlib.suppress(LlmCoordError):
            client.call("system.shutdown", autostart=False)
        _wait(lambda: not _lock_held(paths.lock_file), timeout=20)
