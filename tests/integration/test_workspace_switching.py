"""Mode switching preserves durable conversations and private source."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.workspace.isolated import IsolatedWorkspaceManager, capture_source
from llm_cli.workspace.isolated_sync import IsolatedSynchronizer
from llm_cli.workspace.switching import WorkspaceModeSwitcher


def switcher(env: BatchEnvironment, tmp_path: Path) -> WorkspaceModeSwitcher:
    isolated = IsolatedWorkspaceManager(
        env.store,
        tmp_path / "private",
        env.publisher.candidate_dir,
        env.publisher.lock,
        lambda workspace: None,
    )
    sync = IsolatedSynchronizer(
        env.store,
        env.publisher,
        lambda root, protected: capture_source(
            root, env.publisher.candidate_dir, protected_paths=protected
        ),
        validate=isolated.validate,
    )
    return WorkspaceModeSwitcher(env.store, isolated, sync, env.publisher)


def finish(
    manager: WorkspaceModeSwitcher, intent: str, target: str, **kw: object
) -> dict:
    prepared = manager.request("first", intent, target, **kw)
    return manager.confirm("first", intent, prepared["bootstrap_sequence"])


def test_shared_isolated_shared_preserves_conversation_and_fresh_binding(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    env = batch_env
    with env.store.connection() as con:
        con.execute(
            "UPDATE sessions SET conversation_json=? WHERE session_id='first'",
            (json.dumps({"messages": ["remember me"]}),),
        )
    manager = switcher(env, tmp_path)
    first = finish(manager, "switch-one", "isolated")
    private = first["workspace"]["canonical_path"]
    assert private != str(env.repository)
    assert Path(private, "a.txt").read_text() == "old a\n"
    assert first["session"]["workspace_mode"] == "isolated"
    shared = finish(manager, "switch-two", "shared")
    assert shared["session"]["workspace_id"] == env.workspace_id
    assert env.store.session_conversation("first")[2]["messages"] == ["remember me"]
    second = finish(manager, "switch-three", "isolated")
    assert second["workspace"]["canonical_path"] != private
    assert manager.request("first", "switch-one", "isolated")["state"] == "confirmed"
    assert (
        env.store.get_session("first").workspace_id
        == second["workspace"]["workspace_id"]
    )


def test_dirty_source_requires_explicit_disposition(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    manager = switcher(batch_env, tmp_path)
    isolated = finish(manager, "out", "isolated")
    private = Path(isolated["workspace"]["canonical_path"])
    (private / "a.txt").write_text("private edit\n")
    with pytest.raises(ValueError, match="disposition"):
        manager.request("first", "back", "shared")
    assert batch_env.store.get_session("first").workspace_mode == "isolated"
    finish(manager, "retain", "shared", disposition="retain")
    assert (private / "a.txt").read_text() == "private edit\n"
    assert (batch_env.repository / "a.txt").read_text() == "old a\n"


@pytest.mark.parametrize(
    "disposition", ["publish", "hold", "export", "retain", "discard"]
)
def test_every_private_source_disposition_is_durable(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    disposition: str,
) -> None:
    manager = switcher(batch_env, tmp_path)
    private_result = finish(manager, "private", "isolated")
    private = Path(private_result["workspace"]["canonical_path"])
    (private / "a.txt").write_text("private change\n")
    result = finish(manager, "return", "shared", disposition=disposition)
    assert result["state"] == "confirmed"
    assert (batch_env.repository / "a.txt").read_text() == (
        "private change\n" if disposition == "publish" else "old a\n"
    )
    if disposition == "hold":
        with batch_env.store.connection() as con:
            row = con.execute(
                "SELECT * FROM task_workflows WHERE task_id=?",
                (result["proposal_task_id"],),
            ).fetchone()
            binding = con.execute(
                "SELECT * FROM switch_proposal_bindings WHERE task_id=?",
                (result["proposal_task_id"],),
            ).fetchone()
        assert row["status"] == "awaiting_review"
        assert binding["workspace_id"] == private_result["workspace"]["workspace_id"]
    if disposition == "export":
        bundle = json.loads(Path(result["export_artifact_path"]).read_text())
        assert bundle["version"] == 1 and bundle["source"][0]["path"] == "a.txt"
        assert not Path(result["export_path"]).stat().st_mode & 0o077
    # Replaying is observation, even after another switch has changed binding.
    assert (
        manager.request("first", "return", "shared", disposition=disposition) == result
    )
    with batch_env.store.connection() as con:
        assert con.execute(
            "SELECT count(*) FROM workspace_batches WHERE state='published'"
        ).fetchone()[0] == int(disposition == "publish")


@pytest.mark.parametrize(
    "state", ["created", "queued", "preparing", "running", "operator_attention"]
)
def test_active_or_ambiguous_task_refuses_before_allocating_intent(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    state: str,
) -> None:
    manager = switcher(batch_env, tmp_path)
    checkout = batch_env.store.get_checkout(
        batch_env.store.get_session("first").checkout_id
    )
    task = batch_env.store.create_task(
        repository_id=checkout.repository_id, session_id="first"
    )
    with batch_env.store.connection() as con:
        con.execute("UPDATE tasks SET state=? WHERE task_id=?", (state, task.task_id))
    with pytest.raises(ValueError, match="active task"):
        manager.request("first", "busy", "isolated")
    assert manager.get("busy") is None


def test_pending_switch_fences_task_creation_retry_and_wrong_bootstrap(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    import sqlite3

    manager = switcher(batch_env, tmp_path)
    checkout = batch_env.store.get_checkout(
        batch_env.store.get_session("first").checkout_id
    )
    task = batch_env.store.create_task(
        repository_id=checkout.repository_id, session_id="first"
    )
    with batch_env.store.connection() as con:
        con.execute(
            "UPDATE tasks SET state='completed' WHERE task_id=?", (task.task_id,)
        )
    result = manager.request("first", "pending", "isolated")
    assert batch_env.store.get_session("first").workspace_mode == "shared"
    with pytest.raises(sqlite3.IntegrityError, match="switch requires"):
        batch_env.store.create_task(
            repository_id=checkout.repository_id, session_id="first"
        )
    with pytest.raises(sqlite3.IntegrityError, match="switch requires"):
        batch_env.store.begin_new_attempt(task.task_id)
    with pytest.raises(ValueError, match="exact target"):
        manager.confirm("first", "pending", result["bootstrap_sequence"] + 1)
    assert (
        manager.confirm("first", "pending", result["bootstrap_sequence"])["state"]
        == "confirmed"
    )


def test_unresolved_conflict_blocks_every_disposition(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    manager = switcher(batch_env, tmp_path)
    private = Path(
        finish(manager, "private", "isolated")["workspace"]["canonical_path"]
    )
    (private / "a.txt").write_text(
        "<<<<<<< your pending edits\nlocal\n=======\nincoming\n>>>>>>> "
        "current checkout\n"
    )
    for disposition in ("publish", "hold", "export", "retain", "discard"):
        with pytest.raises(ValueError, match="conflict"):
            manager.request(
                "first", "conflict-" + disposition, "shared", disposition=disposition
            )
    assert batch_env.store.get_session("first").workspace_mode == "isolated"


def test_clean_switch_close_scrubs_private_runtime_and_retires_worktree(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    env = batch_env
    (env.repository / ".gitignore").write_text(".env\n")
    (env.repository / ".env").write_text("PRIVATE_TOKEN=do-not-retain\n")
    manager = switcher(env, tmp_path)
    result = finish(manager, "private", "isolated", approved_secrets=(".env",))
    private = Path(result["workspace"]["canonical_path"])
    assert (private / ".env").exists()
    finish(manager, "shared", "shared")
    env.store.close_session("first", reason="test")
    manager.isolated.cleanup_closed("first")
    assert not private.exists()
    assert not any(path.name == ".env" for path in (tmp_path / "private").rglob("*"))
    assert (env.repository / ".env").exists()


def test_scope_change_requires_attention_and_rollback_preserves_source(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    manager = switcher(batch_env, tmp_path)
    result = manager.request("first", "pending", "isolated")
    (batch_env.repository / "a.txt").write_text("late user edit\n")
    with pytest.raises(ValueError, match="scope changed"):
        manager.confirm("first", "pending", result["bootstrap_sequence"])
    assert manager.get("pending")["state"] == "attention_required"
    rolled_back = manager.rollback("first", "pending")
    assert rolled_back["state"] == "rolled_back"
    assert batch_env.store.get_session("first").workspace_mode == "shared"
    assert (batch_env.repository / "a.txt").read_text() == "late user edit\n"


@pytest.mark.parametrize(
    "phase",
    [
        "requested",
        "prepared",
        "target_creating",
        "transferring",
        "confirming",
        "confirmed",
    ],
)
def test_real_process_death_at_every_switch_phase_is_idempotent(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    phase: str,
) -> None:
    import subprocess
    import sys

    env = batch_env
    script = """
