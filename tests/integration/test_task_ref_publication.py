"""Internal task refs must swap against their real predecessor."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.integrate import publish_task_ref, read_task_ref


def _git(path: Path, *arguments: str) -> str:
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
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    return result.stdout.strip()


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


def _commit(repository: Path, message: str) -> str:
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    return _git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit-tree",
        tree,
        "-m",
        message,
    )


def test_absent_task_ref_reads_as_none(tmp_path: Path) -> None:
    """A missing ref is absence, not a Git failure.

    Git reports an unknown ref as exit 1 or 128 depending on version, so this
    must not be inferred from the exit code alone.
    """

    repository = _repository(tmp_path)
    assert read_task_ref(repository, "never-published") is None


def test_first_publication_requires_an_absent_ref(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    commit = _commit(repository, "result one")

    published = publish_task_ref(
        repository, task_id="t1", new_commit_oid=commit, expected_old_oid=None
    )

    assert published.ref == "refs/llm-coord/tasks/t1"
    assert published.commit_oid == commit
    assert read_task_ref(repository, "t1") == commit


def test_later_attempt_advances_the_ref_from_its_predecessor(tmp_path: Path) -> None:
    """A retried task publishes over its own previous result."""

    repository = _repository(tmp_path)
    first = _commit(repository, "result one")
    publish_task_ref(
        repository, task_id="t1", new_commit_oid=first, expected_old_oid=None
    )

    second = _commit(repository, "result two")
    published = publish_task_ref(
        repository, task_id="t1", new_commit_oid=second, expected_old_oid=first
    )

    assert published.commit_oid == second
    assert read_task_ref(repository, "t1") == second


def test_publication_refuses_a_stale_predecessor(tmp_path: Path) -> None:
    """A ref that moved under us must not be clobbered."""

    repository = _repository(tmp_path)
    first = _commit(repository, "result one")
    publish_task_ref(
        repository, task_id="t1", new_commit_oid=first, expected_old_oid=None
    )
    concurrent = _commit(repository, "someone else")
    publish_task_ref(
        repository, task_id="t1", new_commit_oid=concurrent, expected_old_oid=first
    )

    stale = _commit(repository, "result two")
    with pytest.raises(LlmCoordError) as caught:
        publish_task_ref(
            repository, task_id="t1", new_commit_oid=stale, expected_old_oid=first
        )

    assert caught.value.code is ErrorCode.TARGET_MOVED
    assert read_task_ref(repository, "t1") == concurrent


def test_republishing_over_an_existing_ref_without_a_predecessor_fails(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    first = _commit(repository, "result one")
    publish_task_ref(
        repository, task_id="t1", new_commit_oid=first, expected_old_oid=None
    )

    second = _commit(repository, "result two")
    with pytest.raises(LlmCoordError) as caught:
        publish_task_ref(
            repository, task_id="t1", new_commit_oid=second, expected_old_oid=None
        )

    assert caught.value.code is ErrorCode.TARGET_MOVED
    assert caught.value.details is not None
    assert caught.value.details["actual_oid"] == first
