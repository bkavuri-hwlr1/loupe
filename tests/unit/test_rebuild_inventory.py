"""Live installer inventory tolerates growth without relaxing final validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from llm_cli.errors import LlmCoordError
from llm_cli.workspace import rebuild


@pytest.mark.parametrize("validate", [False, True])
def test_inventory_tolerates_directory_growth_only_while_installing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, validate: bool
) -> None:
    dependency = tmp_path / ".venv"
    dependency.mkdir()
    original_open = os.open
    changed = False

    def open_with_growth(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal changed
        descriptor = original_open(name, flags, *args, **kwargs)
        if name == ".venv" and not changed:
            changed = True
            (dependency / "installed.py").write_text("answer = 42\n")
        return descriptor

    monkeypatch.setattr(os, "open", open_with_growth)
    if validate:
        with pytest.raises(LlmCoordError, match="dependency rebuild directory changed"):
            rebuild.inspect_tree(tmp_path, ".venv", validate=True)
    else:
        receipt = rebuild.inspect_tree(tmp_path, ".venv", validate=False)
        assert receipt["entries"] == 2
        assert receipt["bytes"] == len("answer = 42\n")
    assert changed


@pytest.mark.parametrize("validate", [False, True])
def test_inventory_refuses_directory_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, validate: bool
) -> None:
    dependency = tmp_path / ".venv"
    dependency.mkdir()
    original_open = os.open
    changed = False

    def open_replacement(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal changed
        if name == ".venv" and not changed:
            changed = True
            dependency.rename(tmp_path / "original")
            dependency.mkdir()
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_replacement)
    with pytest.raises(LlmCoordError, match="dependency rebuild directory changed"):
        rebuild.inspect_tree(tmp_path, ".venv", validate=validate)
    assert changed


def test_live_inventory_still_enforces_file_size_after_directory_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dependency = tmp_path / ".venv"
    dependency.mkdir()
    original_open = os.open
    changed = False

    def open_with_growth(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal changed
        descriptor = original_open(name, flags, *args, **kwargs)
        if name == ".venv" and not changed:
            changed = True
            (dependency / "installed.py").write_bytes(b"x" * 17)
        return descriptor

    monkeypatch.setattr(os, "open", open_with_growth)
    monkeypatch.setattr(rebuild, "_MAX_FILE", 16)
    with pytest.raises(
        LlmCoordError, match="dependency rebuild exceeded its size limit"
    ):
        rebuild.inspect_tree(tmp_path, ".venv", validate=False)
    assert changed


@pytest.mark.parametrize("validate", [False, True])
def test_regular_file_growth_requires_fresh_budget_observation_and_final_stability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, validate: bool
) -> None:
    dependency = tmp_path / ".venv"
    dependency.mkdir()
    installed = dependency / "installed.py"
    installed.write_bytes(b"x" * 8)
    original_stat = os.stat
    changed = False

    def stat_with_growth(name: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal changed
        status = original_stat(name, *args, **kwargs)
        if name == "installed.py" and not changed:
            changed = True
            installed.write_bytes(b"x" * 17)
        return status

    monkeypatch.setattr(os, "stat", stat_with_growth)
    monkeypatch.setattr(rebuild, "_MAX_FILE", 16)
    if validate:
        with pytest.raises(LlmCoordError, match="dependency rebuild path changed"):
            rebuild.inspect_tree(tmp_path, ".venv", validate=True)
    else:
        assert rebuild.inspect_tree(tmp_path, ".venv", validate=False)["bytes"] == 8
        # A live observation is advisory. The following pass must detect growth,
        # and the final strict receipt may never accept a changing file.
        with pytest.raises(
            LlmCoordError, match="dependency rebuild exceeded its size limit"
        ):
            rebuild.inspect_tree(tmp_path, ".venv", validate=False)
    assert changed
