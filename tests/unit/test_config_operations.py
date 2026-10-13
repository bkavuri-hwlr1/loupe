from __future__ import annotations

import json
import stat
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from llm_cli.config import operations
from llm_cli.config.loader import load_settings
from llm_cli.config.models import Settings
from llm_cli.config.operations import (
    effective_config,
    load_settings_snapshot,
    set_config_value,
)
from llm_cli.errors import ErrorCode, LlmCoordError


@pytest.fixture
def config(tmp_path: Path) -> Path:
    parent = tmp_path / "config"
    parent.mkdir(mode=0o700)
    return parent / "config.toml"


def test_sources_freeze_with_active_settings_and_saved_changes_are_separate(
    config: Path,
) -> None:
    config.write_text('[agent]\ncommands = "allow"\n')
    active, sources = load_settings_snapshot(config, profile_id="fixture")
    set_config_value(config, profile_id="fixture", key="agent.commands", value="off")
    shown = effective_config(config, active, sources)
    values = {item["key"]: item for item in shown["settings"]}  # type: ignore[union-attr]
    assert values["agent.commands"] == {
        "key": "agent.commands",
        "value": "allow",
        "source": "config file",
    }
    assert values["profile"]["value"] == "fixture"
    assert values["profile"]["source"] == "profile"
    assert values["agent.explore"]["source"] == "default"
    assert shown["saved_pending_restart"] is True
    assert shown["pending_settings"] == [
        {"key": "agent.commands", "value": "off", "source": "config file"}
    ]


@pytest.mark.parametrize(
    "key,value",
    [
        ("agent.explore", '"yes"'),
        ("leases.work_ms", "0"),
        ("leases.work_ms", "1000"),
        ("agent.commands", "unsandboxed"),
        ("agent.commands", "[]"),
        ("agent.commands", "{ nested = true }"),
        ("core.coordination_mode", "[]"),
        ("agent.explore_effort", "[]"),
        ("agent.web_fetch", "{}"),
        ("hooks.pre_tool", '[{match=["write_file"], command=["python"]}, 1]'),
        ("hooks.pre_tool", '[1, {match=["write_file"], command=["python"]}]'),
        ("core.unknown", "oops"),
        ("agent.secret", "opaque"),
        ("agent.model", "sk-" + "x" * 40),
    ],
)
def test_invalid_set_preserves_original_file(
    config: Path, key: str, value: str
) -> None:
    config.write_text(
        '# preserve this comment on failure\n[agent]\nprovider="openai"\n'
    )
    before = config.read_bytes(), config.stat().st_ino, config.stat().st_mtime_ns
    with pytest.raises(LlmCoordError) as failure:
        set_config_value(config, profile_id="fixture", key=key, value=value)
    assert failure.value.code == ErrorCode.CONFIG_INVALID
    assert (
        config.read_bytes(),
        config.stat().st_ino,
        config.stat().st_mtime_ns,
    ) == before
    assert not list(config.parent.glob(".config-*.toml"))


def test_typed_set_preserves_hooks_mcp_and_redacts_all_secret_positions(
    config: Path,
) -> None:
    config.write_text("""[mcp.servers.local]
command = ["python", "--password", "opaque-argument"]
env = { FOO = "opaque-environment" }
approval = "allow"
[[hooks.pre_tool]]
match = ["write_file"]
command = ["python", "guard.py"]
""")
    set_config_value(config, profile_id="fixture", key="agent.explore", value="false")
    set_config_value(
        config,
        profile_id="fixture",
        key="agent.web_domains",
        value='["docs.python.org"]',
    )
    active, sources = load_settings_snapshot(config, profile_id="fixture")
    assert active.agent_explore is False
    assert active.agent_web_domains == ("docs.python.org",)
    assert active.mcp_servers[0].env == (("FOO", "opaque-environment"),)
    assert len(active.hooks) == 1
    rendered = json.dumps(effective_config(config, active, sources))
    assert "opaque-environment" not in rendered
    assert "opaque-argument" not in rendered
    assert "[redacted]" in rendered
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_concurrent_setters_preserve_each_others_values(config: Path) -> None:
    updates = [
        ("agent.commands", "off"),
        ("agent.explore", "false"),
        ("leases.launch_ms", "123456"),
        ("agent.provider", "openai"),
    ]
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [
            workers.submit(
                set_config_value, config, profile_id="fixture", key=key, value=value
            )
            for key, value in updates
        ]
        for future in futures:
            assert future.result()["saved"] is True
    settings = load_settings(config)
    assert settings.agent_commands == "off"
    assert settings.agent_explore is False
    assert settings.launch_lease_ms == 123456
    assert settings.agent_provider == "openai"


def test_symlink_or_hardlink_configuration_is_refused(
    config: Path, tmp_path: Path
) -> None:
    target = tmp_path / "target.toml"
    target.write_text('[agent]\ncommands="ask"\n')
    config.symlink_to(target)
    with pytest.raises(LlmCoordError, match="symlink"):
        set_config_value(
            config, profile_id="fixture", key="agent.commands", value="allow"
        )
    assert target.read_text() == '[agent]\ncommands="ask"\n'
    config.unlink()
    config.hardlink_to(target)
    with pytest.raises(LlmCoordError, match="regular file"):
        set_config_value(
            config, profile_id="fixture", key="agent.commands", value="allow"
        )
    assert target.read_text() == '[agent]\ncommands="ask"\n'


