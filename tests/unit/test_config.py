from __future__ import annotations

from pathlib import Path

import pytest

from llm_cli.config.loader import load_settings
from llm_cli.errors import LlmCoordError


def test_missing_config_uses_safe_defaults(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.toml", profile_id="test")
    assert settings.profile_id == "test"
    assert settings.coordination_mode == "enforce"
    assert settings.agent_provider == "anthropic"
    assert settings.agent_model is None
    assert settings.launch_lease_ms == 600_000
    assert settings.work_lease_ms == 90_000


def test_config_loads_bounded_supported_settings(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[core]
coordination_mode = "observe"

[agent]
provider = "test-provider"
model = "test-model"

[leases]
launch_ms = 120000
work_ms = 60000
renewal_interval_ms = 20000
""",
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.coordination_mode == "observe"
    assert settings.agent_provider == "test-provider"
    assert settings.agent_model == "test-model"
    assert settings.launch_lease_ms == 120_000
    assert settings.work_lease_ms == 60_000
    assert settings.renewal_interval_ms == 20_000


def test_config_rejects_unknown_or_unsafe_values(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[core]\nprovider_secret = 'nope'\n", encoding="utf-8")
    with pytest.raises(LlmCoordError, match="unknown core"):
        load_settings(config)
    config.write_text(
        "[leases]\nwork_ms = 1000\nrenewal_interval_ms = 1000\n",
        encoding="utf-8",
    )
    with pytest.raises(LlmCoordError, match="shorter"):
        load_settings(config)


def test_commands_ask_by_default_and_accept_allow_or_off(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "missing.toml").agent_commands == "ask"
    config = tmp_path / "config.toml"
    for value in ("allow", "off"):
        config.write_text(f'[agent]\ncommands = "{value}"\n', encoding="utf-8")
        assert load_settings(config).agent_commands == value
    config.write_text('[agent]\ncommands = "unsandboxed"\n', encoding="utf-8")
    with pytest.raises(LlmCoordError, match="ask, allow, or off"):
        load_settings(config)
