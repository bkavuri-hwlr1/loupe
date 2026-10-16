"""Isolated sessions keep exact source and local runtime across task boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from test_shared_agent_execution import (
    ScriptedProvider,
    _call,
    _finish,
    _register,
    _request,
    _start,
    _tools,
)
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.workspace.isolated import IsolatedWorkspaceManager


def _manager(env: BatchEnvironment, tmp_path: Path) -> IsolatedWorkspaceManager:
    return IsolatedWorkspaceManager(
        env.store,
        tmp_path / "private",
        env.publisher.candidate_dir,
        env.publisher.lock,
        lambda workspace: None,
    )


def _checkout(env: BatchEnvironment) -> Any:
    workspace = env.store.get_workspace(env.workspace_id)
    assert workspace is not None
    return env.store.get_checkout(workspace.checkout_id)


@pytest.mark.parametrize(
    "phase", ["planned", "registered", "materialized", "hydrated", "ready"]
)
def test_each_creation_crash_recovers_or_cleans_owned_files(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    phase: str,
) -> None:
    env = batch_env
    (env.repository / ".gitignore").write_text(".env\n")
    (env.repository / ".env").write_text("LOCAL_TOKEN=fixture\n")
    manager = _manager(env, tmp_path)

    class Crash(BaseException):
        pass

    def fail(stage: str) -> None:
        if stage == phase:
            raise Crash()

    manager.fault = fail
    with pytest.raises(Crash):
        manager.open(_checkout(env), "crash", approved_secrets=(".env",))
    _manager(env, tmp_path).recover()
    with env.store.connection() as con:
        rows = con.execute(
            "SELECT workspace_id,stage FROM isolated_workspaces"
        ).fetchall()
    if phase == "ready":
        assert len(rows) == 1 and rows[0]["stage"] == "ready"
        _manager(env, tmp_path).validate(rows[0]["workspace_id"])
    else:
        assert not rows
        assert not list((tmp_path / "private").glob("workspace_*"))
        for archive in (tmp_path / "private").glob(".recovery-*"):
            assert not archive.stat().st_mode & 0o077
        # A known interrupted creation can be retried without a manual cleanup.
        manager.fault = lambda stage: None
        manager.open(_checkout(env), "crash", approved_secrets=(".env",))
    assert (env.repository / "a.txt").read_text() == "old a\n"
    assert (env.repository / ".env").read_text() == "LOCAL_TOKEN=fixture\n"


def test_typed_source_is_materialized_without_following_links(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    env = batch_env
    (env.repository / "source-link").symlink_to("a.txt")
    binary = b"\x00\xff\x80image bytes"
    (env.repository / "asset.bin").write_bytes(binary)
    workspace, notices = _manager(env, tmp_path).open(_checkout(env), "typed")
    root = Path(workspace.canonical_path)
    assert os.readlink(root / "source-link") == "a.txt"
    assert (root / "asset.bin").read_bytes() == binary
    assert not any("source-link" in notice for notice in notices)


def test_isolated_task_checks_use_env_without_publishing_or_disclosing_it(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
) -> None:
    async def scenario() -> None:
        root = repository_factory(
            tmp_path, {"a.py": "value = 1\n", ".gitignore": ".env\n"}
        )
        secret = "opaque-test-runtime-value"
        (root / ".env").write_text(secret)
        service = service_factory(tmp_path)
        provider = ScriptedProvider(
            "isolated-checks",
            [
                _tools(
                    _call("read_file", path="a.py"), _call("read_file", path=".env")
                ),
                _tools(_call("write_file", path="a.py", content="value = 2\n")),
                _finish(),
            ],
        )
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials = {"session_id": provider.model, "resume_secret": "isolated-secret"}
        opened = await service.handle(
            _request(
                "session.open",
                {
                    "path": str(root),
                    "session_id": provider.model,
                    "resume_token_hash": hashlib.sha256(
                        credentials["resume_secret"].encode()
                    ).hexdigest(),
                    "provider": "scripted",
                    "model": provider.model,
                    "workspace": "isolated",
                    "mode": "normal",
                    "approved_runtime_files": [".env"],
                },
            )
        )
        await service.handle(
            _request(
                "session.ack", {**credentials, "sequence": opened["bootstrap_sequence"]}
            )
        )
        await service.handle(
            _request(
                "checks.configure",
                {
                    "path": str(root),
                    "config": {
                        "checks": {
                            "runtime": {
                                "argv": [
                                    sys.executable,
                                    "-c",
                                    "from pathlib import Path; "
                                    "assert Path('a.py').read_text() == "
                                    "'value = 2\\n'; "
                                    "data = Path('.env').read_text(); "
                                    "assert data; print(data); "
                                    "Path('.env').write_text('changed only in check')",
                                ],
                            }
                        }
                    },
                },
            )
        )
        try:
            work = await _start(service, root, credentials, "isolated-task", ("*",))
            await asyncio.wait_for(work, 30)
            rows = service.workflow.checks("isolated-task")
            assert rows and rows[0]["state"] == "passed", rows
            assert secret not in repr(rows)
            assert secret not in repr(service.store.list_task_events("isolated-task"))
            assert secret not in repr(provider.results)
            assert provider.results[0][1].is_error
            assert (root / "a.py").read_text() == "value = 1\n"
            assert (root / ".env").read_text() == secret
            private = Path(opened["workspace"]["canonical_path"])
            assert (private / ".env").read_text() == secret
            row = service.workflow.get("isolated-task")
            assert row is not None and row["status"] == "awaiting_review"
        finally:
            service.close()

    asyncio.run(scenario())


def test_equivalent_exclude_replacement_invalidates_creation(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Any,
) -> None:
    from llm_cli.errors import LlmCoordError

    env = batch_env
    rules = tmp_path / "global-ignore"
    rules.write_text("runtime.local\n")
    git_run(env.repository, "config", "core.excludesFile", str(rules))
    (env.repository / "runtime.local").write_text("private input\n")
    manager = _manager(env, tmp_path)

    def replace_policy(stage: str) -> None:
        if stage == "materialized":
            replacement = tmp_path / "replacement"
            replacement.write_text("runtime.local\n")
            replacement.replace(rules)

    manager.fault = replace_policy
    with pytest.raises(LlmCoordError, match="policy changed"):
        manager.open(_checkout(env), "changed-rules")
    assert (env.repository / "runtime.local").read_text() == "private input\n"


def test_broken_cow_probe_cannot_modify_primary_runtime(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.errors import LlmCoordError
    from llm_cli.workspace import isolated

    env = batch_env
    (env.repository / ".gitignore").write_text("runtime.local\n")
    (env.repository / "runtime.local").write_text("original runtime\n")
    seen_probe: list[bytes] = []

    def broken_clone(source: int, parent: int, name: str) -> bool:
        # Simulate a clone whose destination mutation corrupts its source.
        # The source passed to the probe must be disposable owned data.
        seen_probe.append(isolated._read(source, 1024))
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent
        )
        os.close(descriptor)
        writable = os.open("source", os.O_WRONLY, dir_fd=parent)
        try:
            os.write(writable, b"corrupted probe")
        finally:
            os.close(writable)
        return True

    monkeypatch.setattr(isolated, "_clone", broken_clone)
    with pytest.raises(LlmCoordError, match="clone did not isolate"):
        _manager(env, tmp_path).open(_checkout(env), "broken-clone")
    assert seen_probe == [b"loupe private clone probe"]
    assert (env.repository / "runtime.local").read_text() == "original runtime\n"


def test_cleanup_preserves_an_unknown_empty_directory(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    env = batch_env
    manager = _manager(env, tmp_path)

    class Crash(BaseException):
        pass

    def interrupt(stage: str) -> None:
        if stage == "materialized":
            private = next((tmp_path / "private").iterdir())
            (private / "user-created-empty-directory").mkdir()
            raise Crash()

    manager.fault = interrupt
    with pytest.raises(Crash):
        manager.open(_checkout(env), "empty-directory")
    manager.recover()
    private = next((tmp_path / "private").iterdir())
    assert (private / "user-created-empty-directory").is_dir()


def test_global_exclusion_protects_tracked_source_before_artifact_capture(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_run: Any,
) -> None:
    from llm_cli.errors import LlmCoordError
    from llm_cli.workspace.isolated import capture_source

    env = batch_env
    private = b"opaque local protected source fixture"
    (env.repository / "private.custom").write_bytes(private)
    git_run(env.repository, "add", "private.custom")
    rules = tmp_path / "global-ignore"
    rules.write_text("private.custom\n")
    config = tmp_path / "global-config"
    config.write_text(f"[core]\n    excludesFile = {rules}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    with pytest.raises(LlmCoordError, match="protected source"):
        capture_source(env.repository, env.publisher.candidate_dir)
    assert all(
        private not in path.read_bytes()
        for path in env.publisher.candidate_dir.glob("*")
        if path.is_file()
    )


def test_primary_runtime_declassification_is_refused_before_artifacts(
    batch_env: BatchEnvironment,
) -> None:
    from llm_cli.errors import LlmCoordError
    from llm_cli.workspace.isolated import capture_source

    env = batch_env
    private = b"unknown-format private runtime"
    (env.repository / "runtime.local").write_bytes(private)
    with pytest.raises(LlmCoordError, match="runtime path became source"):
        capture_source(
            env.repository,
            env.publisher.candidate_dir,
            protected_paths=("runtime.local",),
        )
    assert all(
        private not in path.read_bytes()
        for path in env.publisher.candidate_dir.glob("*")
        if path.is_file()
    )


class CreationCrash(BaseException):
    pass


def _interrupted_creation(
    env: BatchEnvironment, tmp_path: Path
) -> tuple[IsolatedWorkspaceManager, Path, str]:
    manager = _manager(env, tmp_path)

    def interrupt(stage: str) -> None:
        if stage == "materialized":
            raise CreationCrash()

    manager.fault = interrupt
    with pytest.raises(CreationCrash):
        manager.open(_checkout(env), "recovery-race")
    with env.store.connection() as con:
        workspace_id = str(
            con.execute("SELECT workspace_id FROM isolated_workspaces").fetchone()[0]
        )
    return manager, tmp_path / "private" / workspace_id, workspace_id


def test_cleanup_preserves_a_file_created_after_inventory(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    manager, private, workspace_id = _interrupted_creation(batch_env, tmp_path)

    def late_write(stage: str) -> None:
        if stage == "cleanup_verified":
            (private / "late-user-file").write_text("retain late bytes")

    manager.fault = late_write
    manager.recover()
    assert (private / "late-user-file").read_text() == "retain late bytes"
    state = manager.get(workspace_id)
    assert state is not None and state.stage == "attention"
    assert (
        tmp_path / "private" / (".recovery-" + workspace_id) / "files" / "a.txt"
    ).read_text() == "old a\n"


def test_cleanup_archives_late_replacement_without_erasing_it(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    manager, private, workspace_id = _interrupted_creation(batch_env, tmp_path)

    def late_write(stage: str) -> None:
        if stage == "cleanup_verified":
            replacement = private / "replacement"
            replacement.write_text("retain replacement bytes")
            replacement.replace(private / "a.txt")

    manager.fault = late_write
    manager.recover()
    archive = tmp_path / "private" / (".recovery-" + workspace_id)
    assert (archive / "files" / "a.txt").read_text() == "retain replacement bytes"
    state = manager.get(workspace_id)
    assert state is not None and state.stage == "attention"
    with batch_env.store.connection() as con:
        row = con.execute(
            "SELECT quarantine_reason FROM workspaces WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
    assert row is not None and str(archive) in row[0]


def test_cleanup_resumes_after_archiving_nested_source(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    manager, private, workspace_id = _interrupted_creation(batch_env, tmp_path)
    archive = tmp_path / "private" / (".recovery-" + workspace_id)

    def interrupt(stage: str) -> None:
        if stage == "cleanup_archived" and (archive / "files/docs/b.txt").exists():
            raise CreationCrash()

    manager.fault = interrupt
    with pytest.raises(CreationCrash):
        manager.recover()
    assert not (private / "docs/b.txt").exists()
    assert (archive / "files/docs/b.txt").read_text() == "old b\n"
    _manager(batch_env, tmp_path).recover()
    assert manager.get(workspace_id) is None
    assert not private.exists()
    assert (archive / "files/docs/b.txt").read_text() == "old b\n"


def test_registration_before_owner_marker_recovers_known_empty_worktree(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.workspace import isolated

    real_create = isolated.create_managed_worktree

    def interrupt_after_registration(*args: Any, **kwargs: Any) -> Any:
        real_create(*args, **kwargs)
        raise CreationCrash()

    monkeypatch.setattr(
        isolated, "create_managed_worktree", interrupt_after_registration
    )
    manager = _manager(batch_env, tmp_path)
    with pytest.raises(CreationCrash):
        manager.open(_checkout(batch_env), "registration-window")
    with batch_env.store.connection() as con:
        workspace_id = str(
            con.execute("SELECT workspace_id FROM isolated_workspaces").fetchone()[0]
        )
    manager.recover()
    assert manager.get(workspace_id) is None
    assert not (tmp_path / "private" / workspace_id).exists()


def test_cleanup_retains_later_writes_through_an_open_source_descriptor(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    manager, private, workspace_id = _interrupted_creation(batch_env, tmp_path)
    archive = tmp_path / "private" / (".recovery-" + workspace_id)
    descriptor = os.open(private / "a.txt", os.O_WRONLY)
    try:
        manager.fault = lambda stage: None
        manager.recover()
        assert manager.get(workspace_id) is None
        os.write(descriptor, b"late open-descriptor write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    assert (archive / "files/a.txt").read_bytes() == b"late open-descriptor write"
    assert not private.exists()


@pytest.mark.parametrize(
    "alias", ["LOCAL.DATA", "Données/LOCAL.DATA", "Donne\u0301es/LOCAL.DATA"]
)
def test_runtime_spelling_alias_cannot_enter_model_tools_or_source_artifacts(
    batch_env: BatchEnvironment, tmp_path: Path, alias: str
) -> None:
    from llm_cli.agent.shared_tools import SharedToolBroker
    from llm_cli.agent.source_policy import excluded_paths, register_runtime_paths
    from llm_cli.errors import LlmCoordError
    from llm_cli.workspace.isolated import capture_source

    env = batch_env
    protected = "local.data" if "/" not in alias else "données"
    private = b"opaque runtime spelling alias bytes"
    target = env.repository / alias
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(private)
    # Explicit durable protection applies before a primary path is registered.
    with pytest.raises(LlmCoordError, match="runtime path became source"):
        capture_source(
            env.repository, env.publisher.candidate_dir, protected_paths=(protected,)
        )
    register_runtime_paths(env.repository, (protected,))
    # No ignore rule remains to protect the alias. Frozen policy must do so.
    assert excluded_paths(env.repository, [alias]) == {alias}
    broker = SharedToolBroker(worktree=env.repository, scopes=("*",))
    result = broker.invoke("read_file", {"path": alias})
    assert result.is_error
    assert private.decode() not in result.content
    assert alias not in capture_source(env.repository, env.publisher.candidate_dir)
    assert all(
        private not in path.read_bytes()
        for path in env.publisher.candidate_dir.glob("*")
        if path.is_file()
    )
