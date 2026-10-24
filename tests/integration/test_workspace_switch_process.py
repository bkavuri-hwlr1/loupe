"""Real daemon deaths use a disposable injected authority and isolated Python."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient

_DAEMON = """
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location('authority_helpers', sys.argv[1])
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
helper.install_test_authority()
from llm_cli.daemon import main
original = main.DaemonService
class Adapter:
    name = 'switch-fixture'
    def __init__(self, model): self.model = model
class Service(original):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.providers.register('switch-fixture', lambda model: Adapter(model))
        def fault(state):
            if state == os.environ.get('SWITCH_CRASH_PHASE'):
                os._exit(73)
        self.workspace_switches.fault = fault
main.DaemonService = Service
main.main(['--profile', 'test'])
"""


def _ready(client: DaemonClient, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        assert process.poll() is None, "daemon exited before readiness"
        try:
            client.call("system.ping", autostart=False)
            return
        except LlmCoordError:
            time.sleep(0.05)
    raise AssertionError("test daemon did not become ready")


@pytest.mark.parametrize(
    "phase",
    [
        "requested",
        "prepared",
        "target_creating",
        "transferring",
        "confirming",
        "confirmed",
    ],
)
def test_killed_daemon_replays_exact_switch_and_target_bootstrap(
    tmp_path: Path,
    repository_factory: Any,
    phase: str,
) -> None:
    root = repository_factory(tmp_path, {"a.txt": "unchanged\n"})
    with tempfile.TemporaryDirectory(prefix="lc-switch-", dir="/tmp") as runtime:
        environment = {
            **os.environ,
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(Path(runtime).resolve()),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        paths = AppPaths.resolve("test", environ=environment)
        client = DaemonClient(paths, timeout_seconds=15)
        processes = []
        helper = Path(__file__).with_name("authority_helpers.py")

        def spawn(crash: str) -> subprocess.Popen[str]:
            process = subprocess.Popen(
                [sys.executable, "-I", "-u", "-c", _DAEMON, str(helper)],
                cwd=root,
                env={**environment, "SWITCH_CRASH_PHASE": crash},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            processes.append(process)
            _ready(client, process)
            return process

        credentials = {
            "session_id": "process-switch",
            "resume_secret": "disposable-switch-secret",
        }
        request = {
            **credentials,
            "switch_intent_id": "durable",
            "workspace": "isolated",
        }
        try:
            first = spawn(phase)
            client.call("repo.add", {"path": str(root)}, autostart=False)
            opened = client.call(
                "session.open",
                {
                    "session_id": credentials["session_id"],
                    "path": str(root),
                    "resume_token_hash": hashlib.sha256(
                        credentials["resume_secret"].encode()
                    ).hexdigest(),
                    "provider": "switch-fixture",
                    "model": "fixture",
                    "workspace": "shared",
                },
                autostart=False,
            )
            client.call(
                "session.ack",
                {**credentials, "sequence": opened["bootstrap_sequence"]},
                autostart=False,
            )
            with pytest.raises(LlmCoordError):
                prepared = client.call(
                    "workspace.switch_mode", request, autostart=False
                )
                client.call(
                    "workspace.confirm_switch",
                    {
                        **credentials,
                        "switch_intent_id": "durable",
                        "sequence": prepared["bootstrap_sequence"],
                    },
                    autostart=False,
                )
            assert first.wait(timeout=10) == 73
            restarted = spawn("")
            resumed = client.call("session.resume", credentials, autostart=False)
            if phase != "confirmed":
                assert resumed["workspace_switch"]["switch_intent_id"] == "durable"
            prepared = client.call("workspace.switch_mode", request, autostart=False)
            try:
                assert (
                    prepared["state"] == "confirming"
                    or prepared["state"] == "confirmed"
                )
                confirmed = client.call(
                    "workspace.confirm_switch",
                    {
                        **credentials,
                        "switch_intent_id": "durable",
                        "sequence": prepared["bootstrap_sequence"],
                    },
                    autostart=False,
                )
            except (LlmCoordError, AssertionError):
                rolled_back = client.call(
                    "workspace.rollback_switch",
                    {
                        **credentials,
                        "switch_intent_id": "durable",
                    },
                    autostart=False,
                )
                assert rolled_back["state"] == "rolled_back"
                assert rolled_back["session"]["workspace_mode"] == "shared"
                assert (root / "a.txt").read_text() == "unchanged\n"
                from llm_cli.storage.control import ControlStore

                with ControlStore(paths.control_db).connection() as con:
                    assert (
                        con.execute(
                            "SELECT count(*) FROM isolated_workspaces"
                        ).fetchone()[0]
                        == 1
                    )
                    assert (
                        con.execute(
                            "SELECT count(*) FROM checkout_events "
                            "WHERE event_type='session.workspace_switched'"
                        ).fetchone()[0]
                        == 0
                    )
                return
            assert confirmed["state"] == "confirmed"
            assert confirmed["session"]["workspace_binding_generation"] == 2
            private = Path(confirmed["workspace"]["canonical_path"])
            assert (private / "a.txt").read_text() == "unchanged\n"
            from llm_cli.storage.control import ControlStore

            with ControlStore(paths.control_db).connection() as con:
                assert (
                    con.execute("SELECT count(*) FROM isolated_workspaces").fetchone()[
                        0
                    ]
                    == 1
                )
                assert (
                    con.execute(
                        "SELECT count(*) FROM checkout_events WHERE "
                        "event_type='session.workspace_switched'"
                    ).fetchone()[0]
                    == 1
                )
            client.call("system.shutdown", autostart=False)
            assert restarted.wait(timeout=10) == 0
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                if process.stderr:
                    process.stderr.close()
