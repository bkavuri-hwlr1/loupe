from __future__ import annotations

from pathlib import Path

from llm_cli.coordination.identity import normalize_remote_identity, repository_key


def test_remote_identity_removes_credentials_and_normalizes_transport() -> None:
    expected = "github.com/Owner/Repo"
    assert normalize_remote_identity("git@github.com:Owner/Repo.git") == expected
    assert (
        normalize_remote_identity("https://token@GITHUB.COM/Owner/Repo.git") == expected
    )
    assert normalize_remote_identity("ssh://git@github.com:22/Owner/Repo") == expected
    assert (
        normalize_remote_identity("https://github.com:443/Owner/Repo.git?x=1")
        == expected
    )


def test_local_paths_are_not_treated_as_remote_identities() -> None:
    assert normalize_remote_identity("../repo.git") is None
    assert normalize_remote_identity("file:///tmp/repo.git") is None
    assert normalize_remote_identity(None) is None


def test_repository_key_coordinates_transport_equivalent_clones(tmp_path: Path) -> None:
    first = repository_key(
        profile_id="default",
        integration_adapter="local_git",
        target_ref="refs/heads/main",
        common_git_dir=tmp_path / "one" / ".git",
        normalized_remote="github.com/Owner/Repo",
    )
    second = repository_key(
        profile_id="default",
        integration_adapter="local_git",
        target_ref="refs/heads/main",
        common_git_dir=tmp_path / "two" / ".git",
        normalized_remote="github.com/Owner/Repo",
    )
    assert first == second
    local_first = repository_key(
        profile_id="default",
        integration_adapter="local_git",
        target_ref="refs/heads/main",
        common_git_dir=tmp_path / "one" / ".git",
        normalized_remote="github.com/Owner/Repo",
        coordinate_by_remote=False,
    )
    local_second = repository_key(
        profile_id="default",
        integration_adapter="local_git",
        target_ref="refs/heads/main",
        common_git_dir=tmp_path / "two" / ".git",
        normalized_remote="github.com/Owner/Repo",
        coordinate_by_remote=False,
    )
    assert local_first != local_second