import os, sys, threading
from pathlib import Path
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.batches import SharedBatchPublisher
from llm_cli.workspace.isolated import IsolatedWorkspaceManager, capture_source
from llm_cli.workspace.isolated_sync import IsolatedSynchronizer
from llm_cli.workspace.switching import WorkspaceModeSwitcher
store=ControlStore(Path(sys.argv[1]))
publisher=SharedBatchPublisher(store,Path(sys.argv[2]),threading.RLock())
isolated=IsolatedWorkspaceManager(
    store,Path(sys.argv[3]),publisher.candidate_dir,publisher.lock,lambda workspace:None
)
sync=IsolatedSynchronizer(
    store,publisher,
    lambda root,protected:capture_source(
        root,publisher.candidate_dir,protected_paths=protected
    ),validate=isolated.validate
)
manager=WorkspaceModeSwitcher(store,isolated,sync,publisher)
manager.fault=lambda state: os._exit(73) if state==sys.argv[4] else None
result=manager.request('first','crash','isolated')
manager.confirm('first','crash',result['bootstrap_sequence'])
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(env.store.path),
            str(env.publisher.candidate_dir),
            str(tmp_path / "private"),
            phase,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 73, completed.stderr
    manager = switcher(env, tmp_path)
    manager.recover()
    result = manager.request("first", "crash", "isolated")
    if result["state"] == "confirming":
        result = manager.confirm("first", "crash", result["bootstrap_sequence"])
    assert result["state"] == "confirmed"
    with env.store.connection() as con:
        assert (
            con.execute("SELECT count(*) FROM isolated_workspaces").fetchone()[0] == 1
        )
        assert (
            con.execute(
                "SELECT count(*) FROM checkout_events WHERE "
                "event_type='session.workspace_switched'"
            ).fetchone()[0]
            == 1
        )


