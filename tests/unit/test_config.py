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


def test_explore_is_on_by_default_and_can_be_turned_off(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "missing.toml").agent_explore is True
    config = tmp_path / "config.toml"
    config.write_text("[agent]\nexplore = false\n", encoding="utf-8")
    assert load_settings(config).agent_explore is False
    config.write_text('[agent]\nexplore = "no"\n', encoding="utf-8")
    with pytest.raises(LlmCoordError, match="must be true or false"):
        load_settings(config)


def test_explore_effort_defaults_to_low_and_accepts_task_or_a_level(
    tmp_path: Path,
) -> None:
    assert load_settings(tmp_path / "missing.toml").agent_explore_effort == "low"
    config = tmp_path / "config.toml"
    config.write_text('[agent]\nexplore_effort = "medium"\n', encoding="utf-8")
    assert load_settings(config).agent_explore_effort == "medium"
    config.write_text('[agent]\nexplore_effort = "task"\n', encoding="utf-8")
    assert load_settings(config).agent_explore_effort is None
    for value in ('"lowest"', "1"):
        config.write_text(f"[agent]\nexplore_effort = {value}\n", encoding="utf-8")
        with pytest.raises(LlmCoordError, match="or an effort level"):
            load_settings(config)