@pytest.mark.parametrize("value", ['"invalid"', "[]", "{}"])
def test_active_values_still_show_when_saved_config_is_invalid(
    config: Path, value: str
) -> None:
    settings, sources = load_settings_snapshot(config, profile_id="fixture")
    config.write_text(f"[agent]\ncommands={value}\n")
    shown = effective_config(config, settings, sources)
    assert shown["saved_error"] == "saved configuration is invalid"
    assert shown["saved_pending_restart"] is None
    assert shown["settings"]


def test_digit_prefixed_server_name_reaches_existing_loader(config: Path) -> None:
    set_config_value(
        config,
        profile_id="fixture",
        key="mcp.servers.1tools.command",
        value='["python"]',
    )
    assert load_settings(config).mcp_servers[0].name == "1tools"


def test_quoted_dotted_header_is_a_single_key_part(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents: list[dict[str, object]] = []

    def loader(path: Path, *, profile_id: str) -> Settings:
        documents.append(tomllib.loads(path.read_text()))
        return Settings(profile_id=profile_id)

    monkeypatch.setattr(operations, "load_settings", loader)
    set_config_value(
        config, profile_id="fixture", key="mcp.servers.1tools.read_only", value="true"
    )
    set_config_value(
        config,
        profile_id="fixture",
        key='mcp.servers.1tools.headers."X.Custom"',
        value="MCP_CUSTOM_VALUE",
    )
    set_config_value(
        config,
        profile_id="fixture",
        key='mcp.servers.1tools.headers."X.Authorization"',
        value="MCP_AUTH_TOKEN",
    )
    assert documents[-1] == {
        "mcp": {
            "servers": {
                "1tools": {
                    "read_only": True,
                    "headers": {
                        "X.Custom": "MCP_CUSTOM_VALUE",
                        "X.Authorization": "MCP_AUTH_TOKEN",
                    },
                }
            }
        }
    }
    before = config.read_bytes()
    with pytest.raises(LlmCoordError, match="secrets belong"):
        set_config_value(
            config,
            profile_id="fixture",
            key="mcp.servers.1tools.headers.X.Authorization",
            value="MCP_AUTH_TOKEN",
        )
    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "key",
    [
        "agent",
        "agent.commands = false\nagent.explore",
        'agent.""',
        'agent."\\u0000"',
        "agent." + "x" * 129,
        "agent." + "x" * 513,
        ".".join(["agent"] * 9),
    ],
)
def test_invalid_key_shape_is_refused_without_changing_config(
    config: Path, key: str
) -> None:
    config.write_text('[agent]\ncommands="ask"\n')
    before = config.read_bytes()
    with pytest.raises(LlmCoordError) as failure:
        set_config_value(config, profile_id="fixture", key=key, value="false")
    assert failure.value.code == ErrorCode.CONFIG_INVALID
    assert config.read_bytes() == before


def test_env_set_result_redacts_opaque_values_and_extra_toml_is_refused(
    config: Path,
) -> None:
    config.write_text('[mcp.servers.local]\ncommand=["python"]\n')
    result = set_config_value(
        config,
        profile_id="fixture",
        key="mcp.servers.local.env.FOO",
        value="opaque-environment",
    )
    assert result["value"] == "[redacted]"
    before = config.read_bytes()
    with pytest.raises(LlmCoordError, match="one TOML value"):
        set_config_value(
            config, profile_id="fixture", key="agent.explore", value="false\nextra=123"
        )
    assert config.read_bytes() == before


def test_sensitive_header_key_allows_only_an_environment_reference(
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HTTP fields are introduced by the parallel B3 package. The guard itself
    # permits a reference; the integrated loader remains the final validator.
    def loader(path: Path, *, profile_id: str) -> Settings:
        document = tomllib.loads(path.read_text())
        assert document["mcp"]["servers"]["local"]["headers"] == {
            "Authorization": "MCP_AUTH_TOKEN",
        }
        return Settings(profile_id=profile_id)

    monkeypatch.setattr(operations, "load_settings", loader)
    result = set_config_value(
        config,
        profile_id="fixture",
        key="mcp.servers.local.headers.Authorization",
        value="MCP_AUTH_TOKEN",
    )
    assert result["saved"] is True and result["value"] == "[redacted]"
    before = config.read_bytes()
    for key, value in (
        ("mcp.servers.local.headers.Authorization", "Bearer opaque"),
        ("mcp.servers.local.headers.Authorization", "123"),
        ("mcp.servers.local.headers.Authorization", "sk-" + "x" * 40),
        ("mcp.servers.local.env.API_KEY", "MCP_AUTH_TOKEN"),
        ("agent.api_key", "MCP_AUTH_TOKEN"),
    ):
        with pytest.raises(LlmCoordError, match="secrets belong"):
            set_config_value(config, profile_id="fixture", key=key, value=value)
        assert config.read_bytes() == before
