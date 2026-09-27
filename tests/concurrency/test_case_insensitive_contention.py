"""Contention must follow the working tree's real path identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import ClaimState
from llm_cli.coordination.scopes import CaseFoldAliasError
from llm_cli.storage.control import ControlStore

NOW = 1_750_000_000_000
REPO_KEY = "e" * 64


def _coordinator(
    tmp_path: Path, *task_ids: str, path_case_insensitive: bool
) -> RepositoryCoordinator:
    store = ControlStore(tmp_path / "control.sqlite3", timeout_seconds=5)
    store.initialize()
    store.register_repository(
        repository_id="repo_test",
        repo_key=REPO_KEY,
        display_name="fixture",
        git_common_dir=str(tmp_path / "repo" / ".git"),
        main_worktree_path=str(tmp_path / "repo"),
        target_ref="refs/heads/main",
        path_case_insensitive=path_case_insensitive,
        now=NOW,
    )
    for task_id in task_ids:
        store.create_task(
            repository_id="repo_test", task_id=task_id, title=task_id, now=NOW
        )
    return RepositoryCoordinator(store)


def test_unprobed_registration_defaults_to_contending(tmp_path: Path) -> None:
    """A repository registered without a probe must fail closed, not open."""

    store = ControlStore(tmp_path / "control.sqlite3", timeout_seconds=5)
    store.initialize()
    record = store.register_repository(
        repo_key=REPO_KEY,
        display_name="fixture",
        git_common_dir=str(tmp_path / "repo" / ".git"),
        main_worktree_path=str(tmp_path / "repo"),
        target_ref="refs/heads/main",
        now=NOW,
    )
    assert record.path_case_insensitive is True


def test_case_alias_scopes_contend_on_a_case_insensitive_worktree(
    tmp_path: Path,
) -> None:
    """'Docs/' and 'docs/' are one directory, so the second claim must queue."""

    coordinator = _coordinator(tmp_path, "task_a", "task_b", path_case_insensitive=True)
    first = coordinator.request_claim("task_a", ["Docs/"], now=NOW)
    second = coordinator.request_claim("task_b", ["docs/"], now=NOW)

    assert first.state is ClaimState.ACTIVE_WORK
    assert second.state is ClaimState.QUEUED
    assert second.blocking_claim_ids == (first.claim_id,)


def test_case_alias_scopes_stay_concurrent_on_a_case_sensitive_worktree(
    tmp_path: Path,
) -> None:
    """Distinct directories must not be conflated into false contention."""

    coordinator = _coordinator(
        tmp_path, "task_a", "task_b", path_case_insensitive=False
    )
    first = coordinator.request_claim("task_a", ["Docs/"], now=NOW)
    second = coordinator.request_claim("task_b", ["docs/"], now=NOW)

    assert first.state is ClaimState.ACTIVE_WORK
    assert second.state is ClaimState.ACTIVE_WORK
    assert first.fencing_token != second.fencing_token


def test_released_case_alias_claim_activates_its_waiter(tmp_path: Path) -> None:
    """A queued alias claim is woken by the release that unblocks it."""

    coordinator = _coordinator(tmp_path, "task_a", "task_b", path_case_insensitive=True)
    first = coordinator.request_claim("task_a", ["Docs/"], now=NOW)
    coordinator.request_claim("task_b", ["docs/"], now=NOW)

    result = coordinator.release_claim(first.claim_id, reason="done", now=NOW)

    assert [claim.task_id for claim in result.activated] == ["task_b"]
    assert result.activated[0].state is ClaimState.ACTIVE_WORK


def test_alias_scopes_within_one_claim_are_rejected(tmp_path: Path) -> None:
    """One claim may not reserve two spellings of the same directory.

    The daemon maps this to ``SCOPE_VIOLATION`` rather than granting authority
    under a spelling the operator did not actually reserve.
    """

    coordinator = _coordinator(tmp_path, "task_a", path_case_insensitive=True)
    with pytest.raises(CaseFoldAliasError):
        coordinator.request_claim("task_a", ["Docs/", "docs/notes.md"], now=NOW)
