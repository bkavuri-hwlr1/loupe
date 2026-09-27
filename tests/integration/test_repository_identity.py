"""Registered identity must survive checkouts and local-only registration."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from llm_cli.git.inspect import inspect_repository

_GIT_ENV = {
    "PATH": os.environ["PATH"],
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )
    return result.stdout.strip()


def _git_config(path: Path, name: str) -> str | None:
    """Read a Git config value, returning ``None`` when it is unset.

    ``git config --get`` exits non-zero for an unset key, and ``core.ignorecase``
    is only written when Git's own probe finds a case-insensitive filesystem.
    On a case-sensitive one the key is simply absent, so the test has to read it
    the same way the production probe does rather than treating absence as an
    error.
    """

    result = subprocess.run(
        ["git", "config", "--type=bool", "--get", name],
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "fixture")
    return repository


def test_case_sensitivity_follows_git(tmp_path: Path) -> None:
    """Contention must use Git's own working-tree probe, not a guess."""

    repository = _repository(tmp_path)
    info = inspect_repository(repository, profile_id="test")

    ignore_case = _git_config(repository, "core.ignorecase")
    assert info.path_case_insensitive is (ignore_case != "false")


def test_local_only_identity_is_stable_when_a_remote_exists(tmp_path: Path) -> None:
    """A local-only key must be reproducible without the remote sneaking back in.

    The re-verification the runner performs has to be told how the repository
    was registered; deriving it from the stored remote is impossible because a
    local-only registration still records the remote it declined to use.
    """

    repository = _repository(tmp_path)
    _git(repository, "remote", "add", "origin", "https://example.invalid/demo.git")

    registered = inspect_repository(
        repository, profile_id="test", target="main", coordinate_by_remote=False
    )
    reverified = inspect_repository(
        repository, profile_id="test", target="main", coordinate_by_remote=False
    )
    by_remote = inspect_repository(
        repository, profile_id="test", target="main", coordinate_by_remote=True
    )

    assert registered.repo_key == reverified.repo_key
    assert registered.repo_key != by_remote.repo_key
    assert registered.normalized_remote == "example.invalid/demo"


def test_target_ref_identity_changes_with_the_checked_out_branch(
    tmp_path: Path,
) -> None:
    """Recomputing identity without the registered target derives another repo."""

    repository = _repository(tmp_path)
    registered = inspect_repository(repository, profile_id="test", target="main")
    _git(repository, "checkout", "-b", "feature")

    derived = inspect_repository(repository, profile_id="test")

    assert derived.target_ref == "refs/heads/feature"
    assert derived.repo_key != registered.repo_key
    # The common directory is what stays stable across checkouts, which is why
    # the daemon falls back to it when resolving a path to a registration.
    assert derived.common_git_dir == registered.common_git_dir
