"""Closed private workspaces retire without retaining copied private inputs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from test_isolated_sessions import CreationCrash, _checkout, _manager
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

from llm_cli.git.worktrees import list_git_worktrees


def test_closing_idle_isolated_session_removes_runtime_and_git_registration(
    tmp_path: Path, repository_factory: Any, service_factory: Any
) -> None:
    async def scenario() -> None:
        primary = repository_factory(
            tmp_path, {"a.txt": "original\n", ".gitignore": ".env\n"}
        )
        (primary / ".env").write_text("LOCAL_TOKEN=private fixture\n")
        service = service_factory(tmp_path)
        _register(service, ScriptedProvider("close-idle", []))
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(primary)}))
        credentials = {"session_id": "close-idle", "resume_secret": "close-secret"}
        try:
            opened = await service.handle(
                _request(
                    "session.open",
                    {
                        "path": str(primary),
                        "session_id": credentials["session_id"],
                        "resume_token_hash": hashlib.sha256(
                            credentials["resume_secret"].encode()
                        ).hexdigest(),
                        "provider": "scripted",
                        "model": "close-idle",
                        "workspace": "isolated",
                        "approved_runtime_files": [".env"],
                    },
                )
            )
            private = Path(opened["workspace"]["canonical_path"])
            workspace_id = opened["workspace"]["workspace_id"]
            assert (private / ".env").exists()
            await service.handle(_request("session.close", credentials))
            assert not private.exists()
            assert private not in {
                record.path for record in list_git_worktrees(primary)
            }
            assert service.store.get_workspace(workspace_id).state == "removed"
            assert not list(private.parent.glob(".recovery-*/files/.env"))
            await service.handle(_request("session.close", credentials))
            assert (primary / ".env").read_text() == "LOCAL_TOKEN=private fixture\n"
        finally:
            service.close()

    asyncio.run(scenario())


def test_closed_held_proposal_keeps_runtime_through_restart_until_discard(
    tmp_path: Path, repository_factory: Any, service_factory: Any
) -> None:
    async def scenario() -> None:
        primary = repository_factory(
            tmp_path, {"a.py": "value = 1\n", ".gitignore": ".env\n"}
        )
        (primary / ".env").write_text("private held runtime")
        service = service_factory(tmp_path)
        provider = ScriptedProvider(
            "close-held",
            [
                _tools(_call("read_file", path="a.py")),
                _tools(_call("write_file", path="a.py", content="value = 2\n")),
                _finish(),
            ],
        )
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(primary)}))
        credentials = {"session_id": provider.model, "resume_secret": "close-secret"}
        opened = await service.handle(
            _request(
                "session.open",
                {
                    "path": str(primary),
                    "session_id": credentials["session_id"],
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
        private = Path(opened["workspace"]["canonical_path"])
        await service.handle(
            _request(
                "session.ack", {**credentials, "sequence": opened["bootstrap_sequence"]}
            )
        )
        try:
            await asyncio.wait_for(
                await _start(service, primary, credentials, "close-held-task", ("*",)),
                30,
            )
            assert (
                service.workflow.get("close-held-task")["status"] == "awaiting_review"
            )
            await service.handle(_request("session.close", credentials))
            assert (private / ".env").read_text() == "private held runtime"
        finally:
            service.close()
        restarted = service_factory(tmp_path)
        _register(restarted, ScriptedProvider(provider.model, []))
        try:
            restarted.initialize()
            assert (private / ".env").read_text() == "private held runtime"
            result = await restarted.handle(
                _request("task.discard", {"task_id": "close-held-task"})
            )
            assert result["state"] == "discarded"
            assert not private.exists()
            assert private not in {
                record.path for record in list_git_worktrees(primary)
            }
            assert not list(private.parent.glob(".recovery-*/files/.env"))
            assert (primary / "a.py").read_text() == "value = 1\n"
        finally:
            restarted.close()

    asyncio.run(scenario())


def test_restart_settles_a_completed_unchanged_task_so_close_cleans_up(
    tmp_path: Path, repository_factory: Any, service_factory: Any
) -> None:
    async def scenario() -> None:
        primary = repository_factory(
            tmp_path, {"a.py": "value = 1\n", ".gitignore": ".env\n"}
        )
        (primary / ".env").write_text("private settled runtime")
        service = service_factory(tmp_path)
        provider = ScriptedProvider(
            "close-settled", [_tools(_call("read_file", path="a.py")), _finish()]
        )
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(primary)}))
        credentials = {"session_id": provider.model, "resume_secret": "close-secret"}
        opened = await service.handle(
            _request(
                "session.open",
                {
                    "path": str(primary),
                    "session_id": credentials["session_id"],
                    "resume_token_hash": hashlib.sha256(
                        credentials["resume_secret"].encode()
                    ).hexdigest(),
                    "provider": "scripted",
                    "model": provider.model,
                    "workspace": "isolated",
                    "approved_runtime_files": [".env"],
                },
            )
        )
        private = Path(opened["workspace"]["canonical_path"])
        await service.handle(
            _request(
                "session.ack", {**credentials, "sequence": opened["bootstrap_sequence"]}
            )
        )
        try:
            await asyncio.wait_for(
                await _start(service, primary, credentials, "settled-task", ("*",)),
                30,
            )
            assert service.workflow.get("settled-task")["status"] == "published"
            # A daemon killed after settlement but before the disposition write
            # leaves exactly this row behind.
            with service.store.connection() as con:
                con.execute(
                    "UPDATE task_workflows SET status='running',expires_at=NULL "
                    "WHERE task_id='settled-task'"
                )
        finally:
            service.close()
        restarted = service_factory(tmp_path)
        _register(restarted, ScriptedProvider(provider.model, []))
        try:
            restarted.initialize()
            workflow = restarted.workflow.get("settled-task")
            assert workflow["status"] == "published"
            assert workflow["publication_id"] is None
            assert workflow["expires_at"] is not None
            await restarted.handle(_request("session.close", credentials))
            assert not private.exists()
            assert (primary / ".env").read_text() == "private settled runtime"
        finally:
            restarted.close()

    asyncio.run(scenario())


def _closed_workspace(env: BatchEnvironment, tmp_path: Path) -> tuple[Any, Any, Path]:
    (env.repository / ".gitignore").write_text(".env\n")
    (env.repository / ".env").write_text("private close runtime")
    manager = _manager(env, tmp_path)
    workspace, _ = manager.open(
        _checkout(env), "close-fixture", approved_secrets=(".env",)
    )
    env.store.open_session(
        session_id="close-fixture",
        checkout_id=workspace.checkout_id,
        resume_token_hash=hashlib.sha256(b"close-fixture").hexdigest(),
        provider="fake",
        model="fake",
        workspace_id=workspace.workspace_id,
        workspace_mode="isolated",
    )
    env.store.close_session("close-fixture")
    return manager, workspace, Path(workspace.canonical_path)


def test_closed_cleanup_restarts_and_preserves_late_source_descriptor_writes(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    manager, workspace, private = _closed_workspace(batch_env, tmp_path)
    descriptor = os.open(private / "a.txt", os.O_WRONLY)

    def interrupt(stage: str) -> None:
        if stage == "cleanup_archived":
            raise CreationCrash()

    manager.fault = interrupt
    try:
        with pytest.raises(CreationCrash):
            manager.cleanup_closed()
        assert not (private / ".env").exists()
        _manager(batch_env, tmp_path).recover()
        assert not private.exists()
        os.write(descriptor, b"late source descriptor write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    archive = private.parent / (".recovery-" + workspace.workspace_id) / "files"
    assert (archive / "a.txt").read_bytes() == b"late source descriptor write"
    assert not (archive / ".env").exists()
    assert batch_env.store.get_workspace(workspace.workspace_id).state == "removed"
    _manager(batch_env, tmp_path).recover()


def test_closed_cleanup_preserves_new_user_path_and_scrubs_private_runtime(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    manager, workspace, private = _closed_workspace(batch_env, tmp_path)
    (private / "user-added.txt").write_text("keep this private user edit")
    manager.cleanup_closed()
    assert (private / "user-added.txt").read_text() == "keep this private user edit"
    assert not (private / ".env").exists()
    assert batch_env.store.get_workspace(workspace.workspace_id).state == "quarantined"
    assert (batch_env.repository / ".env").read_text() == "private close runtime"


def test_cleanup_scrubs_owned_runtime_directory_without_following_links(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    manager, workspace, private = _closed_workspace(batch_env, tmp_path)
    outside = tmp_path / "outside-runtime"
    outside.mkdir()
    (outside / "keep").write_text("outside user bytes")
    dependency = private / ".venv"
    dependency.mkdir()
    (dependency / "nested").mkdir()
    (dependency / "nested/private").write_text("private dependency bytes")
    (dependency / "outside-link").symlink_to(outside, target_is_directory=True)
    with batch_env.store.connection() as con:
        con.execute(
            "UPDATE isolated_workspaces SET runtime_files_json=? WHERE workspace_id=?",
            (json.dumps([".env", ".venv"]), workspace.workspace_id),
        )
    manager.cleanup_closed()
    assert not private.exists()
    assert (outside / "keep").read_text() == "outside user bytes"
    assert not list(private.parent.glob(".recovery-*/files/.venv"))


def test_interrupted_hydration_does_not_archive_copied_private_runtime(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    (batch_env.repository / ".gitignore").write_text(".env\n")
    (batch_env.repository / ".env").write_text("private recovery runtime")
    manager = _manager(batch_env, tmp_path)

    def interrupt(stage: str) -> None:
        if stage == "hydrated":
            raise CreationCrash()

    manager.fault = interrupt
    with pytest.raises(CreationCrash):
        manager.open(
            _checkout(batch_env), "hydrated-secret", approved_secrets=(".env",)
        )
    _manager(batch_env, tmp_path).recover()
    assert not list((tmp_path / "private").glob(".recovery-*/files/.env"))
    assert (batch_env.repository / ".env").read_text() == "private recovery runtime"


def test_recovery_prunes_private_inputs_from_pre_fix_owned_archive(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    manager, workspace, private = _closed_workspace(batch_env, tmp_path)
    manager.cleanup_closed()
    archive = private.parent / (".recovery-" + workspace.workspace_id)
    owner_path = archive / "owner.json"
    owner = json.loads(owner_path.read_text())
    owner.pop("private_paths")
    owner_path.write_text(json.dumps(owner))
    (archive / "files/.env").write_text("old archived private bytes")
    _manager(batch_env, tmp_path).recover()
    assert not (archive / "files/.env").exists()
    assert (archive / "files/a.txt").read_text() == "old a\n"


def test_screened_source_copy_is_not_retained_as_recovery_evidence(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    manager, workspace, private = _closed_workspace(batch_env, tmp_path)
    state = manager.get(workspace.workspace_id)
    manifest = state.source_manifest
    manifest["a.txt"]["screened"] = True
    manifest["a.txt"]["content_hash"] = None
    with batch_env.store.connection() as con:
        con.execute(
            "UPDATE isolated_workspaces SET source_manifest_json=? "
            "WHERE workspace_id=?",
            (json.dumps(manifest), workspace.workspace_id),
        )
    (private / "a.txt").write_text("private screened fixture contents")
    manager.cleanup_closed()
    assert not private.exists()
    archive = private.parent / (".recovery-" + workspace.workspace_id)
    assert not (archive / "files/a.txt").exists()
    assert (batch_env.repository / "a.txt").read_text() == "old a\n"
