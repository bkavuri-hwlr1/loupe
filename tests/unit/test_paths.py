from __future__ import annotations

import stat
from pathlib import Path

import pytest

from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths


def test_paths_use_xdg_bases_and_isolate_profiles(tmp_path: Path) -> None:
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
    }
    paths = AppPaths.resolve("team-a", environ=env, home=tmp_path / "ignored")
    assert paths.config_dir == tmp_path / "config" / "llm-coord"
    assert paths.data_dir == tmp_path / "data" / "llm-coord" / "profiles" / "team-a"
    assert paths.runtime_dir == tmp_path / "runtime" / "llm-coord" / "team-a"
    assert paths.control_db.parent == paths.data_dir


def test_paths_use_private_fallback_and_permissions(tmp_path: Path) -> None:
    paths = AppPaths.resolve("default", environ={}, home=tmp_path)
    paths.ensure()
    for directory in (
        paths.config_dir,
        paths.data_dir,
        paths.state_dir,
        paths.runtime_dir,
        paths.log_file.parent,
    ):
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_profile_is_validated_before_it_enters_a_path(tmp_path: Path) -> None:
    with pytest.raises(LlmCoordError):
        AppPaths.resolve("../escape", environ={}, home=tmp_path)


def test_symlink_in_app_state_path_is_rejected(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    state_link = tmp_path / "state-link"
    state_link.symlink_to(actual, target_is_directory=True)
    paths = AppPaths.resolve(
        environ={
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(state_link),
            "LLM_COORD_RUNTIME_DIR": str(tmp_path / "runtime"),
        },
        home=tmp_path,
    )
    with pytest.raises(LlmCoordError, match="symlink"):
        paths.ensure()
