"""Independent acceptance checks for exact isolation and retained private inputs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.agent import source_policy
from llm_cli.errors import LlmCoordError
from llm_cli.workspace import isolated
from llm_cli.workspace.isolated import IsolatedWorkspaceManager


def manager(env: BatchEnvironment, tmp_path: Path) -> IsolatedWorkspaceManager:
    return IsolatedWorkspaceManager(
        env.store,
        tmp_path / "private-worktrees",
        env.publisher.candidate_dir,
        env.publisher.lock,
        lambda workspace: None,
    )


def checkout(env: BatchEnvironment) -> Any:
    shared = env.store.get_workspace(env.workspace_id)
    assert shared is not None
    value = env.store.get_checkout(shared.checkout_id)
    assert value is not None
    return value


def test_creation_preserves_dirty_staged_deleted_and_untracked_source(
    batch_env: BatchEnvironment, tmp_path: Path, git_run: Any
) -> None:
    env = batch_env
    (env.repository / "a.txt").write_text("staged\n")
    git_run(env.repository, "add", "a.txt")
    (env.repository / "a.txt").write_text("new working bytes\n")
    (env.repository / "docs/b.txt").unlink()
    (env.repository / "new.txt").write_text("untracked\n")
    before = tuple(
        git_run(env.repository, *args)
        for args in (
            ("rev-parse", "HEAD"),
            ("write-tree",),
            ("status", "--porcelain=v1"),
        )
    )
    workspace, _ = manager(env, tmp_path).open(checkout(env), "isolated-source")
    private = Path(workspace.canonical_path)
    assert (private / "a.txt").read_text() == "new working bytes\n"
    assert not (private / "docs/b.txt").exists()
    assert (private / "new.txt").read_text() == "untracked\n"
    (private / "a.txt").write_text("private later edit\n")
    assert (env.repository / "a.txt").read_text() == "new working bytes\n"
    assert before == tuple(
        git_run(env.repository, *args)
        for args in (
            ("rev-parse", "HEAD"),
            ("write-tree",),
            ("status", "--porcelain=v1"),
        )
    )


def test_approved_runtime_is_private_and_excluded_after_ignore_change_and_restart(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    (env.repository / ".gitignore").write_text(".env\nsettings.local\nnode_modules/\n")
    secret = b"LOCAL_TEST_TOKEN=private-runtime-fixture\n"
    (env.repository / ".env").write_bytes(secret)
    (env.repository / "settings.local").write_bytes(b"opaque runtime settings\n")
    dependencies = env.repository / "node_modules"
    dependencies.mkdir()
    (dependencies / "module.js").write_text("opaque dependency\n")
    isolate = manager(env, tmp_path)
    workspace, notices = isolate.open(
        checkout(env), "isolated-runtime", approved_secrets=(".env",)
    )
    private = Path(workspace.canonical_path)
    assert (private / ".env").read_bytes() == secret
    assert not (private / ".env").stat().st_mode & 0o177
    assert not private.stat().st_mode & 0o077
    assert (private / ".env").stat().st_ino != (env.repository / ".env").stat().st_ino
    assert not (private / "node_modules").exists()
    assert any("node_modules" in notice for notice in notices)
    (private / ".gitignore").write_text("")
    assert source_policy.excluded_paths(private, [".env", "settings.local"]) == {
        ".env",
        "settings.local",
    }
    source_policy._runtime_roots.clear()
    manager(env, tmp_path).recover()
    assert "settings.local" in source_policy.excluded_paths(private, ["settings.local"])
    state = isolate.get(workspace.workspace_id)
    assert state is not None and ".env" not in state.source_manifest
    assert "settings.local" not in isolated.capture_source(
        private, env.publisher.candidate_dir
    )
    (private / ".env").write_bytes(b"private modified runtime\n")
    assert (env.repository / ".env").read_bytes() == secret
    with env.store.connection() as connection:
        rows = connection.execute("SELECT * FROM isolated_workspaces").fetchall()
    assert all(secret.decode().strip() not in repr(tuple(row)) for row in rows)
    assert all(
        secret not in path.read_bytes()
        for path in env.publisher.candidate_dir.iterdir()
        if path.is_file()
    )


def test_runtime_secret_is_not_copied_without_exact_consent(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    (env.repository / ".gitignore").write_text(".env\n")
    (env.repository / ".env").write_text("LOCAL_TEST_TOKEN=private\n")
    with pytest.raises(LlmCoordError, match="consent"):
        manager(env, tmp_path).open(checkout(env), "no-consent")
    assert not list((tmp_path / "private-worktrees").glob("*"))


def test_recovery_preserves_unknown_files_after_interrupted_creation(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    isolate = manager(env, tmp_path)

    class Crash(BaseException):
        pass

    def interrupt(stage: str) -> None:
        if stage == "materialized":
            private = next((tmp_path / "private-worktrees").iterdir())
            (private / "unexpected-user-data.txt").write_text("preserve me\n")
            raise Crash()

    isolate.fault = interrupt
    with pytest.raises(Crash):
        isolate.open(checkout(env), "interrupted")
    manager(env, tmp_path).recover()
    private = next((tmp_path / "private-worktrees").iterdir())
    assert (private / "unexpected-user-data.txt").read_text() == "preserve me\n"
    assert (env.repository / "a.txt").read_text() == "old a\n"
    with env.store.connection() as connection:
        row = connection.execute("SELECT stage FROM isolated_workspaces").fetchone()
    assert row is not None and row[0] == "attention"


def test_runtime_fifo_is_rejected_without_waiting_for_a_writer(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "runtime.pipe")
    # O_NONBLOCK must precede the fstat type check; opening a FIFO normally hangs.
    with (
        pytest.raises(LlmCoordError, match="regular"),
        isolated._source(tmp_path, "runtime.pipe"),
    ):
        pytest.fail("a FIFO cannot become a runtime input")


def test_screened_source_fixture_copies_privately_without_model_artifacts(
    batch_env: BatchEnvironment, tmp_path: Path, git_run: Any
) -> None:
    from llm_cli.agent.shared_tools import SharedToolBroker

    env = batch_env
    body = b'FAKE_AWS = "' + b"AKIA" + b"EXAMPLE000000000" + b'"\n'
    fixture = env.repository / "fixture.py"
    fixture.write_bytes(body)
    git_run(env.repository, "add", "fixture.py")
    isolate = manager(env, tmp_path)
    workspace, _ = isolate.open(checkout(env), "screened-fixture")
    private = Path(workspace.canonical_path)
    assert (private / "fixture.py").read_bytes() == body
    state = isolate.get(workspace.workspace_id)
    assert state is not None
    assert state.source_manifest["fixture.py"]["screened"] is True
    assert state.source_manifest["fixture.py"]["content_hash"] is None
    broker = SharedToolBroker(worktree=private, scopes=("*",))
    result = broker.invoke("read_file", {"path": "fixture.py"})
    assert result.is_error and "recognized secret" in result.content
    assert broker.candidates() == ()
    assert all(
        body not in path.read_bytes() for path in env.publisher.candidate_dir.iterdir()
    )
    with env.store.connection() as con:
        rows = con.execute("SELECT * FROM isolated_workspaces").fetchall()
    assert body.decode().strip() not in repr([tuple(row) for row in rows])


def test_creation_retries_a_concurrent_source_save(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env
    original = isolated._capture_path
    raced = False

    def moving_save(root: Path, path: str) -> tuple[bytes, os.stat_result, bool]:
        nonlocal raced
        if path == "a.txt" and not raced:
            raced = True
            (root / path).write_text("saved while opening\n")
            raise isolated._SourceChanged
        return original(root, path)

    monkeypatch.setattr(isolated, "_capture_path", moving_save)
    workspace, _ = manager(env, tmp_path).open(checkout(env), "moving-save")
    assert raced
    assert (Path(workspace.canonical_path) / "a.txt").read_text() == (
        "saved while opening\n"
    )
