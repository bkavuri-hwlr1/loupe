"""The watcher setting and the resync notice users and models see."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_cli.cli.render import checkout_event_text
from llm_cli.config.loader import load_settings
from llm_cli.config.operations import load_settings_snapshot, settings_view
from llm_cli.errors import ErrorCode, LlmCoordError


def test_watchers_default_to_auto_and_can_be_turned_off(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "missing.toml").workspace_watchers == "auto"
    config = tmp_path / "config.toml"
    config.write_text('[workspace]\nwatchers = "off"\n')
    settings, sources = load_settings_snapshot(config, profile_id="test")
    assert settings.workspace_watchers == "off"
    shown = {item["key"]: item for item in settings_view(settings, sources)}
    assert shown["workspace.watchers"] == {
        "key": "workspace.watchers",
        "value": "off",
        "source": "config file",
    }


@pytest.mark.parametrize(
    "document",
    ['[workspace]\nwatchers = "on"\n', "[workspace]\nwatchers = false\n"],
)
def test_watchers_reject_other_values(tmp_path: Path, document: str) -> None:
    config = tmp_path / "config.toml"
    config.write_text(document)
    with pytest.raises(LlmCoordError, match='"auto" or "off"') as error:
        load_settings(config)
    assert error.value.code is ErrorCode.CONFIG_INVALID


def test_workspace_table_rejects_unknown_keys(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[workspace]\nwatcher = "off"\n')
    with pytest.raises(LlmCoordError) as error:
        load_settings(config)
    assert error.value.code is ErrorCode.CONFIG_INVALID


def test_resync_notice_tells_the_user_to_re_read() -> None:
    text = checkout_event_text(
        {
            "event_type": "checkout.resync_required",
            "caused_by_session_id": None,
            "payload": {"code": "CONTEXT_RESYNC_REQUIRED", "reason": "overflow"},
        },
        own_session_id="session",
    )
    assert text is not None
    assert "rescanned" in text and "Re-read" in text
