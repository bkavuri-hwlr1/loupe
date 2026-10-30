"""Explicit isolated count limits do not bypass the other capture bounds."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from llm_cli.config.loader import load_settings
from llm_cli.errors import LlmCoordError
from llm_cli.workspace.hydration import HydrationBudgets
from llm_cli.workspace.isolated import SourceCaptureCache, capture_source


def test_isolated_count_limits_load_independently_of_rebuild(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[isolated]\nsource_max_paths = 50000\nhydration_max_entries = 100000\n"
    )
    settings = load_settings(config)
    assert settings.isolated_source_max_paths == 50_000
    assert settings.isolated_hydration_max_entries == 100_000
    assert settings.isolated_rebuild == ()


@pytest.mark.parametrize("key", ["source_max_paths", "hydration_max_entries"])
@pytest.mark.parametrize("value", ["0", "-1", "100001", "true", '"50000"', "2.5"])
def test_isolated_count_limits_reject_invalid_values(
    tmp_path: Path, key: str, value: str
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f"[isolated]\n{key} = {value}\n")
    with pytest.raises(LlmCoordError, match=f"isolated.{key}.*1.*100000"):
        load_settings(config)


def test_explicit_source_limit_refuses_before_storing_any_source(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root = repository_factory(tmp_path, {"a.py": "first\n", "b.py": "second\n"})
    candidates = tmp_path / "candidates"
    with pytest.raises(LlmCoordError, match=r"isolated\.source_max_paths=1"):
        capture_source(root, candidates, max_paths=1)
    assert not candidates.exists()
    assert len(capture_source(root, candidates, max_paths=2)) == 2


def test_explicit_source_limit_also_governs_a_warm_capture(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root = repository_factory(tmp_path, {"a.py": "first\n", "b.py": "second\n"})
    candidates = tmp_path / "candidates"
    cache = SourceCaptureCache()
    assert len(capture_source(root, candidates, max_paths=2, cache=cache)) == 2
    with pytest.raises(LlmCoordError, match=r"isolated\.source_max_paths=1"):
        capture_source(root, candidates, max_paths=1, cache=cache)


def test_other_hydration_bounds_and_entry_ceiling_are_unchanged() -> None:
    budgets = HydrationBudgets(max_entries=100_000)
    assert budgets.max_metadata_bytes == 4 * 1024 * 1024
    assert budgets.max_seconds == 10
    assert budgets.max_depth == 64
    assert budgets.max_file_bytes == 256 * 1024 * 1024
    assert budgets.max_total_bytes == 2 * 1024 * 1024 * 1024
    assert budgets.max_allocated_bytes == 2 * 1024 * 1024 * 1024
    with pytest.raises(LlmCoordError, match=r"max_entries.*100000"):
        HydrationBudgets(max_entries=100_001)


def test_a_watched_capture_cannot_reuse_a_higher_count_limit(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    from llm_cli.workspace.isolated import capture_watched_source
    from llm_cli.workspace.watchers import WatchRegistry

    root = repository_factory(tmp_path, {"a.py": "first\n", "b.py": "second\n"})
    candidates = tmp_path / "candidates"
    cache = SourceCaptureCache()
    watcher = WatchRegistry(cookie_dir=tmp_path / "cookies")
    try:
        result = capture_watched_source(
            root,
            candidates,
            protected_paths=(),
            cache=cache,
            watch=watcher,
            max_paths=2,
        )
        assert len(result) == 2
        with pytest.raises(LlmCoordError, match=r"isolated\.source_max_paths=1"):
            capture_watched_source(
                root,
                candidates,
                protected_paths=(),
                cache=cache,
                watch=watcher,
                max_paths=1,
            )
    finally:
        watcher.close()


def test_count_settings_roundtrip_with_truthful_config_display(tmp_path: Path) -> None:
    from llm_cli.config.operations import (
        effective_config,
        load_settings_snapshot,
        set_config_value,
    )

    config = tmp_path / "config.toml"
    set_config_value(
        config, profile_id="admission", key="isolated.source_max_paths", value="50000"
    )
    set_config_value(
        config,
        profile_id="admission",
        key="isolated.hydration_max_entries",
        value="100000",
    )
    settings, sources = load_settings_snapshot(config, profile_id="admission")
    view = effective_config(config, settings, sources)
    entries = {item["key"]: item for item in view["settings"]}
    assert entries["isolated.source_max_paths"] == {
        "key": "isolated.source_max_paths",
        "value": 50000,
        "source": "config file",
    }
    assert entries["isolated.hydration_max_entries"] == {
        "key": "isolated.hydration_max_entries",
        "value": 100000,
        "source": "config file",
    }
    assert "isolated_source_max_paths" not in entries
    assert view["saved_pending_restart"] is False


@pytest.mark.parametrize("explicit", [False, True])
def test_hydration_preview_uses_profile_limit_with_explicit_flag_precedence(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    explicit: bool,
) -> None:
    from types import SimpleNamespace
    from typing import cast

    from llm_cli.cli.app import build_parser, dispatch
    from llm_cli.protocol.client import DaemonClient

    root = repository_factory(tmp_path, {"a.py": "pass\n", "b.py": "pass\n"})
    config = tmp_path / "profile.toml"
    config.write_text("[isolated]\nhydration_max_entries=2\n")

    def no_rpc(*args: object, **kwargs: object) -> object:
        raise AssertionError("a hydration preview must not send an RPC")

    client = SimpleNamespace(
        paths=SimpleNamespace(config_file=config, profile_id="preview"), call=no_rpc
    )
    command = ["--json", "workspace", "hydrate", str(root), "--plan"]
    if explicit:
        command += ["--max-entries", "20"]
    result = dispatch(build_parser().parse_args(command), cast(DaemonClient, client))
    assert result["budgets"]["max_entries"] == (20 if explicit else 2)
    assert result["complete"] is explicit
