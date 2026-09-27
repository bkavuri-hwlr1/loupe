"""Real-checkout publication, durable conflicts, and crash-boundary recovery."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from llm_cli.storage.control import ControlStore
from llm_cli.workspace import batches
from llm_cli.workspace.batches import BatchFile, SharedBatchPublisher
from llm_cli.workspace.broker import WorkspaceBrokerError, load_candidate_content
from llm_cli.workspace.identity import (
    ABSENT,
    EXECUTABLE_MODE,
    REGULAR_MODE,
    identify_path,
)


@dataclass
class BatchEnvironment:
    repository: Path
    store: ControlStore
    publisher: SharedBatchPublisher
    workspace_id: str

    def files(self) -> tuple[BatchFile, ...]:
        return tuple(
            BatchFile(
                path, identify_path(self.repository / path), content, REGULAR_MODE
            )
            for path, content in (("a.txt", b"new a\n"), ("docs/b.txt", b"new b\n"))
        )


@pytest.fixture
def batch_env(
    tmp_path: Path, repository_factory: Callable[[Path, dict[str, str]], Path]
) -> BatchEnvironment:
    repository = repository_factory(
        tmp_path, {"a.txt": "old a\n", "docs/b.txt": "old b\n"}
    )
    store = ControlStore(tmp_path / "control.sqlite3")
    store.initialize()
    registered = store.register_repository(
        repo_key="a" * 64,
        display_name="batch fixture",
        git_common_dir=str(repository / ".git"),
        main_worktree_path=str(repository),
        target_ref="refs/heads/main",
        path_case_insensitive=False,
    )
    checkout = store.ensure_checkout(
        repository=registered,
        canonical_path=str(repository),
        git_common_dir=str(repository / ".git"),
    )
    workspace = store.ensure_shared_workspace(checkout)
    for session_id in ("first", "second"):
        store.open_session(
            session_id=session_id,
            checkout_id=checkout.checkout_id,
            resume_token_hash=hashlib.sha256(session_id.encode()).hexdigest(),
            provider="fake",
            model="fake",
            workspace_id=workspace.workspace_id,
        )
    with store.connection() as connection:
        connection.execute("UPDATE sessions SET state = 'active'")
    return BatchEnvironment(
        repository,
        store,
        SharedBatchPublisher(store, tmp_path / "candidates", threading.RLock()),
        workspace.workspace_id,
    )


def _events(environment: BatchEnvironment) -> int:
    with environment.store.connection() as connection:
        return int(
            connection.execute(
                "SELECT count(*) FROM checkout_events "
                "WHERE event_type = 'workspace.batch_published'"
            ).fetchone()[0]
        )


def test_batch_publishes_once_without_touching_git(
    batch_env: BatchEnvironment, git_run: Callable[..., str]
) -> None:
    env = batch_env
    baseline = tuple(
        git_run(env.repository, *args)
        for args in (("rev-parse", "HEAD"), ("write-tree",), ("show-ref",))
    )
    files = env.files()
    result = env.publisher.publish("batch", "first", files, ("*",))
    assert result.state == "published"
    assert result.workspace_revision == 1
    assert result.paths == ("a.txt", "docs/b.txt")
    assert all(
        (env.repository / item.relative_path).read_bytes() == item.content
        for item in files
    )
    assert env.publisher.publish("batch", "first", files, ("*",)) == result
    assert env.publisher.get("batch") == result
    assert _events(env) == 1
    assert not env.publisher.blocks_workspace(env.workspace_id)
    assert (
        tuple(
            git_run(env.repository, *args)
            for args in (("rev-parse", "HEAD"), ("write-tree",), ("show-ref",))
        )
        == baseline
    )


@pytest.mark.parametrize(
    "changed", ["content", "session", "scopes", "base", "order", "mode", "case"]
)
def test_batch_id_is_immutable(batch_env: BatchEnvironment, changed: str) -> None:
    env = batch_env
    files = env.files()
    env.publisher.publish("batch", "first", files, ("*",))
    session = "second" if changed == "session" else "first"
    scopes = ("a.txt", "docs/") if changed == "scopes" else ("*",)
    if changed == "content":
        files = (replace(files[0], content=b"another"), files[1])
    elif changed == "base":
        files = (replace(files[0], base=ABSENT), files[1])
    elif changed == "order":
        files = tuple(reversed(files))
    elif changed == "mode":
        files = (replace(files[0], mode=EXECUTABLE_MODE), files[1])
    with pytest.raises(WorkspaceBrokerError, match="bound to another"):
        env.publisher.publish("batch", session, files, scopes, changed == "case")


def test_conflict_preserves_whole_candidate_and_writes_nothing(
    batch_env: BatchEnvironment,
) -> None:
    env = batch_env
    first_files = env.files()
    second_files = tuple(
        replace(item, content=b"second " + item.content) for item in first_files
    )
    env.publisher.publish("first", "first", first_files, ("*",))
    result = env.publisher.publish("second", "second", second_files, ("*",))
    assert result.state == "diverged"
    assert result.workspace_revision is None
    assert _events(env) == 1
    with env.store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM workspace_batches WHERE batch_id = 'second'"
        ).fetchone()
    request = json.loads(row["request_json"])
    observations = json.loads(row["observations_json"])
    assert set(observations) == {"a.txt", "docs/b.txt"}
    for item, entry in zip(second_files, request["files"], strict=True):
        assert (
            load_candidate_content(env.publisher.candidate_dir, entry["content_hash"])
            == item.content
        )
    assert all(
        (env.repository / item.relative_path).read_bytes() == item.content
        for item in first_files
    )
    assert not env.publisher.blocks_workspace(env.workspace_id)
    assert env.publisher.recover() == []


@pytest.mark.parametrize("crash_after", [0, 1, 2])
def test_restart_rolls_forward_every_crash_boundary_once(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch, crash_after: int
) -> None:
    env = batch_env
    files = env.files()
    original = batches.atomic_replace_regular_file
    calls = 0

    def interrupted(**kwargs: Any) -> None:
        nonlocal calls
        if calls == crash_after:
            raise RuntimeError("simulated process death")
        original(**kwargs)
        calls += 1
        if calls == 2 and crash_after == 2:
            raise RuntimeError("simulated process death")

    with monkeypatch.context() as patch:
        patch.setattr(batches, "atomic_replace_regular_file", interrupted)
        with pytest.raises(RuntimeError, match="process death"):
            env.publisher.publish("crash", "first", files, ("*",))
    assert env.publisher.blocks_workspace(env.workspace_id)
    assert _events(env) == 0
    restored = SharedBatchPublisher(
        env.store, env.publisher.candidate_dir, threading.RLock()
    )
    (result,) = restored.recover("crash")
    assert result.state == "published"
    assert result.workspace_revision == 1
    assert _events(env) == 1
    assert all(
        (env.repository / item.relative_path).read_bytes() == item.content
        for item in files
    )
    assert restored.recover() == []
    assert restored.publish("crash", "first", files, ("*",)) == result


def test_third_state_blocks_workspace_until_explicit_recovery(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env
    files = env.files()
    original = batches.atomic_replace_regular_file
    calls = 0

    def interrupted(**kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("crash")
        original(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(batches, "atomic_replace_regular_file", interrupted)
        with pytest.raises(RuntimeError):
            env.publisher.publish("crash", "first", files, ("*",))
    (env.repository / "docs/b.txt").write_bytes(b"external edit")
    (result,) = env.publisher.recover()
    assert result.state == "operator_attention"
    assert (env.repository / "docs/b.txt").read_bytes() == b"external edit"
    assert _events(env) == 0
    with pytest.raises(WorkspaceBrokerError, match="unresolved batch"):
        env.publisher.publish("blocked", "second", env.files(), ("*",))
    (env.repository / "docs/b.txt").write_bytes(b"old b\n")
    # An ordinary identical retry cannot quietly resolve operator attention.
    assert (
        env.publisher.publish("crash", "first", files, ("*",)).state
        == "operator_attention"
    )
    assert env.publisher.blocks_workspace(env.workspace_id)
    assert env.publisher.recover("crash")[0].state == "published"
    assert not env.publisher.blocks_workspace(env.workspace_id)


def test_materialized_results_are_verified_before_revision(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env
    original = batches.atomic_replace_regular_file

    def external_edit(**kwargs: Any) -> None:
        original(**kwargs)
        if kwargs["relative_path"] == "docs/b.txt":
            (env.repository / "a.txt").write_bytes(b"external after first rename")

    monkeypatch.setattr(batches, "atomic_replace_regular_file", external_edit)
    result = env.publisher.publish("batch", "first", env.files(), ("*",))
    assert result.state == "operator_attention"
    assert result.workspace_revision is None
    assert _events(env) == 0
    assert env.publisher.blocks_workspace(env.workspace_id)


def test_equal_base_and_result_still_gets_one_verified_publication(
    batch_env: BatchEnvironment,
) -> None:
    env = batch_env
    files = tuple(
        replace(item, content=(env.repository / item.relative_path).read_bytes())
        for item in env.files()
    )
    assert (
        env.publisher.publish("unchanged", "first", files, ("*",)).state == "published"
    )
    assert _events(env) == 1


def test_new_file_and_executable_result(batch_env: BatchEnvironment) -> None:
    env = batch_env
    files = (BatchFile("new.sh", ABSENT, b"#!/bin/sh\n", EXECUTABLE_MODE), *env.files())
    assert env.publisher.publish("new", "first", files, ("*",)).state == "published"
    assert identify_path(env.repository / "new.sh").mode == EXECUTABLE_MODE


@pytest.mark.parametrize(
    "invalid",
    [
        "scope",
        "traversal",
        "admin",
        "duplicate",
        "alias",
        "parent",
        "binary",
        "oversize",
        "many",
        "aggregate",
        "base",
    ],
)
def test_unsupported_inputs_fail_before_checkout_writes(
    batch_env: BatchEnvironment, invalid: str
) -> None:
    env = batch_env
    files = env.files()
    scopes = ("docs/",) if invalid == "scope" else ("*",)
    if invalid == "traversal":
        files = (replace(files[0], relative_path="../escape"),)
    elif invalid == "admin":
        files = (replace(files[0], relative_path=".git/config"),)
    elif invalid == "duplicate":
        files = (files[0], files[0])
    elif invalid == "alias":
        files = (files[0], replace(files[1], relative_path="A.TXT"))
    elif invalid == "parent":
        files = (replace(files[0], relative_path="missing/new.txt", base=ABSENT),)
    elif invalid == "binary":
        files = (replace(files[0], content=b"\xff"),)
    elif invalid == "oversize":
        files = (replace(files[0], content=b"a" * (batches.MAX_SHARED_TEXT_BYTES + 1)),)
    elif invalid == "many":
        files = tuple(
            replace(files[0], relative_path=f"file-{n}", base=ABSENT) for n in range(51)
        )
    elif invalid == "aggregate":
        files = tuple(
            replace(
                files[0],
                relative_path=f"file-{n}",
                base=ABSENT,
                content=b"a" * batches.MAX_SHARED_TEXT_BYTES,
            )
            for n in range(17)
        )
    elif invalid == "base":
        files = (replace(files[0], base=replace(ABSENT, digest="0" * 64)),)
    with pytest.raises(ValueError):
        env.publisher.publish("invalid", "first", files, scopes, invalid == "alias")
    assert (env.repository / "a.txt").read_bytes() == b"old a\n"
    assert (env.repository / "docs/b.txt").read_bytes() == b"old b\n"
    assert _events(env) == 0
    assert env.publisher.get("invalid") is None


def test_inactive_session_is_refused(batch_env: BatchEnvironment) -> None:
    env = batch_env
    with env.store.connection() as connection:
        connection.execute(
            "UPDATE sessions SET state = 'closed' WHERE session_id = 'first'"
        )
    with pytest.raises(WorkspaceBrokerError, match="active shared"):
        env.publisher.publish("batch", "first", env.files(), ("*",))


def test_recovery_reconciles_journal_even_after_session_closes(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env

    def interrupted(**kwargs: Any) -> None:
        raise RuntimeError("crash before rename")

    with monkeypatch.context() as patch:
        patch.setattr(batches, "atomic_replace_regular_file", interrupted)
        with pytest.raises(RuntimeError):
            env.publisher.publish("crash", "first", env.files(), ("*",))
    with env.store.connection() as connection:
        connection.execute("UPDATE sessions SET state = 'closed'")
    assert env.publisher.recover("crash")[0].state == "published"


def test_corrupt_candidate_body_keeps_journal_and_checkout_safe(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env
    files = env.files()

    def interrupted(**kwargs: Any) -> None:
        raise RuntimeError("crash before rename")

    with monkeypatch.context() as patch:
        patch.setattr(batches, "atomic_replace_regular_file", interrupted)
        with pytest.raises(RuntimeError):
            env.publisher.publish("crash", "first", files, ("*",))
    digest = hashlib.sha256(files[1].content).hexdigest()
    (env.publisher.candidate_dir / digest).write_bytes(b"corrupt")
    assert env.publisher.recover("crash")[0].state == "operator_attention"
    assert (env.repository / "a.txt").read_bytes() == b"old a\n"
    assert (env.repository / "docs/b.txt").read_bytes() == b"old b\n"
    assert env.publisher.blocks_workspace(env.workspace_id)
    assert _events(env) == 0


@pytest.mark.parametrize("parent_kind", ["symlink", "nested_repository"])
def test_parent_cannot_redirect_batch_outside_checkout_boundary(
    batch_env: BatchEnvironment, tmp_path: Path, parent_kind: str
) -> None:
    env = batch_env
    if parent_kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        (env.repository / "escape").symlink_to(outside, target_is_directory=True)
    else:
        (env.repository / "escape/.git").mkdir(parents=True)
    candidate = BatchFile("escape/new.txt", ABSENT, b"content", REGULAR_MODE)
    with pytest.raises(WorkspaceBrokerError):
        env.publisher.publish("unsafe", "first", (candidate,), ("*",))
    assert not (env.repository / "escape/new.txt").exists()
    assert env.publisher.get("unsafe") is None


def test_case_policy_cannot_be_weakened_by_caller(batch_env: BatchEnvironment) -> None:
    env = batch_env
    with env.store.connection() as connection:
        connection.execute("UPDATE checkouts SET path_case_insensitive = 1")
    first, second = env.files()
    second = replace(second, relative_path="A.TXT")
    with pytest.raises(WorkspaceBrokerError, match="aliases"):
        env.publisher.publish("alias", "first", (first, second), ("*",))
    assert env.publisher.get("alias") is None


def test_unreadable_or_nontext_preflight_is_durable_divergence(
    batch_env: BatchEnvironment,
) -> None:
    env = batch_env
    files = env.files()
    (env.repository / "docs/b.txt").write_bytes(b"\xff")
    result = env.publisher.publish("binary", "first", files, ("*",))
    assert result.state == "diverged"
    assert (env.repository / "a.txt").read_bytes() == b"old a\n"
    assert (env.repository / "docs/b.txt").read_bytes() == b"\xff"
    assert _events(env) == 0
