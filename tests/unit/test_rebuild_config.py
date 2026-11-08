"""Explicit rebuild permission and strict bounded installer settings."""

from pathlib import Path

import pytest

from llm_cli.config.loader import load_settings
from llm_cli.config.models import RebuildConfig
from llm_cli.config.operations import initial_sources, settings_view
from llm_cli.errors import LlmCoordError


def test_rebuild_settings_are_opt_in_and_support_argv_and_network_table(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    assert load_settings(config).isolated_rebuild == ()
    config.write_text("""
[isolated.rebuild]
".venv" = ["uv", "sync", "--frozen"]
"web/node_modules" = {argv = ["npm", "ci"], network = true, timeout_seconds = 180}
""")
    settings = load_settings(config)
    assert settings.isolated_rebuild == (
        RebuildConfig(".venv", ("uv", "sync", "--frozen")),
        RebuildConfig("web/node_modules", ("npm", "ci"), True, 180),
    )
    view = {
        entry["key"]: entry
        for entry in settings_view(settings, initial_sources(config, settings))
    }
    assert view["isolated.rebuild"]["source"] == "config file"


@pytest.mark.parametrize(
    "value",
    [
        '"../.venv" = ["uv"]',
        '"/tmp/.venv" = ["uv"]',
        '".git/node_modules" = ["npm"]',
        '"source" = ["npm"]',
        '".venv" = []',
        '".venv" = ["uv", ""]',
        '".venv" = {argv = ["uv"], network = "yes"}',
        '".venv" = {argv = ["uv"], timeout_seconds = true}',
        '".venv" = {argv = ["uv"], timeout_seconds = 301}',
        '".venv" = {argv = ["uv"], unknown = true}',
        '".venv" = ["uv"]\n".venv/node_modules" = ["npm"]',
    ],
)
def test_rebuild_rejects_unsafe_roots_and_unbounded_installers(
    tmp_path: Path, value: str
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[isolated.rebuild]\n" + value)
    with pytest.raises(LlmCoordError):
        load_settings(config)


def test_rebuild_roots_reject_unicode_aliases(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[isolated.rebuild]\n"é/.venv" = ["uv"]\n"é/.venv" = ["uv"]\n')
    with pytest.raises(LlmCoordError, match="distinct"):
        load_settings(config)


def test_config_set_supports_quoted_rebuild_roots(tmp_path: Path) -> None:
    from llm_cli.config.operations import set_config_value

    config = tmp_path / "config" / "config.toml"
    set_config_value(
        config,
        profile_id="test",
        key='isolated.rebuild.".venv"',
        value='["uv", "sync", "--frozen"]',
    )
    settings = load_settings(config)
    assert settings.isolated_rebuild == (
        RebuildConfig(".venv", ("uv", "sync", "--frozen")),
    )


def test_journal_fits_maximum_unicode_and_escaped_argument_budget(
    tmp_path: Path,
) -> None:
    from llm_cli.workspace.rebuild import JOURNAL, prepare, read_journal

    specs = tuple(
        RebuildConfig(f"project-{number}/.venv", ("tool", '"' * 4090, '"' * 4090))
        for number in range(16)
    )
    prepare(tmp_path, "owner-large-journal", specs)
    assert (tmp_path / JOURNAL).stat().st_size < 512 * 1024
    value = read_journal(tmp_path, "owner-large-journal")
    assert value is not None and len(value["specs"]) == 16
    unicode_specs = tuple(
        RebuildConfig(f"project-{number}/.venv", ("tool", "é" * 4090))
        for number in range(16)
    )
    prepare(tmp_path, "owner-unicode-journal", unicode_specs)
    assert read_journal(tmp_path, "owner-unicode-journal") is not None


def test_rebuild_argument_budget_counts_encoded_bytes(tmp_path: Path) -> None:
    import json

    config = tmp_path / "config.toml"
    config.write_text(
        '[isolated.rebuild]\n".venv" = ' + json.dumps(["uv", "é" * 3000, "é" * 3000])
    )
    with pytest.raises(LlmCoordError, match="argv"):
        load_settings(config)


def test_installer_cache_is_explicit_and_has_bounded_visible_settings(
    tmp_path: Path,
) -> None:
    config = tmp_path / "cache.toml"
    defaults = load_settings(config)
    assert defaults.isolated_rebuild_cache is False
    assert defaults.isolated_rebuild_cache_max_bytes == 512 * 1024**2
    config.write_text("""[isolated.rebuild]
cache = true
cache_max_bytes = 123456
".venv" = ["uv", "sync", "--frozen"]
""")
    settings = load_settings(config)
    assert settings.isolated_rebuild_cache is True
    assert settings.isolated_rebuild_cache_max_bytes == 123456
    assert len(settings.isolated_rebuild) == 1
    view = {
        entry["key"]: entry
        for entry in settings_view(settings, initial_sources(config, settings))
    }
    assert view["isolated.rebuild.cache"]["value"] is True
    assert view["isolated.rebuild.cache_max_bytes"]["value"] == 123456


@pytest.mark.parametrize(
    "value",
    [
        'cache = "yes"',
        "cache = 1",
        "cache_max_bytes = true",
        "cache_max_bytes = 0",
        "cache_max_bytes = 65535",
        f"cache_max_bytes = {5 * 1024**3 + 1}",
    ],
)
def test_installer_cache_rejects_invalid_consent_or_size(
    tmp_path: Path,
    value: str,
) -> None:
    config = tmp_path / "cache.toml"
    config.write_text("[isolated.rebuild]\n" + value)
    with pytest.raises(LlmCoordError):
        load_settings(config)
