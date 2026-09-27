"""Shared pytest fixtures for real local Git repositories and daemons."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from llm_cli.config.models import Settings
from llm_cli.daemon.service import DaemonService
from llm_cli.paths import AppPaths


@pytest.fixture
def git_run() -> Callable[..., str]:
    """Run Git without inheriting a developer's global configuration."""

    def run(path: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ["PATH"],
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            },
        )
        return result.stdout.strip()

    return run


@pytest.fixture
def repository_factory(
    git_run: Callable[..., str],
) -> Callable[[Path, dict[str, str]], Path]:
    def create(tmp_path: Path, files: dict[str, str]) -> Path:
        repository = tmp_path / "repository"
        for relative_path, content in files.items():
            target = repository / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        git_run(repository, "init", "-b", "main")
        git_run(repository, "config", "user.name", "Fixture")
        git_run(repository, "config", "user.email", "fixture@example.invalid")
        git_run(repository, "add", "-A")
        git_run(repository, "commit", "-m", "base")
        return repository

    return create


@pytest.fixture
def service_factory() -> Callable[[Path], DaemonService]:
    def create(tmp_path: Path) -> DaemonService:
        paths = AppPaths.resolve(
            "test",
            environ={
                "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
                "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
                "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
                "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
            },
            home=tmp_path,
        )
        return DaemonService(
            paths,
            Settings(profile_id="test"),
            asyncio.Event(),
            boot_id="boot-shared-workspace",
        )

    return create


@pytest.fixture
def request_factory() -> Callable[[str, dict[str, Any]], Any]:
    from llm_cli.protocol.envelopes import Request

    def create(method: str, params: dict[str, Any]) -> Request:
        return Request.create(
            request_id=f"request_{method.replace('.', '_')}",
            method=method,
            params=params,
            profile_id="test",
        )

    return create
