"""Publication must not turn private checkout files into public files."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_shared_agent_execution import (
    ScriptedProvider,
    _assert_completed,
    _call,
    _finish,
    _request,
    _tools,
)
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env
from test_verified_workflow import await_task, setup

from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.daemon.service import DaemonService
from llm_cli.workspace import batches, broker
from llm_cli.workspace.batches import BatchFile
from llm_cli.workspace.identity import (
    ABSENT,
    EXECUTABLE_MODE,
    REGULAR_MODE,
    identify_path,
)


@pytest.mark.parametrize("permissions", [0o600, 0o640, 0o700])
def test_edit_preserves_filesystem_permissions(
    batch_env: BatchEnvironment, permissions: int
) -> None:
    path = batch_env.repository / "a.txt"
    path.chmod(permissions)
    base = identify_path(path)
    candidate = BatchFile("a.txt", base, b"private replacement\n", base.mode)
    result = batch_env.publisher.publish("private", "first", (candidate,), ("*",))
    assert result.state == "published"
    assert stat.S_IMODE(path.stat().st_mode) == permissions


@pytest.mark.parametrize("umask", [0o000, 0o077])
@pytest.mark.parametrize(
    "mode,expected", [(REGULAR_MODE, 0o600), (EXECUTABLE_MODE, 0o700)]
)
def test_new_files_remain_private_under_restrictive_umask(
    batch_env: BatchEnvironment, mode: str, expected: int, umask: int
) -> None:
    path = batch_env.repository / "new.txt"
    old_mask = os.umask(umask)
    try:
        result = batch_env.publisher.publish(
            "private", "first", (BatchFile("new.txt", ABSENT, b"new\n", mode),), ("*",)
        )
    finally:
        os.umask(old_mask)
    assert result.state == "published"
    assert stat.S_IMODE(path.stat().st_mode) == expected


def test_git_mode_change_only_adds_owner_execute(batch_env: BatchEnvironment) -> None:
    path = batch_env.repository / "a.txt"
    path.chmod(0o640)
    candidate = BatchFile("a.txt", identify_path(path), b"script\n", EXECUTABLE_MODE)
    result = batch_env.publisher.publish("executable", "first", (candidate,), ("*",))
    assert result.state == "published"
    assert stat.S_IMODE(path.stat().st_mode) == 0o740
    assert identify_path(path).mode == EXECUTABLE_MODE


def test_legacy_checkpoint_edit_preserves_live_permissions(
    batch_env: BatchEnvironment,
) -> None:
    env = batch_env
    path = env.repository / "a.txt"
    path.chmod(0o640)
    tools = SharedToolBroker(worktree=env.repository, scopes=("*",))
    assert not tools.invoke(
        "apply_patch", {"path": "a.txt", "old_text": "old", "new_text": "new"}
    ).is_error
    saved = json.loads(json.dumps(tools.usage_snapshot()))
    state = saved["shared_workspace_state"]
    state["version"] = 2
    for item in state["files"]:
        item.pop("permissions")
        item["base"].pop("permissions")
    restored = SharedToolBroker(worktree=env.repository, scopes=("*",))
    restored.restore_usage(saved)
    result = env.publisher.publish("legacy", "first", restored.candidates(), ("*",))
    assert result.state == "published"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_recovery_does_not_widen_permissions_tightened_after_replacement(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env
    path = env.repository / "a.txt"
    path.chmod(0o640)
    candidate = BatchFile("a.txt", identify_path(path), b"private\n", REGULAR_MODE)
    original = batches.atomic_replace_regular_file

    def crash(**kwargs: Any) -> None:
        original(**kwargs)
        raise RuntimeError("process died after replacement")

    with monkeypatch.context() as patch:
        patch.setattr(batches, "atomic_replace_regular_file", crash)
        with pytest.raises(RuntimeError, match="process died"):
            env.publisher.publish("private", "first", (candidate,), ("*",))
    path.chmod(0o600)
    (result,) = env.publisher.recover("private")
    assert result.state == "published"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("permissions", [0o600, 0o640, 0o700])
def test_renamed_file_keeps_access_across_checkpoint_and_crash(
    batch_env: BatchEnvironment, permissions: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env
    source, target = env.repository / "a.txt", env.repository / "renamed.txt"
    source.chmod(permissions)
    tools = SharedToolBroker(worktree=env.repository, scopes=("*",))
    result = tools.invoke(
        "rename_file", {"source": "a.txt", "destination": "renamed.txt"}
    )
    assert not result.is_error, result.content
    restored = SharedToolBroker(worktree=env.repository, scopes=("*",))
    restored.restore_usage(json.loads(json.dumps(tools.usage_snapshot())))

    def crash(**kwargs: Any) -> None:
        raise RuntimeError("process died after removing source")

    with monkeypatch.context() as patch:
        patch.setattr(batches, "atomic_replace_regular_file", crash)
        with pytest.raises(RuntimeError, match="process died"):
            env.publisher.publish("rename", "first", restored.candidates(), ("*",))
    assert not source.exists() and not target.exists()
    (recovered,) = env.publisher.recover("rename")
    assert recovered.state == "published"
    assert stat.S_IMODE(target.stat().st_mode) == permissions


def test_user_tightening_permissions_during_staging_is_preserved(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env
    path = env.repository / "a.txt"
    path.chmod(0o644)
    original = broker.workspace_target
    calls = 0

    def tighten(*args: Any, **kwargs: Any) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            path.chmod(0o600)
        return original(*args, **kwargs)

    monkeypatch.setattr(broker, "workspace_target", tighten)
    candidate = BatchFile("a.txt", identify_path(path), b"private\n", REGULAR_MODE)
    result = env.publisher.publish("tightened", "first", (candidate,), ("*",))
    assert result.state == "published"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_renaming_refuses_permissions_changed_since_observation(
    batch_env: BatchEnvironment,
) -> None:
    env = batch_env
    path = env.repository / "a.txt"
    path.chmod(0o640)
    tools = SharedToolBroker(worktree=env.repository, scopes=("*",))
    assert not tools.invoke(
        "rename_file", {"source": "a.txt", "destination": "renamed.txt"}
    ).is_error
    path.chmod(0o600)
    result = env.publisher.publish("changed", "first", tools.candidates(), ("*",))
    assert result.state == "diverged"
    assert path.exists() and not (env.repository / "renamed.txt").exists()


def test_legacy_journal_keeps_live_permissions_on_recovery(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env
    path = env.repository / "a.txt"
    path.chmod(0o640)

    def crash(**kwargs: Any) -> None:
        raise RuntimeError("process died")

    with monkeypatch.context() as patch:
        patch.setattr(batches, "atomic_replace_regular_file", crash)
        with pytest.raises(RuntimeError, match="process died"):
            env.publisher.publish(
                "legacy",
                "first",
                (BatchFile("a.txt", identify_path(path), b"restored\n", REGULAR_MODE),),
                ("*",),
            )
    with env.store.connection() as connection:
        row = connection.execute(
            "SELECT request_json FROM workspace_batches WHERE batch_id='legacy'"
        ).fetchone()
        request = json.loads(row[0])
        for entry in request["files"]:
            entry["base"].pop("permissions", None)
            entry["result"].pop("permissions", None)
        connection.execute(
            "UPDATE workspace_batches SET request_json=? WHERE batch_id='legacy'",
            (json.dumps(request),),
        )
    (result,) = env.publisher.recover("legacy")
    assert result.state == "published"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.parametrize("permissions", [0o600, 0o640, 0o700])
def test_undo_restores_permissions_of_renamed_and_deleted_files(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    permissions: int,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n", "old.txt": "old\n"})
        (root / "a.py").chmod(permissions)
        (root / "old.txt").chmod(permissions)
        service = service_factory(tmp_path)
        provider = ScriptedProvider(
            "permission-session",
            [
                _tools(
                    _call("rename_file", source="a.py", destination="renamed.py"),
                    _call("delete_file", path="old.txt"),
                ),
                _finish(),
            ],
        )
        creds = await setup(service, root, provider)
        await await_task(service, root, creds)
        _assert_completed(service, "task")
        assert stat.S_IMODE((root / "renamed.py").stat().st_mode) == permissions
        undo = await service.handle(_request("task.undo", {"task_id": "task"}))
        assert undo["state"] == "published"
        for name in ("a.py", "old.txt"):
            assert stat.S_IMODE((root / name).stat().st_mode) == permissions

    asyncio.run(scenario())
