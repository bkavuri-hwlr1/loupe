from __future__ import annotations

import subprocess
from pathlib import Path

from llm_cli.git.inspect import inspect_repository


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": __import__("os").environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    )
    return result.stdout.strip()


def test_inspect_local_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "fixture")

    info = inspect_repository(repository, profile_id="test")

    assert info.worktree == repository.resolve()
    assert info.main_worktree == repository.resolve()
    assert info.target_ref == "refs/heads/main"
    assert info.base_oid == _git(repository, "rev-parse", "HEAD")
    assert info.object_format in {"sha1", "sha256"}
    assert info.normalized_remote is None
    assert len(info.repo_key) == 64
