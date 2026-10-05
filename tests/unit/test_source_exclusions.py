"""User exclusion rules stay current while costing few Git processes."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from llm_cli.agent import source_policy
from llm_cli.agent.source_policy import excluded_paths, exclusion_scope


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "notes.txt").write_text("notes\n")
    (root / "source.txt").write_text("source\n")
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    return root


def _exclude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rules: str | None
) -> None:
    config = tmp_path / "gitconfig"
    if rules is None:
        config.write_text("")
    else:
        exclusions = tmp_path / "user-ignore"
        exclusions.write_text(rules)
        config.write_text(f'[core]\n    excludesFile = "{exclusions}"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))


def _count_setting_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    reads: list[Path] = []
    original: Callable[..., str] = source_policy._user_excludes_file

    def counted(root: Path, remaining: Callable[[], float] | None) -> str:
        reads.append(root)
        return original(root, remaining)

    monkeypatch.setattr(source_policy, "_user_excludes_file", counted)
    return reads


def test_scope_reads_the_user_setting_once(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exclude(tmp_path, monkeypatch, "notes.txt\n")
    reads = _count_setting_reads(monkeypatch)

    with exclusion_scope():
        for _ in range(3):
            assert excluded_paths(repository, ["notes.txt", "source.txt"]) == {
                "notes.txt"
            }
        with exclusion_scope():
            excluded_paths(repository, ["notes.txt"])
    assert reads == [repository]

    excluded_paths(repository, ["notes.txt"])
    excluded_paths(repository, ["notes.txt"])
    assert len(reads) == 3


def test_a_changed_setting_applies_in_the_next_scope(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exclude(tmp_path, monkeypatch, None)
    with exclusion_scope():
        assert excluded_paths(repository, ["notes.txt"]) == set()

    _exclude(tmp_path, monkeypatch, "notes.txt\n")
    with exclusion_scope():
        assert excluded_paths(repository, ["notes.txt"]) == {"notes.txt"}


def test_user_rules_reuse_one_empty_repository_and_replace_a_removed_one(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exclude(tmp_path, monkeypatch, "notes.txt\n")
    assert excluded_paths(repository, ["notes.txt"]) == {"notes.txt"}
    first = source_policy._empty_repository_directory
    assert first is not None
    assert excluded_paths(repository, ["source.txt"]) == set()
    assert source_policy._empty_repository_directory is first

    shutil.rmtree(first.name)
    assert excluded_paths(repository, ["notes.txt"]) == {"notes.txt"}
    second = source_policy._empty_repository_directory
    assert second is not None and second is not first
    assert (Path(second.name) / ".git/HEAD").is_file()
