"""Configuration commands report the daemon's effective startup snapshot."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from llm_cli.cli.app import build_parser, dispatch
from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient


@pytest.mark.usefixtures("isolated_daemon_autostart")
def test_live_config_show_set_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with tempfile.TemporaryDirectory(prefix="lc-config-", dir="/tmp") as runtime:
        for name, path in {
            "LLM_COORD_CONFIG_HOME": tmp_path / "config",
            "LLM_COORD_DATA_HOME": tmp_path / "data",
            "LLM_COORD_STATE_HOME": tmp_path / "state",
            "LLM_COORD_RUNTIME_DIR": Path(runtime).resolve(),
        }.items():
            monkeypatch.setenv(name, str(path))
        paths = AppPaths.resolve("config-test")
        paths.ensure()
        paths.config_file.write_text('[agent]\ncommands="allow"\n')
        client = DaemonClient(paths, timeout_seconds=30)
        try:
            shown = dispatch(build_parser().parse_args(["config", "show"]), client)
            values = {item["key"]: item for item in shown["settings"]}
            assert values["agent.commands"]["value"] == "allow"
            assert values["agent.commands"]["source"] == "config file"
            assert values["profile"]["value"] == "config-test"
            assert shown["saved_pending_restart"] is False
            result = dispatch(
                build_parser().parse_args(["config", "set", "agent.commands", "off"]),
                client,
            )
            assert result["saved"] is True and result["restart_required"] is True
            shown = client.call("config.show")
            values = {item["key"]: item for item in shown["settings"]}
            assert values["agent.commands"]["value"] == "allow"
            assert shown["pending_settings"] == [
                {"key": "agent.commands", "value": "off", "source": "config file"}
            ]
            before = paths.config_file.read_bytes()
            with pytest.raises(LlmCoordError, match="ask, allow, or off"):
                client.call("config.set", {"key": "agent.commands", "value": "unsafe"})
            assert paths.config_file.read_bytes() == before
            client.call("system.shutdown", autostart=False)
            assert client.wait_until_stopped(15)
            shown = client.call("config.show")
            values = {item["key"]: item for item in shown["settings"]}
            assert values["agent.commands"]["value"] == "off"
            assert shown["saved_pending_restart"] is False
        finally:
            client.call("system.shutdown", autostart=False)
            assert client.wait_until_stopped(15)