def test_real_daemon_switch_next_task_preserves_model_history_and_workspace(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
) -> None:
    import asyncio

    from test_shared_agent_execution import (
        ScriptedProvider,
        _call,
        _finish,
        _open_session,
        _register,
        _request,
        _tools,
    )

    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.txt": "before\n"})
        provider = ScriptedProvider(
            "switch-context",
            [
                _tools(_call("read_file", path="a.txt")),
                _finish("remembered history"),
                _tools(_call("read_file", path="a.txt")),
                _tools(_call("write_file", path="a.txt", content="isolated edit\n")),
                _finish("private turn"),
                _tools(_call("read_file", path="a.txt")),
                _tools(_call("write_file", path="a.txt", content="shared followup\n")),
                _finish("shared turn"),
            ],
        )
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(root)}))
            credentials = await _open_session(service, root, provider)

            async def task(task_id: str, generation: int) -> None:
                await service.handle(
                    _request(
                        "task.run",
                        {
                            **credentials,
                            "title": "remember the prior conversation",
                            "path": str(root),
                            "scopes": ["*"],
                            "task_id": task_id,
                            "workspace_binding_generation": generation,
                        },
                    )
                )
                background = service._background_tasks.get((task_id, 1))
                assert background is not None
                await asyncio.wait_for(background, 15)
                assert service.store.get_task(task_id).state in {
                    "completed",
                    "reviewing",
                }

            await task("before-switch", 1)
            original_history = list(provider.history)
            assert (
                service.store.session_conversation(credentials["session_id"])[2][
                    "coordination_sequence"
                ]
                is not None
            )
            await service.handle(
                _request(
                    "workspace.read_file",
                    {
                        **credentials,
                        "relative_path": "a.txt",
                    },
                )
            )
            prepared = await service.handle(
                _request(
                    "workspace.switch_mode",
                    {
                        **credentials,
                        "switch_intent_id": "rpc-switch",
                        "workspace": "isolated",
                    },
                )
            )
            assert prepared["state"] == "confirming"
            target_path = Path(prepared["workspace"]["canonical_path"])
            result = await service.handle(
                _request(
                    "workspace.confirm_switch",
                    {
                        **credentials,
                        "switch_intent_id": "rpc-switch",
                        "sequence": prepared["bootstrap_sequence"],
                    },
                )
            )
            assert result["session"]["workspace_binding_generation"] == 2
            assert (
                service.store.session_conversation(credentials["session_id"])[2][
                    "coordination_sequence"
                ]
                is None
            )
            with service.store.connection() as con:
                assert (
                    con.execute(
                        "SELECT count(*) FROM workspace_read_observations "
                        "WHERE session_id=?",
                        (credentials["session_id"],),
                    ).fetchone()[0]
                    == 0
                )
            # Old terminal credentials cannot submit work against their old binding.
            from llm_cli.errors import LlmCoordError

            with pytest.raises(LlmCoordError, match="binding changed"):
                await service.handle(
                    _request(
                        "task.run",
                        {
                            **credentials,
                            "title": "stale",
                            "path": str(root),
                            "task_id": "stale",
                            "scopes": ["*"],
                        },
                    )
                )
            service.store.set_session_mode(credentials["session_id"], "normal")
            (target_path / "a.txt").write_text("private baseline\n")
            await task("after-switch", 2)
            assert provider.history[: len(original_history)] == original_history
            execution = service.store.get_execution("after-switch", 1)
            assert execution.workspace_id == result["workspace"]["workspace_id"]
            assert execution.worktree_path == str(target_path)
            assert provider.results[1][0].content == "private baseline\n"
            isolated_history = list(provider.history)
            opening = str(provider.history[len(original_history)]["content"])
            assert (
                '"bootstrap"' in opening
                and result["workspace"]["workspace_id"] in opening
            )
            assert (root / "a.txt").read_text() == "before\n"
            assert service.workflow.get("after-switch")["status"] == "awaiting_review"
            prepared = await service.handle(
                _request(
                    "workspace.switch_mode",
                    {
                        **credentials,
                        "switch_intent_id": "back-shared",
                        "workspace": "shared",
                        "disposition": "hold",
                    },
                )
            )
            await service.handle(
                _request(
                    "workspace.confirm_switch",
                    {
                        **credentials,
                        "switch_intent_id": "back-shared",
                        "sequence": prepared["bootstrap_sequence"],
                    },
                )
            )
            retained_session = service.workflow._task_session(
                service.store.get_session(credentials["session_id"]),
                "after-switch",
                1,
            )
            assert retained_session.workspace_id == execution.workspace_id
            service.workflow.apply("after-switch", allow_unverified=True)
            assert (root / "a.txt").read_text() == "isolated edit\n"
            assert (
                service.store.session_conversation(credentials["session_id"])[2][
                    "coordination_sequence"
                ]
                is None
            )
            service.store.set_session_mode(credentials["session_id"], "auto")
            await task("after-return", 3)
            assert provider.history[: len(isolated_history)] == isolated_history
            opening = str(provider.history[len(isolated_history)]["content"])
            shared_workspace = service.store.get_session(
                credentials["session_id"]
            ).workspace_id
            assert '"bootstrap"' in opening and shared_workspace in opening
            assert (root / "a.txt").read_text() == "shared followup\n"
            assert (
                service.store.get_execution("after-return", 1).workspace_id
                == shared_workspace
            )
        finally:
            await service.drain()
            service.close()

    asyncio.run(scenario())


