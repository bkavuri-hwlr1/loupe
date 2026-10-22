"""G3 cutover uses durable checkpoints and never silently activates legacy work."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from test_physical_authority import _marker, _owner

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.workspace import regime as regime_module
from llm_cli.workspace.authority import Mutation, PhysicalAuthority
from llm_cli.workspace.installation import private_read
from llm_cli.workspace.regime import (
    CutoverManager,
    assert_legacy_claim,
    legacy_activation_allowed,
)


@pytest.fixture
def repository(
    tmp_path: Path, repository_factory: Callable[[Path, dict[str, str]], Path]
) -> Path:
    return repository_factory(tmp_path, {"hello.py": "print('hello')\n"})


def test_preview_is_observational_and_conversion_is_explicit(
    tmp_path: Path, repository: Path
) -> None:
    authority = _owner(tmp_path, repository, "a")
    manager = CutoverManager(authority.store, authority)
    plan = manager.preview(repository)
    assert plan["ready"] and plan["generation"] == 0
    assert not _marker(authority, repository).exists()
    assert manager.preview(repository)["checkpoint"] == plan["checkpoint"]
    result = manager.begin(repository, plan["checkpoint"])
    assert result["regime"] == "workspace" and result["generation"] == 2
    marker = private_read(_marker(authority, repository))
    assert marker is not None and marker["version"] == 2 and marker["state"] == "clean"
    with authority.store.connection() as connection:
        assert not legacy_activation_allowed(
            connection, plan["mappings"][0]["repo_key"]
        )
        with pytest.raises(LlmCoordError) as error:
            assert_legacy_claim(connection, plan["mappings"][0]["repo_key"])
        assert error.value.code == ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED
    authority.close()


@pytest.mark.parametrize(
    "boundary",
    [
        "intent_saved",
        "transition_marked",
        "checkpoint_saved",
        "workspace_marked",
        "intent_completed",
    ],
)
def test_each_cutover_phase_resumes_without_downgrade(
    tmp_path: Path, repository: Path, boundary: str
) -> None:
    authority = _owner(tmp_path, repository, "a")

    def crash(stage: str) -> None:
        if stage == boundary:
            raise RuntimeError("crash")

    manager = CutoverManager(authority.store, authority, fault=crash)
    plan = manager.preview(repository)
    with pytest.raises(RuntimeError, match="crash"):
        manager.begin(repository, plan["checkpoint"], intent_id="cutover_test")
    with authority.store.connection() as connection:
        assert not legacy_activation_allowed(
            connection, plan["mappings"][0]["repo_key"]
        )
    authority.close()
    recovered = _owner(tmp_path, repository, "a")
    result = CutoverManager(recovered.store, recovered).resume(
        repository, "cutover_test", checkpoint=plan["checkpoint"]
    )
    assert result["regime"] == "workspace" and result["generation"] == 2
    assert (
        CutoverManager(recovered.store, recovered).begin(
            repository,
            plan["checkpoint"],
            intent_id="cutover_test",
        )["intent_id"]
        == "cutover_test"
    )
    assert (
        CutoverManager(recovered.store, recovered).resume(
            repository, "cutover_test", checkpoint=plan["checkpoint"]
        )["regime"]
        == "workspace"
    )
    recovered.close()


def test_stale_preview_and_wrong_resume_checkpoint_refuse(
    tmp_path: Path, repository: Path, git_run: Callable[..., str]
) -> None:
    authority = _owner(tmp_path, repository, "a")
    manager = CutoverManager(authority.store, authority)
    plan = manager.preview(repository)
    git_run(repository, "commit", "--allow-empty", "-m", "new head")
    with pytest.raises(LlmCoordError) as error:
        manager.begin(repository, plan["checkpoint"])
    assert error.value.code == ErrorCode.CUTOVER_CHECKPOINT_STALE
    assert manager.preview(repository)["regime"] == "legacy"
    fresh = manager.preview(repository)
    manager.begin(repository, fresh["checkpoint"], intent_id="cutover_test")
    with pytest.raises(LlmCoordError) as error:
        manager.resume(repository, "cutover_test", checkpoint="0" * 64)
    assert error.value.code == ErrorCode.CUTOVER_CHECKPOINT_STALE
    authority.close()


def test_regime_observer_never_writes_inside_existing_transaction(
    tmp_path: Path, repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = _owner(tmp_path, repository, "a")
    with authority.pin(repository):
        monkeypatch.setattr(
            authority.installation,
            "validate",
            lambda: (_ for _ in ()).throw(AssertionError("write validation")),
        )
        with authority.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            assert authority.regime(repository)["regime"] == "legacy"
            connection.rollback()
    authority.close()


def test_monotonic_storage_and_old_marker_contract(
    tmp_path: Path, repository: Path
) -> None:
    authority = _owner(tmp_path, repository, "a")
    manager = CutoverManager(authority.store, authority)
    plan = manager.preview(repository)
    manager.begin(repository, plan["checkpoint"])
    with authority.store.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE checkout_regimes SET regime='legacy',generation=0"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM checkout_regimes")
    marker = private_read(_marker(authority, repository))
    assert marker is not None and marker["version"] != 1
    authority.close()


def _claim(authority: PhysicalAuthority, state: str = "queued") -> str:
    store = authority.store
    with store.connection() as connection:
        repo = connection.execute("SELECT * FROM repositories").fetchone()
    task = store.create_task(repository_id=repo["repository_id"], title="test")
    now = store._now(None)
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO claims(claim_id,task_id,task_attempt,repo_key,state,"
            "queue_sequence,fencing_token,lease_expires_at,created_at,updated_at) "
            "VALUES ('claim_test',?,1,?,?,1,?,?,?,?)",
            (
                task.task_id,
                repo["repo_key"],
                state,
                1 if state == "active_work" else None,
                now + 100_000 if state == "active_work" else None,
                now,
                now,
            ),
        )
        connection.execute(
            "UPDATE tasks SET current_claim_id='claim_test',state=?,"
            "coordination_state=? WHERE task_id=?",
            ("running" if state == "active_work" else "queued", state, task.task_id),
        )
    return "claim_test"


@pytest.mark.parametrize("cancel", [False, True])
def test_queued_claim_is_retained_by_default_or_explicitly_cancelled(
    tmp_path: Path,
    repository: Path,
    cancel: bool,
) -> None:
    authority = _owner(tmp_path, repository, "a")
    claim_id = _claim(authority)
    manager = CutoverManager(authority.store, authority)
    plan = manager.preview(repository)
    assert plan["ready"]
    result = manager.begin(
        repository, plan["checkpoint"], {claim_id: "cancel"} if cancel else {}
    )
    assert result["regime"] == "workspace"
    with authority.store.connection() as connection:
        claim = connection.execute(
            "SELECT * FROM claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        assert claim["state"] == ("cancelled" if cancel else "queued")
        saved = connection.execute(
            "SELECT * FROM cutover_dispositions WHERE kind='claim' AND object_id=?",
            (claim_id,),
        ).fetchone()
        assert saved["prior_state"] == "queued"
        assert saved["disposition"] == ("cancel" if cancel else "retain_legacy")
        assert not legacy_activation_allowed(connection, claim["repo_key"])
        event = connection.execute(
            "SELECT payload_json FROM checkout_events "
            "WHERE event_type='checkout.regime_cutover'"
        ).fetchone()
        assert event is not None and claim_id in event[0]
    authority.close()


def test_queue_remains_blocked_after_transition_crash(
    tmp_path: Path,
    repository: Path,
) -> None:
    authority = _owner(tmp_path, repository, "a")
    claim_id = _claim(authority)

    def crash(stage: str) -> None:
        if stage == "transition_marked":
            raise RuntimeError("crash")

    manager = CutoverManager(authority.store, authority, fault=crash)
    plan = manager.preview(repository)
    with pytest.raises(RuntimeError):
        manager.begin(
            repository,
            plan["checkpoint"],
            intent_id="queue_cutover",
        )
    with authority.store.connection() as connection:
        assert (
            connection.execute(
                "SELECT state FROM claims WHERE claim_id=?", (claim_id,)
            ).fetchone()[0]
            == "queued"
        )
        assert not legacy_activation_allowed(
            connection, plan["mappings"][0]["repo_key"]
        )
    result = CutoverManager(authority.store, authority).resume(
        repository, "queue_cutover", checkpoint=plan["checkpoint"]
    )
    assert result["regime"] == "workspace"
    authority.close()


def test_active_claim_and_unfinished_journal_block_before_transition(
    tmp_path: Path,
    repository: Path,
) -> None:
    authority = _owner(tmp_path, repository, "a")
    claim_id = _claim(authority, "active_work")
    manager = CutoverManager(authority.store, authority)
    plan = manager.preview(repository)
    with pytest.raises(LlmCoordError):
        manager.begin(repository, plan["checkpoint"], {claim_id: "cancel"})
    assert manager.preview(repository)["regime"] == "legacy"
    with authority.mutation(repository, "manual", "other-operation"):
        pass
    prior = private_read(_marker(authority, repository))
    with pytest.raises(LlmCoordError):
        manager.begin(repository, plan["checkpoint"])
    assert private_read(_marker(authority, repository)) == prior
    authority.close()


@pytest.mark.parametrize("regime", ["transition", "workspace"])
def test_crash_between_marker_write_and_database_phase_is_idempotent(
    tmp_path: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    regime: str,
) -> None:
    authority = _owner(tmp_path, repository, "a")
    manager = CutoverManager(authority.store, authority)
    plan = manager.preview(repository)
    original = authority.advance_regime

    def interrupted(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        if kwargs["regime"] == regime:
            raise RuntimeError("marker written")

    monkeypatch.setattr(authority, "advance_regime", interrupted)
    with pytest.raises(RuntimeError, match="marker written"):
        manager.begin(repository, plan["checkpoint"], intent_id="split_cutover")
    monkeypatch.setattr(authority, "advance_regime", original)
    result = manager.resume(repository, "split_cutover", checkpoint=plan["checkpoint"])
    assert result["regime"] == "workspace" and result["generation"] == 2
    authority.close()


def test_all_explicit_targets_adapters_and_clones_are_previewed(
    tmp_path: Path,
    repository: Path,
    git_run: Callable[..., str],
) -> None:
    authority = _owner(tmp_path, repository, "a")
    store = authority.store
    with store.connection() as connection:
        repo = connection.execute("SELECT * FROM repositories").fetchone()
    other = tmp_path / "clone"
    git_run(repository, "clone", "--no-hardlinks", str(repository), str(other))
    store.register_repository(
        repo_key=repo["repo_key"],
        display_name="other",
        git_common_dir=str(other / ".git"),
        main_worktree_path=str(other),
        target_ref="refs/heads/other",
        profile_id="a",
        integration_adapter="fixture",
    )
    plan = CutoverManager(store, authority).preview(repository)
    assert plan["ready"]
    assert {item["target_ref"] for item in plan["mappings"]} >= {
        repo["target_ref"],
        "refs/heads/other",
    }
    assert {item["integration_adapter"] for item in plan["mappings"]} == {
        "local",
        "fixture",
    }
    assert len({item["physical_key"] for item in plan["mappings"]}) == 2
    assert {item["canonical_path"] for item in plan["mappings"]} == {
        str(repository),
        str(other),
    }
    authority.close()


def test_unproven_pre_upgrade_clone_history_is_visible_and_blocks(
    tmp_path: Path,
    repository: Path,
) -> None:
    authority = _owner(tmp_path, repository, "a")
    store = authority.store
    with store.connection() as connection:
        repo = connection.execute("SELECT * FROM repositories").fetchone()
        connection.execute(
            "UPDATE repository_registration_provenance SET provenance='legacy_snapshot'"
        )
    task = store.create_task(repository_id=repo["repository_id"], title="old")
    with store.connection() as connection:
        connection.execute(
            "UPDATE tasks SET state='completed' WHERE task_id=?", (task.task_id,)
        )
    manager = CutoverManager(store, authority)
    plan = manager.preview(repository)
    assert any(
        item["reason"] == "unproven_legacy_clone_history" for item in plan["blockers"]
    )
    assert plan["tasks"][0]["task_id"] == task.task_id
    with pytest.raises(LlmCoordError):
        manager.begin(repository, plan["checkpoint"])
    authority.close()


def test_completed_resume_never_confirms_a_later_unrelated_dirty_journal(
    tmp_path: Path,
    repository: Path,
) -> None:
    authority = _owner(tmp_path, repository, "a")
    manager = CutoverManager(authority.store, authority)
    plan = manager.preview(repository)
    result = manager.begin(repository, plan["checkpoint"])
    with authority.mutation(repository, "batch", "later_batch"):
        pass
    dirty = private_read(_marker(authority, repository))
    with pytest.raises(LlmCoordError):
        manager.resume(repository, result["intent_id"], checkpoint=plan["checkpoint"])
    assert private_read(_marker(authority, repository)) == dirty
    with authority.store.connection() as connection:
        assert (
            connection.execute(
                "SELECT state FROM authority_journals WHERE journal_id=?",
                (dirty["journal_id"],),
            ).fetchone()[0]
            == "applying"
        )
    authority.close()


def test_mutation_confirmation_requires_exact_domain_reference(
    tmp_path: Path,
    repository: Path,
) -> None:
    authority = _owner(tmp_path, repository, "a")
    with authority.mutation(repository, "batch", "original"):
        pass
    dirty = private_read(_marker(authority, repository))
    with authority.pin(repository, recovery=True) as pin, pytest.raises(LlmCoordError):
        Mutation(authority, pin.owner, "batch", "different").confirm()
    assert private_read(_marker(authority, repository)) == dirty
    authority.close()


def test_confirmed_g2_execution_proves_pre_upgrade_sessionless_history(
    tmp_path: Path,
    repository: Path,
    git_run: Callable[..., str],
) -> None:
    authority = _owner(tmp_path, repository, "a")
    claim_id = _claim(authority, "active_work")
    store = authority.store
    with store.connection() as connection:
        claim = connection.execute(
            "SELECT * FROM claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
    execution = store.create_execution(
        task_id=claim["task_id"],
        attempt=1,
        claim_id=claim_id,
        driver="fixture_write",
        worktree_path=str(repository),
        base_oid=git_run(repository, "rev-parse", "HEAD"),
        boot_id="old",
    )
    with authority.mutation(
        repository, "legacy_execution", execution.execution_id
    ) as operation:
        operation.confirm()
    now = store._now(None)
    with store.connection() as connection:
        connection.execute(
            "UPDATE repository_registration_provenance SET provenance='legacy_snapshot'"
        )
        connection.execute(
            "UPDATE task_executions SET state='published' WHERE execution_id=?",
            (execution.execution_id,),
        )
        connection.execute(
            "UPDATE claims SET state='released',lease_expires_at=NULL,"
            "terminal_expires_at=?,released_at=? WHERE claim_id=?",
            (now + 100_000, now, claim_id),
        )
        connection.execute(
            "UPDATE tasks SET state='completed',coordination_state='released' "
            "WHERE task_id=?",
            (claim["task_id"],),
        )
    manager = CutoverManager(store, authority)
    plan = manager.preview(repository)
    assert plan["ready"]
    with store.connection() as connection:
        assert not manager._proven_legacy_history(
            connection, claim["repo_key"], {"unmapped_old_checkout"}
        )
    assert plan["authority_receipts"][0]["physical_key"] == plan["physical_key"]
    assert manager.begin(repository, plan["checkpoint"])["regime"] == "workspace"
    authority.close()


def test_independent_proven_clones_can_cut_over_sequentially(
    tmp_path: Path,
    repository: Path,
    git_run: Callable[..., str],
) -> None:
    authority = _owner(tmp_path, repository, "a")
    store = authority.store
    with store.connection() as connection:
        repo = connection.execute("SELECT * FROM repositories").fetchone()
    other = tmp_path / "clone"
    git_run(repository, "clone", "--no-hardlinks", str(repository), str(other))
    store.register_repository(
        repo_key=repo["repo_key"],
        display_name="clone",
        git_common_dir=str(other / ".git"),
        main_worktree_path=str(other),
        target_ref=repo["target_ref"],
        profile_id="a",
    )
    manager = CutoverManager(store, authority)
    first = manager.preview(repository)
    manager.begin(repository, first["checkpoint"])
    second = manager.preview(other)
    assert second["ready"]
    assert manager.begin(other, second["checkpoint"])["regime"] == "workspace"
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM checkout_regimes WHERE regime='workspace'"
            ).fetchone()[0]
            == 2
        )
    authority.close()


@pytest.mark.parametrize("state", ["expired", "publishing", "active_integration"])
def test_unsettled_expiry_and_publication_require_recovery_before_cutover(
    tmp_path: Path,
    repository: Path,
    state: str,
) -> None:
    authority = _owner(tmp_path, repository, "a")
    claim_id = _claim(authority, "active_work")
    now = authority.store._now(None)
    with authority.store.connection() as connection:
        connection.execute(
            "UPDATE claims SET state=?,lease_expires_at=NULL,"
            "terminal_expires_at=? WHERE claim_id=?",
            (state, now + 100_000 if state == "expired" else None, claim_id),
        )
        connection.execute("UPDATE tasks SET coordination_state=?", (state,))
    manager = CutoverManager(authority.store, authority)
    plan = manager.preview(repository)
    assert not plan["ready"]
    assert any(
        d["kind"] == "claim"
        and d["id"] == claim_id
        and d["default"] in {"recover", "drain_or_recover"}
        for d in plan["dispositions"]
    )
    with pytest.raises(LlmCoordError):
        manager.begin(repository, plan["checkpoint"])
    assert manager.preview(repository)["regime"] == "legacy"
    authority.close()


def test_readonly_regime_rejects_changed_retained_identity(
    tmp_path: Path,
    repository: Path,
) -> None:
    authority = _owner(tmp_path, repository, "a")
    with authority.pin(repository):
        pass
    with authority.store.connection() as connection:
        connection.execute("UPDATE authority_checkouts SET binding_json='{}'")
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(LlmCoordError) as error:
            authority.regime(repository)
        assert error.value.code == ErrorCode.CHECKOUT_IDENTITY_CHANGED
        connection.rollback()
    authority.close()


def test_dirty_source_edits_invalidate_checkpoint_even_when_git_status_is_unchanged(
    tmp_path: Path,
    repository: Path,
    git_run: Callable[..., str],
) -> None:
    authority = _owner(tmp_path, repository, "a")
    manager = CutoverManager(authority.store, authority)
    target = repository / "hello.py"
    target.write_text("print('first')\n")
    plan = manager.preview(repository)
    status = git_run(repository, "status", "--porcelain")
    target.write_text("print('other')\n")
    assert git_run(repository, "status", "--porcelain") == status
    assert manager.preview(repository)["checkpoint"] != plan["checkpoint"]
    with pytest.raises(LlmCoordError) as error:
        manager.begin(repository, plan["checkpoint"])
    assert error.value.code == ErrorCode.CUTOVER_CHECKPOINT_STALE
    authority.close()


def test_staging_same_worktree_bytes_invalidates_checkpoint(
    tmp_path: Path,
    repository: Path,
    git_run: Callable[..., str],
) -> None:
    authority = _owner(tmp_path, repository, "a")
    manager = CutoverManager(authority.store, authority)
    (repository / "hello.py").write_text("print('staged')\n")
    plan = manager.preview(repository)
    git_run(repository, "add", "hello.py")
    assert manager.preview(repository)["checkpoint"] != plan["checkpoint"]
    with pytest.raises(LlmCoordError) as error:
        manager.begin(repository, plan["checkpoint"])
    assert error.value.code == ErrorCode.CUTOVER_CHECKPOINT_STALE
    authority.close()


def test_checkpoint_excludes_private_runtime_bodies_and_individual_secret_hashes(
    tmp_path: Path,
    repository: Path,
) -> None:
    from llm_cli.agent.source_policy import register_runtime_paths

    authority = _owner(tmp_path, repository, "a")
    manager = CutoverManager(authority.store, authority)
    private = repository / ".env"
    runtime = repository / "runtime.cache"
    private.write_text("PRIVATE_VALUE=first-private-value\n")
    runtime.write_text("first-runtime-value\n")
    fake = "sk-" + "a" * 40
    (repository / "fixture.txt").write_text(fake)
    register_runtime_paths(repository, ("runtime.cache",))
    try:
        plan = manager.preview(repository)
        serialized = json.dumps(plan)
        assert fake not in serialized
        assert hashlib.sha256(fake.encode()).hexdigest() not in serialized
        assert "first-private-value" not in serialized
        assert "first-runtime-value" not in serialized
        private.write_text("PRIVATE_VALUE=other-private-value\n")
        runtime.write_text("other-runtime-value\n")
        assert manager.preview(repository)["checkpoint"] == plan["checkpoint"]
    finally:
        register_runtime_paths(repository, ())
        authority.close()


def test_unstable_source_observation_fails_closed_without_marker(
    tmp_path: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.workspace import external

    authority = _owner(tmp_path, repository, "a")
    manager = CutoverManager(authority.store, authority)
    original = external.metadata
    changed = False

    def changing(root: Path, path: str) -> str:
        nonlocal changed
        stamp = original(root, path)
        if path == "hello.py" and not changed:
            changed = True
            (root / path).write_text("print('changed during observation')\n")
        return stamp

    monkeypatch.setattr(external, "metadata", changing)
    with pytest.raises(LlmCoordError) as error:
        manager.preview(repository)
    assert error.value.code == ErrorCode.CUTOVER_BLOCKED
    assert not _marker(authority, repository).exists()
    authority.close()


def test_status_and_inventory_never_observe_source_or_issue_a_fresh_checkpoint(
    tmp_path: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.workspace import regime

    authority = _owner(tmp_path, repository, "a")
    manager = CutoverManager(authority.store, authority)
    plan = manager.preview(repository)
    completed = manager.begin(repository, plan["checkpoint"])
    calls: list[str] = []

    def unavailable(physical: Any) -> str:
        calls.append(physical.key)
        raise LlmCoordError(ErrorCode.CUTOVER_BLOCKED, "source is being edited")

    monkeypatch.setattr(regime, "_source_checkpoint", unavailable)
    inventory = manager.inventory(repository)
    assert inventory["mappings"] == plan["mappings"]
    assert inventory["checkpoint"] is None and not inventory["checkpoint_observed"]
    status = manager.status(repository)
    assert status["intent_id"] == completed["intent_id"]
    assert status["checkpoint"] == plan["checkpoint"]
    assert status["current_checkpoint"] is None and not status["checkpoint_observed"]
    assert calls == []
    with pytest.raises(LlmCoordError, match="source is being edited"):
        manager.preview(repository)
    assert len(calls) == 1
    authority.close()


def test_source_over_checkpoint_budget_does_not_block_status_or_inventory(
    tmp_path: Path,
    repository: Path,
) -> None:
    authority = _owner(tmp_path, repository, "a")
    manager = CutoverManager(authority.store, authority)
    with (repository / "large-source.bin").open("wb") as output:
        output.truncate(65 * 1024 * 1024)
    inventory = manager.inventory(repository)
    status = manager.status(repository)
    assert inventory["mappings"] and status["regime"] == "legacy"
    assert inventory["checkpoint"] is None and status["checkpoint"] is None
    assert not inventory["checkpoint_observed"] and not status["checkpoint_observed"]
    with pytest.raises(LlmCoordError) as error:
        manager.preview(repository)
    assert error.value.code == ErrorCode.CUTOVER_BLOCKED
    assert not _marker(authority, repository).exists()
    authority.close()


def _crash_at(boundary: str) -> Callable[[str], None]:
    def crash(stage: str) -> None:
        if stage == boundary:
            raise RuntimeError("crash")

    return crash


def test_editor_save_during_apply_does_not_strand_transition(
    tmp_path: Path, repository: Path
) -> None:
    authority = _owner(tmp_path, repository, "a")
    target = repository / "hello.py"

    def editor_saves(stage: str) -> None:
        if stage == "transition_marked":
            target.write_bytes(target.read_bytes())

    manager = CutoverManager(authority.store, authority, fault=editor_saves)
    plan = manager.preview(repository)
    result = manager.begin(repository, plan["checkpoint"], intent_id="cutover_save")
    assert result["regime"] == "workspace" and result["generation"] == 2
    authority.close()


@pytest.mark.parametrize(
    "boundary",
    ["intent_saved", "transition_marked", "checkpoint_saved", "workspace_marked"],
)
def test_resume_tolerates_source_index_and_head_changes_after_intent(
    tmp_path: Path,
    repository: Path,
    git_run: Callable[..., str],
    boundary: str,
) -> None:
    authority = _owner(tmp_path, repository, "a")
    manager = CutoverManager(authority.store, authority, fault=_crash_at(boundary))
    plan = manager.preview(repository)
    with pytest.raises(RuntimeError, match="crash"):
        manager.begin(repository, plan["checkpoint"], intent_id="cutover_edit")
    target = repository / "hello.py"
    target.write_bytes(target.read_bytes())
    target.write_text("print('edited')\n")
    git_run(repository, "add", "hello.py")
    git_run(repository, "commit", "-q", "-m", "edited during cutover")
    result = CutoverManager(authority.store, authority).resume(
        repository, "cutover_edit", checkpoint=plan["checkpoint"]
    )
    assert result["regime"] == "workspace" and result["generation"] == 2
    authority.close()


def test_retained_legacy_history_change_during_transition_still_refuses(
    tmp_path: Path, repository: Path
) -> None:
    authority = _owner(tmp_path, repository, "a")
    claim_id = _claim(authority)
    manager = CutoverManager(
        authority.store, authority, fault=_crash_at("transition_marked")
    )
    plan = manager.preview(repository)
    with pytest.raises(RuntimeError, match="crash"):
        manager.begin(repository, plan["checkpoint"], intent_id="cutover_history")
    with authority.store.connection() as connection:
        connection.execute(
            "UPDATE claims SET updated_at=updated_at+1 WHERE claim_id=?", (claim_id,)
        )
    with pytest.raises(LlmCoordError) as error:
        CutoverManager(authority.store, authority).resume(
            repository, "cutover_history", checkpoint=plan["checkpoint"]
        )
    assert error.value.code == ErrorCode.CUTOVER_CHECKPOINT_STALE
    assert authority.regime(repository)["regime"] == "transition"
    authority.close()


def test_saved_intent_reads_as_transition_before_its_marker_advances(
    tmp_path: Path, repository: Path
) -> None:
    authority = _owner(tmp_path, repository, "a")
    manager = CutoverManager(
        authority.store, authority, fault=_crash_at("intent_saved")
    )
    plan = manager.preview(repository)
    with pytest.raises(RuntimeError, match="crash"):
        manager.begin(repository, plan["checkpoint"], intent_id="cutover_pending")
    marker = private_read(_marker(authority, repository))
    assert marker is not None and marker["version"] == 1
    observed = authority.regime(repository)
    assert observed["regime"] == "transition" and observed["generation"] == 0
    with pytest.raises(LlmCoordError) as error:
        authority.assert_regime(repository, allowed=("legacy", "workspace"))
    assert error.value.code == ErrorCode.CUTOVER_BLOCKED
    authority.close()


def test_apply_hashes_source_outside_the_write_transaction(
    tmp_path: Path, repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = _owner(tmp_path, repository, "a")
    manager = CutoverManager(authority.store, authority)
    plan = manager.preview(repository)
    original = regime_module._source_checkpoint
    writable: list[bool] = []

    def observed(physical: Any) -> str:
        # Another daemon writer must not wait behind the source scan.
        with closing(sqlite3.connect(authority.store.path, timeout=0.1)) as other:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        writable.append(True)
        return original(physical)

    monkeypatch.setattr(regime_module, "_source_checkpoint", observed)
    manager.begin(repository, plan["checkpoint"])
    assert writable
    authority.close()
