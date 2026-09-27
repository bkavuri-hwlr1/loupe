"""Resolving a path to its registration must not depend on the current branch."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from llm_cli.config.models import Settings
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.envelopes import Request


def _git(path: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        },
    )


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "base",
    )
    return repository


def _service(tmp_path: Path) -> DaemonService:
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
    service = DaemonService(
        paths, Settings(profile_id="test"), asyncio.Event(), boot_id="boot"
    )
    service.initialize()
    return service


def _request(method: str, params: dict[str, Any]) -> Request:
    return Request.create(
        request_id=f"request_{method.replace('.', '_')}",
        method=method,
        params=params,
        profile_id="test",
    )


def test_repo_status_resolves_after_checking_out_another_branch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        try:
            added = await service.handle(
                _request("repo.add", {"path": str(repository)})
            )
            _git(repository, "checkout", "-b", "feature")

            status = await service.handle(
                _request("repo.status", {"path": str(repository)})
            )

            assert status["repo_key"] == added["repo_key"]
            assert status["target_ref"] == "refs/heads/main"
        finally:
            service.close()

    asyncio.run(scenario())


def test_repo_add_records_the_worktree_case_behavior(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        try:
            added = await service.handle(
                _request("repo.add", {"path": str(repository)})
            )
            assert isinstance(added["path_case_insensitive"], bool)
            assert added["coordinate_by_remote"] is True

            local = await service.handle(
                _request(
                    "repo.add",
                    {"path": str(repository), "coordinate_by_remote": False},
                )
            )
            assert local["coordinate_by_remote"] is False
        finally:
            service.close()

    asyncio.run(scenario())


def test_ambiguous_registrations_name_their_targets(tmp_path: Path) -> None:
    """Two registered targets must be reported, never silently conflated."""

    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        try:
            await service.handle(
                _request("repo.add", {"path": str(repository), "target": "main"})
            )
            _git(repository, "branch", "release")
            await service.handle(
                _request("repo.add", {"path": str(repository), "target": "release"})
            )
            _git(repository, "checkout", "-b", "feature")

            with pytest.raises(LlmCoordError) as caught:
                await service.handle(_request("repo.status", {"path": str(repository)}))

            assert caught.value.code is ErrorCode.REPOSITORY_NOT_FOUND
            assert caught.value.details == {
                "registered_targets": ["refs/heads/main", "refs/heads/release"]
            }
        finally:
            service.close()

    asyncio.run(scenario())
