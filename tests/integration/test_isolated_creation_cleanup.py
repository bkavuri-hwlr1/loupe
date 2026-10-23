"""Failed creation scrubs owned private copies while retaining source evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_isolated_creation_safety import checkout, manager
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.agent.source_policy import contains_secret_material
from llm_cli.errors import LlmCoordError
from llm_cli.workspace import isolated
from llm_cli.workspace.isolated import IsolatedWorkspaceManager

_PRIVATE = b"LOCAL_VALUE=orphan-creation-private-canary\n"
_SCREENED = b'FAKE_AWS="' + b"AKIA" + b"EXAMPLE000000000" + b'"\n'


class CreationCrash(BaseException):
    pass


def _inputs(env: BatchEnvironment) -> None:
    (env.repository / ".gitignore").write_text(".env\n")
    (env.repository / ".env").write_bytes(_PRIVATE)
    (env.repository / "screened-fixture.py").write_bytes(_SCREENED)
    assert contains_secret_material(_SCREENED)


def _only_workspace(env: BatchEnvironment) -> Any:
    with env.store.connection() as con:
        row = con.execute("SELECT * FROM workspaces WHERE mode='isolated'").fetchone()
        assert row is not None
        return row


def _legacy_attention(
    env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    unknown_source: bool = True,
) -> tuple[IsolatedWorkspaceManager, Path, Any]:
    _inputs(env)
    original = isolated.copy_runtime_file

    def crash_after_copy(primary: Path, private: Path, entry: Any) -> None:
        original(primary, private, entry)
        if entry.path == ".env":
            if unknown_source:
                (private / "user-evidence.txt").write_text(
                    "preserve ordinary user bytes\n"
                )
            raise CreationCrash()

    monkeypatch.setattr(isolated, "copy_runtime_file", crash_after_copy)
    instance = manager(env, tmp_path)
    with pytest.raises(CreationCrash):
        instance.open(checkout(env), "failed-creation", approved_secrets=(".env",))
    row = _only_workspace(env)
    instance._quarantine(row["workspace_id"])
    private = Path(row["canonical_path"])
    assert (private / ".env").read_bytes() == _PRIVATE
    assert (private / "screened-fixture.py").read_bytes() == _SCREENED
    return instance, private, row


@pytest.mark.parametrize("unknown_source", [False, True])
def test_ordinary_creation_error_scrubs_private_runtime_and_screened_source(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unknown_source: bool,
) -> None:
    env = batch_env
    _inputs(env)
    original = isolated.copy_runtime_file

    def save_after_copy(primary: Path, private: Path, entry: Any) -> None:
        original(primary, private, entry)
        if entry.path == ".env":
            if unknown_source:
                (private / "user-evidence.txt").write_text(
                    "preserve ordinary user bytes\n"
                )
            (primary / "a.txt").write_text("outside save during creation\n")

    monkeypatch.setattr(isolated, "copy_runtime_file", save_after_copy)
    instance = manager(env, tmp_path)
    with pytest.raises(
        LlmCoordError, match="source or ignore/attribute/config policy changed"
    ):
        instance.open(checkout(env), "failed-creation", approved_secrets=(".env",))
    row = _only_workspace(env)
    private = Path(row["canonical_path"])
    assert not (private / ".env").exists()
    assert not (private / "screened-fixture.py").exists()
    archive = private.parent / (".recovery-" + row["workspace_id"]) / "files"
    assert not (archive / ".env").exists()
    assert not (archive / "screened-fixture.py").exists()
    assert (env.repository / ".env").read_bytes() == _PRIVATE
    assert (env.repository / "screened-fixture.py").read_bytes() == _SCREENED
    with env.store.connection() as con:
        assert (
            con.execute(
                "SELECT 1 FROM sessions WHERE session_id='failed-creation'"
            ).fetchone()
            is None
        )
    if unknown_source:
        assert (
            private / "user-evidence.txt"
        ).read_text() == "preserve ordinary user bytes\n"
        assert env.store.get_workspace(row["workspace_id"]).state == "quarantined"
    else:
        assert env.store.get_workspace(row["workspace_id"]).state == "removed"
        assert instance.get(row["workspace_id"]) is None
        assert (archive / "a.txt").read_text() == "old a\n"
    assert _PRIVATE not in env.store.path.read_bytes()
    assert _SCREENED not in env.store.path.read_bytes()
    assert all(
        _PRIVATE not in p.read_bytes() and _SCREENED not in p.read_bytes()
        for p in env.publisher.candidate_dir.iterdir()
        if p.is_file()
    )


def test_recovery_scrubs_previously_quarantined_orphan_creation(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, private, row = _legacy_attention(batch_env, tmp_path, monkeypatch)
    runtime = instance.runtime._directory(row["workspace_id"])
    generation = runtime / "generation-0"
    generation.mkdir(mode=0o700)
    (generation / ".env").write_bytes(_PRIVATE)
    instance.recover()
    assert not runtime.exists()
    assert not (private / ".env").exists()
    assert not (private / "screened-fixture.py").exists()
    assert (
        private / "user-evidence.txt"
    ).read_text() == "preserve ordinary user bytes\n"
    assert (batch_env.repository / ".env").read_bytes() == _PRIVATE
    assert instance.get(row["workspace_id"]).stage == "attention"
    instance.recover()
    assert (private / "user-evidence.txt").exists()


@pytest.mark.parametrize(
    "damage", ["token", "git_symlink", "replacement", "archive_owner"]
)
def test_orphan_recovery_never_scrubs_an_unverified_worktree(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    instance, private, row = _legacy_attention(batch_env, tmp_path, monkeypatch)
    original = private
    if damage == "token":
        (Path(row["git_dir"]) / "loupe-isolated-owner").write_text(
            "foreign-owner-token"
        )
    elif damage == "git_symlink":
        (private / ".git").unlink()
        (private / ".git").symlink_to(
            batch_env.repository / ".git", target_is_directory=True
        )
    elif damage == "replacement":
        original = private.with_name(private.name + "-original")
        private.rename(original)
        private.mkdir(mode=0o700)
        (private / ".env").write_text("replacement user runtime")
        (private / "user-evidence.txt").write_text("replacement user evidence")
    else:
        archive = private.parent / (".recovery-" + row["workspace_id"])
        archive.mkdir(mode=0o700)
        (archive / "owner.json").write_text(json.dumps({"token": "not-this-owner"}))
    before = (private / ".env").read_bytes()
    instance.recover()
    assert (private / ".env").read_bytes() == before
    assert (original / "screened-fixture.py").read_bytes() == _SCREENED
    assert (private / "user-evidence.txt").exists()
    assert instance.get(row["workspace_id"]).stage == "attention"


@pytest.mark.parametrize("reference", ["session", "live_execution", "held_proposal"])
def test_attention_runtime_is_retained_for_registered_or_referenced_workspaces(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
) -> None:
    env = batch_env
    instance, private, row = _legacy_attention(env, tmp_path, monkeypatch)
    runtime = instance.runtime._directory(row["workspace_id"])
    generation = runtime / "generation-0"
    generation.mkdir(mode=0o700)
    (generation / ".env").write_bytes(_PRIVATE)
    if reference == "session":
        env.store.open_session(
            session_id="failed-creation",
            checkout_id=row["checkout_id"],
            workspace_id=row["workspace_id"],
            workspace_mode="isolated",
            resume_token_hash="b" * 64,
            provider="scripted",
            model="held",
        )
    else:
        task = env.store.create_task(
            repository_id=checkout(env).repository_id,
            title="retained private user",
        )
        with env.store.connection() as con:
            if reference == "held_proposal":
                con.execute(
                    "INSERT INTO task_workflows(task_id,attempt,proposal_json,"
                    "status,created_at) "
                    "VALUES (?,1,'[]','awaiting_review',1)",
                    (task.task_id,),
                )
                con.execute(
                    "INSERT INTO isolated_runtime_holds VALUES (?,1,?,0)",
                    (task.task_id, row["workspace_id"]),
                )
            else:
                con.execute(
                    "INSERT INTO claims(claim_id,task_id,task_attempt,repo_key,state,"
                    "queue_sequence,fencing_token,lease_expires_at,"
                    "created_at,updated_at) "
                    "VALUES ('live-claim',?,1,?,'active_work',1,1,?,1,1)",
                    (task.task_id, task.repo_key, env.store._now(None) + 86_400_000),
                )
                state = instance.get(row["workspace_id"])
                con.execute(
                    "INSERT INTO task_executions(execution_id,task_id,"
                    "task_attempt,claim_id,"
                    "driver,state,worktree_path,base_oid,workspace_id,"
                    "created_at,updated_at) "
                    "VALUES ('live-execution',?,1,'live-claim','scripted',"
                    "'running',?,?,?,1,1)",
                    (task.task_id, str(private), state.base_oid, row["workspace_id"]),
                )
    instance.recover()
    assert (private / ".env").read_bytes() == _PRIVATE
    assert (private / "screened-fixture.py").read_bytes() == _SCREENED
    assert (private / "user-evidence.txt").exists()
    assert (generation / ".env").read_bytes() == _PRIVATE
    assert instance.get(row["workspace_id"]).stage == "attention"


def test_cleanup_error_does_not_replace_the_original_creation_error(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = batch_env
    _inputs(env)
    original = isolated.copy_runtime_file
    instance = manager(env, tmp_path)

    def save_after_copy(primary: Path, private: Path, entry: Any) -> None:
        original(primary, private, entry)
        if entry.path == ".env":
            (primary / "a.txt").write_text("outside save during creation\n")

    def cleanup_refused(workspace_id: str) -> bool:
        raise OSError("independent cleanup failure")

    monkeypatch.setattr(isolated, "copy_runtime_file", save_after_copy)
    monkeypatch.setattr(instance, "_clean_incomplete_owned", cleanup_refused)
    with pytest.raises(
        LlmCoordError, match="source or ignore/attribute/config policy changed"
    ):
        instance.open(checkout(env), "failed-creation", approved_secrets=(".env",))
    row = _only_workspace(env)
    assert env.store.get_workspace(row["workspace_id"]).state == "quarantined"


def test_actual_service_failed_open_retires_owned_runtime_and_confirms_authority(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import hashlib

    from test_shared_agent_execution import ScriptedProvider, _register, _request

    async def scenario() -> None:
        root = repository_factory(
            tmp_path, {"a.txt": "original\n", ".gitignore": ".env\n"}
        )
        (root / ".env").write_bytes(_PRIVATE)
        (root / "screened-fixture.py").write_bytes(_SCREENED)
        service = service_factory(tmp_path)
        _register(service, ScriptedProvider("failed-open", []))
        service.initialize()
        original = isolated.copy_runtime_file

        def save_after_copy(primary: Path, private: Path, entry: Any) -> None:
            original(primary, private, entry)
            if entry.path == ".env":
                (primary / "a.txt").write_text("outside save during creation\n")

        monkeypatch.setattr(isolated, "copy_runtime_file", save_after_copy)
        try:
            await service.handle(_request("repo.add", {"path": str(root)}))
            with pytest.raises(
                LlmCoordError, match="source or ignore/attribute/config policy changed"
            ):
                await service.handle(
                    _request(
                        "session.open",
                        {
                            "path": str(root),
                            "session_id": "failed-open",
                            "resume_token_hash": hashlib.sha256(
                                b"local-fixture-resume"
                            ).hexdigest(),
                            "provider": "scripted",
                            "model": "failed-open",
                            "workspace": "isolated",
                            "approved_runtime_files": [".env"],
                        },
                    )
                )
            assert service.store.get_session("failed-open") is None
            with service.store.connection() as con:
                workspaces = con.execute(
                    "SELECT * FROM workspaces WHERE mode='isolated'"
                ).fetchall()
            assert len(workspaces) == 1 and workspaces[0]["state"] == "removed"
            private = Path(workspaces[0]["canonical_path"])
            assert not private.exists()
            assert not list(private.parent.glob(".recovery-*/files/.env"))
            assert not list(
                private.parent.glob(".recovery-*/files/screened-fixture.py")
            )
            assert (root / ".env").read_bytes() == _PRIVATE
            assert service.physical_authority.status(root)["state"] == "owned"
            with service.store.connection() as con:
                assert (
                    con.execute(
                        "SELECT state FROM authority_journals "
                        "WHERE kind='isolated_create'"
                    ).fetchone()[0]
                    == "confirmed"
                )
        finally:
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("runtime_owner", ["owned", "foreign"])
def test_initialization_failure_scrubs_only_owned_orphan_runtime_generations(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_owner: str,
) -> None:
    env = batch_env
    _inputs(env)
    instance = manager(env, tmp_path)
    initialize = instance.runtime.initialize
    generations: list[Path] = []

    def fail_after_initialize(workspace_id: str, *args: Any, **kwargs: Any) -> None:
        initialize(workspace_id, *args, **kwargs)
        workspace = env.store.get_workspace(workspace_id)
        private = Path(workspace.canonical_path)
        (private / "user-evidence.txt").write_text("preserve ordinary user bytes\n")
        runtime = instance.runtime._directory(workspace_id)
        generation = runtime / "generation-0"
        generation.mkdir(mode=0o700)
        (generation / ".env").write_bytes(_PRIVATE)
        generations.append(runtime)
        if runtime_owner == "foreign":
            (runtime / "owner").write_text("foreign-owner-token")
        raise ValueError("initialization failed after private runtime copy")

    monkeypatch.setattr(instance.runtime, "initialize", fail_after_initialize)
    with pytest.raises(ValueError, match="initialization failed after private"):
        instance.open(checkout(env), "initialized-orphan", approved_secrets=(".env",))
    row = _only_workspace(env)
    private = Path(row["canonical_path"])
    assert not (private / ".env").exists()
    assert not (private / "screened-fixture.py").exists()
    assert (private / "user-evidence.txt").read_text() == (
        "preserve ordinary user bytes\n"
    )
    assert env.store.get_workspace(row["workspace_id"]).state == "quarantined"
    if runtime_owner == "owned":
        assert not generations[0].exists()
        with env.store.connection() as con:
            assert (
                con.execute(
                    "SELECT 1 FROM isolated_runtime_state WHERE workspace_id=?",
                    (row["workspace_id"],),
                ).fetchone()
                is None
            )
    else:
        assert (generations[0] / "generation-0/.env").read_bytes() == _PRIVATE
    assert (env.repository / ".env").read_bytes() == _PRIVATE


def test_dependency_source_rejection_scrubs_runtime_and_preserves_private_edit(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    from test_dependency_rebuild import prepare

    from llm_cli.execution.sandbox import available_sandbox

    if available_sandbox() is None:
        pytest.skip("real command sandbox required")
    env = batch_env
    _inputs(env)
    instance = prepare(env, tmp_path)

    def change_private(stage: str) -> None:
        if stage == "rebuild_running:.venv":
            row = _only_workspace(env)
            (Path(row["canonical_path"]) / "a.txt").write_text(
                "late private source edit\n"
            )

    instance.fault = change_private
    with pytest.raises(
        LlmCoordError, match="dependency installer changed isolated source"
    ):
        instance.open(checkout(env), "failed-rebuild-open", approved_secrets=(".env",))
    row = _only_workspace(env)
    private = Path(row["canonical_path"])
    assert not (private / ".env").exists()
    assert not (private / "screened-fixture.py").exists()
    assert not (private / ".venv").exists()
    assert (private / "a.txt").read_text() == "late private source edit\n"
    assert (env.repository / "a.txt").read_text() == "old a\n"
    assert env.store.get_workspace(row["workspace_id"]).state == "quarantined"
    with env.store.connection() as con:
        assert (
            con.execute(
                "SELECT 1 FROM isolated_runtime_state WHERE workspace_id=?",
                (row["workspace_id"],),
            ).fetchone()
            is None
        )
    instance.recover()
    assert (private / "a.txt").read_text() == "late private source edit\n"