def test_runtime_activity_and_actual_worker_lifetime_refuse_switch(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    manager = switcher(batch_env, tmp_path)
    result = finish(manager, "private", "isolated")
    workspace_id = result["workspace"]["workspace_id"]
    with (
        manager.isolated.runtime.activity(workspace_id),
        pytest.raises(ValueError, match="active workspace"),
    ):
        manager.request("first", "busy-export", "shared")
    manager.worker_busy = lambda owner: owner == "first"
    with pytest.raises(ValueError, match="active task"):
        manager.request("first", "worker", "shared")


@pytest.mark.parametrize("passes", [False, True])
def test_publish_runs_required_check_against_exact_private_candidate(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    passes: bool,
) -> None:
    import sys

    from llm_cli.execution.checks import validate_config

    manager = switcher(batch_env, tmp_path)
    (batch_env.repository / ".gitignore").write_text(".env\n")
    (batch_env.repository / ".env").write_text("private-runtime-fixture")
    private = Path(
        finish(manager, "private", "isolated", approved_secrets=(".env",))["workspace"][
            "canonical_path"
        ]
    )
    (private / "a.txt").write_text("checked private source\n")
    check = (
        "from pathlib import Path; "
        "assert Path('a.txt').read_text() == 'checked private source\\n'; "
        "assert Path('.env').read_text() == 'private-runtime-fixture'; "
        f"raise SystemExit({0 if passes else 1})"
    )
    checkout_id = batch_env.store.get_session("first").checkout_id
    with batch_env.store.connection() as con:
        con.execute(
            "INSERT INTO check_configurations VALUES (?,?)",
            (
                checkout_id,
                json.dumps(
                    validate_config(
                        {"checks": {"test": {"argv": [sys.executable, "-c", check]}}}
                    )
                ),
            ),
        )
    if passes:
        result = finish(manager, "publish", "shared", disposition="publish")
        assert result["state"] == "confirmed"
        assert (
            batch_env.repository / "a.txt"
        ).read_text() == "checked private source\n"
    else:
        with pytest.raises(ValueError, match=r"check|Check|test"):
            manager.request("first", "publish", "shared", disposition="publish")
        assert manager.get("publish")["state"] == "attention_required"
        assert (batch_env.repository / "a.txt").read_text() == "old a\n"
        assert (private / "a.txt").read_text() == "checked private source\n"


def test_private_source_change_during_checks_refuses_publication(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.execution import checks

    manager = switcher(batch_env, tmp_path)
    private = Path(
        finish(manager, "private", "isolated")["workspace"]["canonical_path"]
    )
    (private / "a.txt").write_text("frozen candidate\n")
    real_check = checks.check_retained_proposal

    def edited(*args: Any, **kwargs: Any) -> str | None:
        result = real_check(*args, **kwargs)
        (private / "a.txt").write_text("late private edit\n")
        return result

    monkeypatch.setattr(checks, "check_retained_proposal", edited)
    with pytest.raises(ValueError, match="changed during switch checks"):
        manager.request("first", "publish", "shared", disposition="publish")
    assert (batch_env.repository / "a.txt").read_text() == "old a\n"
    assert (private / "a.txt").read_text() == "late private edit\n"


def test_runtime_only_changes_require_explicit_disposition(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    (batch_env.repository / ".gitignore").write_text(".env\n")
    (batch_env.repository / ".env").write_text("private-runtime-fixture")
    manager = switcher(batch_env, tmp_path)
    private = Path(
        finish(manager, "private", "isolated", approved_secrets=(".env",))["workspace"][
            "canonical_path"
        ]
    )
    (private / ".env").write_text("changed-private-runtime")
    with pytest.raises(
        ValueError, match=r"runtime inventory changed.*explicit retain or discard"
    ):
        manager.request("first", "back", "shared")
    assert manager.get("back") is None
    result = finish(manager, "discard", "shared", disposition="discard")
    assert result["classification"] == "runtime_only"
    assert "changed-private-runtime" not in json.dumps(result)


def test_exported_git_patch_roundtrips_text_binary_symlink_and_mode(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Any,
) -> None:
    import os

    manager = switcher(batch_env, tmp_path)
    private = Path(
        finish(manager, "private", "isolated")["workspace"]["canonical_path"]
    )
    (private / "a.txt").write_text("exported source\n")
    (private / "a.txt").chmod(0o755)
    (private / "asset.bin").write_bytes(b"\0\xffbinary export\x80")
    (private / "source-link").symlink_to("a.txt")
    result = finish(manager, "export", "shared", disposition="export")
    patch = Path(result["export_path"])
    assert b"GIT binary patch" in patch.read_bytes()
    assert Path(result["export_artifact_path"]).is_file()
    git_run(batch_env.repository, "apply", "--check", str(patch))
    git_run(batch_env.repository, "apply", str(patch))
    assert (batch_env.repository / "a.txt").read_text() == "exported source\n"
    assert (batch_env.repository / "a.txt").stat().st_mode & 0o111
    assert (
        batch_env.repository / "asset.bin"
    ).read_bytes() == b"\0\xffbinary export\x80"
    assert os.readlink(batch_env.repository / "source-link") == "a.txt"


def test_held_apply_after_switch_uses_original_runtime_and_session_path_environment(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import hashlib
    import sys

    from test_command_environment_sessions import _session_service
    from test_shared_agent_execution import (
        ScriptedProvider,
        _call,
        _finish,
        _register,
        _request,
        _start,
        _tools,
    )

    monkeypatch.setenv("PROJECT_MODE", "wrong-daemon-terminal")

    async def scenario() -> None:
        root = repository_factory(
            tmp_path, {"a.txt": "base\n", ".gitignore": ".env\nruntime/\n"}
        )
        (root / ".env").write_text("original-runtime-input")
        (root / "runtime").mkdir()
        (root / "runtime/data").write_text("original-dependency-input")
        provider = ScriptedProvider(
            "held-switch-runtime",
            [
                _tools(
                    _call("read_file", path="a.txt"),
                    _call("write_file", path="a.txt", content="candidate\n"),
                ),
                _finish(),
            ],
        )
        service = _session_service(service_factory(tmp_path))
        _register(service, provider)
        service.initialize()
        credentials = {
            "session_id": provider.model,
            "resume_secret": "switch-held-secret",
        }
        environment = {
            "PROJECT_INPUT": str(root / ".env"),
            "PROJECT_DEP": str(root / "runtime/data"),
            "PROJECT_MODE": "origin-terminal",
        }
        try:
            await service.handle(_request("repo.add", {"path": str(root)}))
            opened = await service.handle(
                _request(
                    "session.open",
                    {
                        "session_id": provider.model,
                        "path": str(root),
                        "resume_token_hash": hashlib.sha256(
                            credentials["resume_secret"].encode()
                        ).hexdigest(),
                        "provider": "scripted",
                        "model": provider.model,
                        "workspace": "isolated",
                        "mode": "normal",
                        "approved_runtime_files": [".env"],
                        "command_environment": environment,
                    },
                )
            )
            await service.handle(
                _request(
                    "session.ack",
                    {**credentials, "sequence": opened["bootstrap_sequence"]},
                )
            )
            await service.handle(
                _request(
                    "checks.configure",
                    {
                        "path": str(root),
                        "config": {
                            "checks": {
                                "exact-runtime": {
                                    "argv": [
                                        sys.executable,
                                        "-c",
                                        "import os; from pathlib import Path; "
                                        "assert Path('a.txt').read_text()=="
                                        "'candidate\\n'; "
                                        "assert Path(os.environ['PROJECT_INPUT'])"
                                        ".read_text()=="
                                        "'original-runtime-input'; "
                                        "assert Path(os.environ['PROJECT_DEP'])"
                                        ".read_text()=="
                                        "'original-dependency-input'; "
                                        "assert os.environ['PROJECT_MODE']=="
                                        "'origin-terminal'; "
                                        "print('verified original runtime')",
                                    ]
                                }
                            },
                        },
                    },
                )
            )
            await asyncio.wait_for(
                await _start(service, root, credentials, "held-before-switch", ("*",)),
                20,
            )
            assert (
                service.workflow.get("held-before-switch")["status"]
                == "awaiting_review"
            )
            old_workspace = opened["workspace"]["workspace_id"]
            private = Path(opened["workspace"]["canonical_path"])
            prepared = await service.handle(
                _request(
                    "workspace.switch_mode",
                    {
                        **credentials,
                        "workspace": "shared",
                        "switch_intent_id": "held-back",
                        "disposition": "hold",
                    },
                )
            )
            await service.handle(
                _request(
                    "workspace.confirm_switch",
                    {
                        **credentials,
                        "switch_intent_id": "held-back",
                        "sequence": prepared["bootstrap_sequence"],
                        "command_environment": environment,
                    },
                )
            )
            (root / ".env").write_text("later-shared-input")
            (root / "runtime/data").write_text("later-shared-dependency")
            (root / "outside.txt").write_text("force fresh source/check evidence\n")
            applied = await service.handle(
                _request("task.apply", {"task_id": "held-before-switch"})
            )
            assert applied["state"] == "published"
            assert (root / "a.txt").read_text() == "candidate\n"
            assert (private / ".env").read_text() == "original-runtime-input"
            with service.store.connection() as con:
                hold = con.execute(
                    "SELECT workspace_id FROM isolated_runtime_holds "
                    "WHERE task_id='held-before-switch'"
                ).fetchone()
                checks = con.execute(
                    "SELECT state,output FROM check_runs "
                    "WHERE task_id='held-before-switch' ORDER BY created_at"
                ).fetchall()
            assert hold[0] == old_workspace
            assert len(checks) >= 2 and checks[-1]["state"] == "passed"
            assert "verified original runtime" in checks[-1]["output"]
            row = service.workflow.get("held-before-switch")
            action = service.store.get_task(applied["task_id"])
            execution = service.store.get_execution(action.task_id, action.attempt)
            assert row["publication_id"] == execution.execution_id
            undo = await service.handle(
                _request("task.undo", {"task_id": "held-before-switch"})
            )
            assert undo["state"] == "published"
            assert (root / "a.txt").read_text() == "base\n"
        finally:
            await service.drain()
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", ["rollback", "close"])
def test_slow_switch_checks_cannot_publish_after_cancel_or_owner_close(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: str,
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from llm_cli.execution import checks

    manager = switcher(batch_env, tmp_path)
    private = Path(
        finish(manager, "private", "isolated")["workspace"]["canonical_path"]
    )
    (private / "a.txt").write_text("pending publication\n")
    entered, release = threading.Event(), threading.Event()
    actual = checks.check_retained_proposal

    def waiting(*args: Any, **kwargs: Any) -> str | None:
        entered.set()
        assert release.wait(10)
        return actual(*args, **kwargs)

    monkeypatch.setattr(checks, "check_retained_proposal", waiting)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(
            manager.request, "first", "cancelled", "shared", disposition="publish"
        )
        try:
            assert entered.wait(10)
            # The checker is outside the publication barrier.
            assert batch_env.publisher.lock.acquire(timeout=2)
            batch_env.publisher.lock.release()
            if cancel == "rollback":
                pending = manager.rollback("first", "cancelled")
                assert pending["state"] == "rollback_pending"
            else:
                batch_env.store.close_session("first", reason="test cancellation")
        finally:
            release.set()
        if cancel == "rollback":
            assert worker.result(timeout=10)["state"] == "rolled_back"
        else:
            with pytest.raises(ValueError, match="closed or disconnected"):
                worker.result(timeout=10)
    assert manager.get("cancelled")["state"] == (
        "rolled_back" if cancel == "rollback" else "attention_required"
    )
    assert (batch_env.repository / "a.txt").read_text() == "old a\n"
    assert (private / "a.txt").read_text() == "pending publication\n"
    with batch_env.store.connection() as con:
        assert (
            con.execute(
                "SELECT count(*) FROM workspace_batches WHERE state='published'"
            ).fetchone()[0]
            == 0
        )


def test_disconnected_publish_recovery_waits_for_owner_before_creating_proposal(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    manager = switcher(batch_env, tmp_path)
    private = Path(
        finish(manager, "private", "isolated")["workspace"]["canonical_path"]
    )
    (private / "a.txt").write_text("private publish\n")

    class Crash(BaseException):
        pass

    manager.fault = lambda state: (
        (_ for _ in ()).throw(Crash()) if state == "requested" else None
    )
    with pytest.raises(Crash):
        manager.request("first", "reconnect", "shared", disposition="publish")
    batch_env.store.disconnect_active_sessions()
    manager = switcher(batch_env, tmp_path)
    manager.recover()
    assert manager.get("reconnect")["state"] == "requested"
    assert manager.get("reconnect")["proposal_task_id"] is None
    batch_env.store.resume_session("first")
    result = finish(manager, "reconnect", "shared", disposition="publish")
    assert result["state"] == "confirmed"
    assert (batch_env.repository / "a.txt").read_text() == "private publish\n"


def test_export_partial_write_crash_cannot_poison_final_artifact_names(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Any,
) -> None:
    import multiprocessing
    import os

    manager = switcher(batch_env, tmp_path)
    private = Path(
        finish(manager, "private", "isolated")["workspace"]["canonical_path"]
    )
    (private / "a.txt").write_text("recoverable export\n")

    def child() -> None:
        isolated = switcher(batch_env, tmp_path)
        isolated.fault = lambda phase: (
            os._exit(73) if phase == "export_partial" else None
        )
        isolated.request("first", "export-crash", "shared", disposition="export")
        os._exit(99)

    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(20)
    assert process.exitcode == 73
    exports = batch_env.publisher.candidate_dir / "switch-exports"
    assert not list(exports.glob("*.patch"))
    assert not list(exports.glob("*.patch.json"))
    manager = switcher(batch_env, tmp_path)
    manager.recover()
    result = finish(manager, "export-crash", "shared", disposition="export")
    assert len(list(exports.glob("*.patch"))) == 1
    assert len(list(exports.glob("*.patch.json"))) == 1
    git_run(batch_env.repository, "apply", "--check", result["export_path"])
    git_run(batch_env.repository, "apply", result["export_path"])
    assert (batch_env.repository / "a.txt").read_text() == "recoverable export\n"


def test_export_preserves_an_unknown_existing_artifact(tmp_path: Path) -> None:
    from llm_cli.workspace.patches import write_export_artifact

    root = tmp_path / "exports"
    root.mkdir(mode=0o700)
    name = "0" * 64 + ".patch"
    target = root / name
    target.write_bytes(b"operator evidence")
    target.chmod(0o600)
    with pytest.raises(ValueError, match="identity changed"):
        write_export_artifact(root, name, b"different candidate", lambda phase: None)
    assert target.read_bytes() == b"operator evidence"


def test_later_workspace_never_sweeps_old_held_source_into_its_publication(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    manager = switcher(batch_env, tmp_path)
    first_open = finish(manager, "first-private", "isolated")
    first = Path(first_open["workspace"]["canonical_path"])
    (first / "a.txt").write_text("first workspace held source\n")
    held = finish(manager, "hold-first", "shared", disposition="hold")
    second = Path(
        finish(manager, "second-private", "isolated")["workspace"]["canonical_path"]
    )
    (second / "docs/b.txt").write_text("second workspace source\n")
    finish(manager, "publish-second", "shared", disposition="publish")
    assert (batch_env.repository / "a.txt").read_text() == "old a\n"
    assert (
        batch_env.repository / "docs/b.txt"
    ).read_text() == "second workspace source\n"
    proposal = manager.workflow.get(held["proposal_task_id"])
    assert (
        proposal["status"] == "awaiting_review" and proposal["publication_id"] is None
    )
    owner = manager.workflow._task_session(
        batch_env.store.get_session("first"), held["proposal_task_id"], 1
    )
    assert owner.workspace_id == first_open["workspace"]["workspace_id"]
    assert owner.workspace_id != batch_env.store.get_session("first").workspace_id


@pytest.mark.parametrize(
    "phase", ["rollback_pending", "rolled_back", "attention_required"]
)
def test_real_process_death_in_rollback_and_attention_preserves_source(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    phase: str,
) -> None:
    import multiprocessing
    import os

    def child() -> None:
        manager = switcher(batch_env, tmp_path)
        manager.fault = lambda state: os._exit(73) if state == phase else None
        result = manager.request("first", "recovery", "isolated")
        if phase == "attention_required":
            (batch_env.repository / "a.txt").write_text("later source\n")
            manager.confirm("first", "recovery", result["bootstrap_sequence"])
        else:
            manager.rollback("first", "recovery")
        os._exit(99)

    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(20)
    assert process.exitcode == 73
    manager = switcher(batch_env, tmp_path)
    manager.recover()
    if phase == "attention_required":
        assert manager.get("recovery")["state"] == "attention_required"
        manager.rollback("first", "recovery")
    assert manager.get("recovery")["state"] == "rolled_back"
    assert batch_env.store.get_session("first").workspace_mode == "shared"
    expected = "later source\n" if phase == "attention_required" else "old a\n"
    assert (batch_env.repository / "a.txt").read_text() == expected
    with batch_env.store.connection() as con:
        assert (
            con.execute("SELECT count(*) FROM isolated_workspaces").fetchone()[0] == 1
        )


@pytest.mark.parametrize(
    "physical,tampered", [(False, False), (True, False), (True, True)]
)
def test_failed_switch_target_scrubs_runtime_but_preserves_unknown_source(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    physical: bool,
    tampered: bool,
) -> None:
    from contextlib import ExitStack

    from llm_cli.errors import LlmCoordError
    from llm_cli.workspace.authority import PhysicalAuthority

    with ExitStack() as resources:
        (batch_env.repository / ".gitignore").write_text(".env\n")
        (batch_env.repository / ".env").write_text("copied-private-runtime")
        authority = None
        if physical:
            authority = PhysicalAuthority(
                batch_env.store,
                "test",
                "target-failure-boot",
                tmp_path / "socket",
                root=tmp_path / "physical-authority" / "v1",
            )
            batch_env.store.authority = authority
            resources.callback(authority.close)
        manager = switcher(batch_env, tmp_path)

        def fail(workspace_id: str, *args: Any, **kwargs: Any) -> None:
            private = Path(batch_env.store.get_workspace(workspace_id).canonical_path)
            assert (private / ".env").read_text() == "copied-private-runtime"
            (private / "unknown-source.txt").write_text("preserve this source\n")
            if tampered:
                state = batch_env.store.get_workspace(workspace_id)
                Path(state.git_dir, "loupe-isolated-owner").write_text("foreign-owner")
            raise RuntimeError("fixture runtime initialization failed")

        monkeypatch.setattr(manager.isolated.runtime, "initialize", fail)
        with pytest.raises(RuntimeError, match="initialization failed"):
            manager.request(
                "first", "failed-target", "isolated", approved_secrets=(".env",)
            )
        row = manager.get("failed-target")
        assert row["target_workspace_id"] is not None
        private = Path(
            batch_env.store.get_workspace(row["target_workspace_id"]).canonical_path
        )
        assert (private / ".env").exists() == tampered
        assert (private / "unknown-source.txt").read_text() == "preserve this source\n"
        manager.rollback("first", "failed-target")
        batch_env.store.close_session("first", reason="test")
        manager.isolated.cleanup_closed("first")
        manager.isolated.recover()
        assert (private / ".env").exists() == tampered
        assert (private / "unknown-source.txt").read_text() == "preserve this source\n"

        if authority is not None:
            if tampered:
                with pytest.raises(LlmCoordError), authority.pin(batch_env.repository):
                    pass
            else:
                with authority.pin(batch_env.repository):
                    pass


@pytest.mark.parametrize(
    "phase", ["planned", "registered", "materialized", "hydrated", "ready"]
)
def test_inner_target_creation_crash_never_allocates_a_second_target(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    phase: str,
) -> None:
    manager = switcher(batch_env, tmp_path)

    class Crash(BaseException):
        pass

    def fault(stage: str) -> None:
        if stage == phase:
            raise Crash()

    manager.isolated.fault = fault
    with pytest.raises(Crash):
        manager.request("first", "inner-crash", "isolated")
    row = manager.get("inner-crash")
    target_id = row["target_workspace_id"]
    assert target_id is not None
    manager = switcher(batch_env, tmp_path)
    manager.isolated.recover()
    manager.recover()
    result = manager.request("first", "inner-crash", "isolated")
    if result["state"] == "confirming":
        manager.confirm("first", "inner-crash", result["bootstrap_sequence"])
    else:
        assert result["state"] == "attention_required"
        manager.rollback("first", "inner-crash")
    assert manager.get("inner-crash")["target_workspace_id"] == target_id
    with batch_env.store.connection() as con:
        assert (
            con.execute(
                "SELECT count(*) FROM workspaces WHERE mode='isolated'"
            ).fetchone()[0]
            == 1
        )


def test_cancelled_switch_waiter_keeps_physical_pin_until_worker_returns(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    from test_shared_agent_execution import (
        ScriptedProvider,
        _open_session,
        _register,
        _request,
    )

    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.txt": "base\n"})
        service = service_factory(tmp_path)
        provider = ScriptedProvider("switch-pin-lifetime", [])
        _register(service, provider)
        service.initialize()
        entered, release, finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def waiting(*args: Any, **kwargs: Any) -> dict[str, Any]:
            entered.set()
            try:
                assert release.wait(10)
                return {}
            finally:
                finished.set()

        monkeypatch.setattr(service.workspace_switches, "request", waiting)
        try:
            await service.handle(_request("repo.add", {"path": str(root)}))
            credentials = await _open_session(service, root, provider)
            operation = asyncio.create_task(
                service.handle(
                    _request(
                        "workspace.switch_mode",
                        {
                            **credentials,
                            "switch_intent_id": "cancelled-waiter",
                            "workspace": "isolated",
                        },
                    )
                )
            )
            assert await asyncio.to_thread(entered.wait, 5)
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
            service.physical_authority.release_idle(force=True)
            assert service.physical_authority.status(root)["state"] == "owned"
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 10)
            for _ in range(100):
                service.physical_authority.release_idle(force=True)
                if service.physical_authority.status(root)["state"] != "owned":
                    break
                await asyncio.sleep(0.01)
            service.close()

    asyncio.run(scenario())
