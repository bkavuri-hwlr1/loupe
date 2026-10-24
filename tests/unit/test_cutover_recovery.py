"""Accepted current-history recovery stays forward-only and exactly replayable."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_cutover_regime import _claim, _crash_at
from test_physical_authority import _marker, _owner
from test_workspace_cutover_cli import _Client

from llm_cli.cli.app import build_parser, dispatch
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.workspace import regime as module
from llm_cli.workspace.authority import PhysicalAuthority
from llm_cli.workspace.installation import private_read, private_write
from llm_cli.workspace.regime import CutoverManager


def _prepared(
    tmp_path: Path,
    factory: Callable[..., Path],
    boundary: str = "intent_saved",
    *,
    cancel: bool = False,
) -> tuple[Path, PhysicalAuthority, CutoverManager, dict[str, Any]]:
    repository = factory(tmp_path, {"source.py": "VALUE = 1\n"})
    authority = _owner(tmp_path, repository, "owner")
    claim = _claim(authority)
    manager = CutoverManager(authority.store, authority, fault=_crash_at(boundary))
    plan = manager.preview(repository)
    with pytest.raises(RuntimeError, match="crash"):
        manager.begin(
            repository,
            plan["checkpoint"],
            {claim: "cancel"} if cancel else {},
            intent_id="cutover-recovery",
        )
    return repository, authority, CutoverManager(authority.store, authority), plan


def _change_history(authority: PhysicalAuthority) -> None:
    with authority.store.connection() as connection:
        connection.execute("UPDATE claims SET updated_at=updated_at+1")


def _preview(
    manager: CutoverManager, root: Path, plan: dict[str, Any]
) -> dict[str, Any]:
    return manager.recovery(root, "cutover-recovery", checkpoint=plan["checkpoint"])


def _acks(authority: PhysicalAuthority) -> list[dict[str, Any]]:
    with authority.store.connection() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM cutover_recovery_acknowledgements "
                "ORDER BY acknowledgement_id"
            )
        ]


@pytest.mark.parametrize("boundary", ["intent_saved", "transition_marked"])
def test_explicit_history_ack_preserves_original_intent_and_dispositions(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    boundary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, authority, manager, plan = _prepared(tmp_path, repository_factory, boundary)
    with authority.store.connection() as connection:
        original = dict(connection.execute("SELECT * FROM cutover_intents").fetchone())
        dispositions = [
            tuple(row)
            for row in connection.execute("SELECT * FROM cutover_dispositions")
        ]
    _change_history(authority)
    with pytest.raises(LlmCoordError) as error:
        manager.resume(root, "cutover-recovery", checkpoint=plan["checkpoint"])
    assert error.value.code == ErrorCode.CUTOVER_CHECKPOINT_STALE
    before = private_read(_marker(authority, root))
    monkeypatch.setattr(
        module, "_source_checkpoint", lambda physical: pytest.fail("source read")
    )
    recovery = _preview(manager, root, plan)
    assert recovery["ready"] and recovery["metadata_only"]
    assert not recovery["checkpoint_observed"] and _acks(authority) == []
    assert private_read(_marker(authority, root)) == before
    result = manager.resume(
        root,
        "cutover-recovery",
        checkpoint=plan["checkpoint"],
        accept_current_history=recovery["recovery_checkpoint"],
    )
    assert result["regime"] == "workspace" and result["generation"] == 2
    (ack,) = _acks(authority)
    assert ack["original_history_checkpoint"] == plan["history_checkpoint"]
    assert ack["accepted_history_checkpoint"] == recovery["history_checkpoint"]
    assert ack["operator_action"] == "accept_current_history"
    assert ack["created_at"] > 0
    with authority.store.connection() as connection:
        saved = connection.execute("SELECT * FROM cutover_intents").fetchone()
        for key in ("checkpoint", "plan_json", "dispositions_json"):
            assert saved[key] == original[key]
        assert [
            tuple(row)
            for row in connection.execute("SELECT * FROM cutover_dispositions")
        ] == dispositions
        for sql in (
            "UPDATE cutover_recovery_acknowledgements SET operator_action='other'",
            "DELETE FROM cutover_recovery_acknowledgements",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)
    authority.close()


def test_ack_saved_crash_replays_once_and_new_history_needs_a_new_ack(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root, authority, manager, plan = _prepared(tmp_path, repository_factory)
    _change_history(authority)
    recovery = _preview(manager, root, plan)
    crashing = CutoverManager(authority.store, authority, fault=_crash_at("ack_saved"))
    with pytest.raises(RuntimeError, match="crash"):
        crashing.resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=recovery["recovery_checkpoint"],
        )
    assert len(_acks(authority)) == 1
    _change_history(authority)
    with pytest.raises(LlmCoordError) as error:
        manager.resume(root, "cutover-recovery", checkpoint=plan["checkpoint"])
    assert error.value.code == ErrorCode.CUTOVER_CHECKPOINT_STALE
    assert len(_acks(authority)) == 1
    recovery = _preview(manager, root, plan)
    with pytest.raises(RuntimeError, match="crash"):
        crashing.resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=recovery["recovery_checkpoint"],
        )
    assert len(_acks(authority)) == 2
    authority.close()
    restarted = _owner(tmp_path, root, "owner")
    result = CutoverManager(restarted.store, restarted).resume(
        root, "cutover-recovery", checkpoint=plan["checkpoint"]
    )
    assert result["regime"] == "workspace" and len(_acks(restarted)) == 2
    # Retrying the acknowledged command after completion stays exact/idempotent.
    assert (
        CutoverManager(restarted.store, restarted).resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=recovery["recovery_checkpoint"],
        )["regime"]
        == "workspace"
    )
    restarted.close()


def test_stale_recovery_token_does_not_write_acknowledgement(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root, authority, manager, plan = _prepared(tmp_path, repository_factory)
    recovery = _preview(manager, root, plan)
    _change_history(authority)
    with pytest.raises(LlmCoordError) as error:
        manager.resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=recovery["recovery_checkpoint"],
        )
    assert error.value.code == ErrorCode.CUTOVER_CHECKPOINT_STALE
    assert _acks(authority) == []
    authority.close()


def test_foreign_clean_handoff_is_explicitly_recorded_without_importing_history(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root, owner, _, plan = _prepared(tmp_path, repository_factory)
    owner.close()
    peer = _owner(tmp_path, root, "peer")
    with peer.pin(root):
        (root / "source.py").write_text("VALUE = 9\n")
    peer.close()
    owner = _owner(tmp_path, root, "owner")
    manager = CutoverManager(owner.store, owner)
    with pytest.raises(LlmCoordError) as error:
        manager.resume(root, "cutover-recovery", checkpoint=plan["checkpoint"])
    assert error.value.code == ErrorCode.CUTOVER_CHECKPOINT_STALE
    recovery = _preview(manager, root, plan)
    assert recovery["ready"]
    assert [gap["previous_profile"] for gap in recovery["ownership_gaps"]] == ["peer"]
    assert (
        manager.resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=recovery["recovery_checkpoint"],
        )["regime"]
        == "workspace"
    )
    (ack,) = _acks(owner)
    assert json.loads(ack["ownership_gaps_json"]) == recovery["ownership_gaps"]
    assert (root / "source.py").read_text() == "VALUE = 9\n"
    assert "VALUE = 9" not in ack["inventory_json"]
    owner.close()


@pytest.mark.parametrize(
    "damage", ["mapping", "active_claim", "saved_disposition", "selected_claim"]
)
def test_recovery_does_not_bypass_changed_mapping_or_unsafe_work(
    tmp_path: Path, repository_factory: Callable[..., Path], damage: str
) -> None:
    root, authority, manager, plan = _prepared(
        tmp_path, repository_factory, cancel=True
    )
    with authority.store.connection() as connection:
        if damage == "mapping":
            connection.execute("UPDATE repositories SET target_ref='refs/heads/other'")
        elif damage == "active_claim":
            connection.execute(
                "UPDATE claims SET state='active_work',fencing_token=1,"
                "lease_expires_at=?",
                (authority.store._now(None) + 86_400_000,),
            )
        elif damage == "saved_disposition":
            connection.execute(
                "UPDATE cutover_dispositions SET disposition='retain_history'"
            )
        else:
            connection.execute(
                "UPDATE claims SET state='cancelled',terminal_expires_at=?",
                (authority.store._now(None) + 86_400_000,),
            )
    recovery = _preview(manager, root, plan)
    assert not recovery["ready"] and recovery["blockers"]
    with pytest.raises(LlmCoordError) as error:
        manager.resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=recovery["recovery_checkpoint"],
        )
    assert error.value.code == ErrorCode.CUTOVER_BLOCKED
    assert _acks(authority) == []
    authority.close()


@pytest.mark.parametrize("state", ["dirty", "attention"])
def test_foreign_unfinished_journal_is_never_acknowledged(
    tmp_path: Path, repository_factory: Callable[..., Path], state: str
) -> None:
    root, owner, _, plan = _prepared(tmp_path, repository_factory)
    owner.close()
    peer = _owner(tmp_path, root, "peer")
    with peer.mutation(root, "manual", "foreign-publication"):
        pass
    peer.close()
    marker_path = _marker(peer, root)
    marker = private_read(marker_path)
    assert marker is not None
    marker["state"] = state
    private_write(marker_path, marker)
    owner = _owner(tmp_path, root, "owner")
    manager = CutoverManager(owner.store, owner)
    recovery = _preview(manager, root, plan)
    assert not recovery["ready"]
    with pytest.raises(LlmCoordError) as error:
        manager.resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=recovery["recovery_checkpoint"],
        )
    assert error.value.code == ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
    assert _acks(owner) == [] and private_read(marker_path) == marker
    owner.close()


def test_no_recovery_override_after_dispositions_are_checkpointed(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root, authority, manager, plan = _prepared(
        tmp_path, repository_factory, "checkpoint_saved"
    )
    _change_history(authority)
    recovery = _preview(manager, root, plan)
    assert not recovery["ready"]
    with pytest.raises(LlmCoordError) as error:
        manager.resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=recovery["recovery_checkpoint"],
        )
    assert error.value.code == ErrorCode.CUTOVER_BLOCKED
    assert _acks(authority) == []
    authority.close()


def test_recovery_cli_requires_both_tokens_and_sends_only_explicit_acknowledgement(
    tmp_path: Path,
) -> None:
    from typing import cast

    from llm_cli.protocol.client import DaemonClient

    client = _Client()
    parser = build_parser()
    for action in ("recovery", "resume"):
        args = [
            "--json",
            "workspace",
            "cutover",
            action,
            str(tmp_path),
            "--intent",
            "intent",
            "--checkpoint",
            "original",
        ]
        if action == "resume":
            args += ["--accept-current-history", "recovery-token"]
        dispatch(parser.parse_args(args), cast(DaemonClient, client))
    assert client.calls == [
        (
            "workspace.cutover.recovery",
            {"path": str(tmp_path), "intent_id": "intent", "checkpoint": "original"},
        ),
        (
            "workspace.cutover.resume",
            {
                "path": str(tmp_path),
                "intent_id": "intent",
                "checkpoint": "original",
                "accept_current_history": "recovery-token",
            },
        ),
    ]
    with pytest.raises(SystemExit):
        parser.parse_args(["workspace", "cutover", "recovery", "--intent", "intent"])


@pytest.mark.parametrize(
    "boundary",
    ["transition_marked", "checkpoint_saved", "workspace_marked", "intent_completed"],
)
def test_acknowledged_recovery_replays_each_later_fault_boundary(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    boundary: str,
) -> None:
    root, authority, manager, plan = _prepared(
        tmp_path, repository_factory, cancel=True
    )
    _change_history(authority)
    recovery = _preview(manager, root, plan)
    crashing = CutoverManager(authority.store, authority, fault=_crash_at(boundary))
    with pytest.raises(RuntimeError, match="crash"):
        crashing.resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=recovery["recovery_checkpoint"],
        )
    authority.close()
    restarted = _owner(tmp_path, root, "owner")
    result = CutoverManager(restarted.store, restarted).resume(
        root,
        "cutover-recovery",
        checkpoint=plan["checkpoint"],
        accept_current_history=recovery["recovery_checkpoint"],
    )
    assert result["regime"] == "workspace"
    assert len(_acks(restarted)) == 1
    with restarted.store.connection() as connection:
        assert (
            connection.execute("SELECT state FROM claims").fetchone()[0] == "cancelled"
        )
    restarted.close()


def test_mapping_changed_after_saved_ack_still_blocks_exact_replay(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root, authority, manager, plan = _prepared(tmp_path, repository_factory)
    recovery = _preview(manager, root, plan)
    crashing = CutoverManager(authority.store, authority, fault=_crash_at("ack_saved"))
    with pytest.raises(RuntimeError, match="crash"):
        crashing.resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=recovery["recovery_checkpoint"],
        )
    with authority.store.connection() as connection:
        connection.execute("UPDATE repositories SET target_ref='refs/heads/changed'")
    with pytest.raises(LlmCoordError) as caught:
        manager.resume(root, "cutover-recovery", checkpoint=plan["checkpoint"])
    assert caught.value.code == ErrorCode.CUTOVER_BLOCKED
    assert len(_acks(authority)) == 1
    authority.close()


def test_new_foreign_handoff_after_ack_requires_another_reviewed_ack(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root, owner, manager, plan = _prepared(tmp_path, repository_factory)
    recovery = _preview(manager, root, plan)
    crashing = CutoverManager(owner.store, owner, fault=_crash_at("ack_saved"))
    with pytest.raises(RuntimeError, match="crash"):
        crashing.resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=recovery["recovery_checkpoint"],
        )
    owner.close()
    peer = _owner(tmp_path, root, "peer")
    with peer.pin(root):
        pass
    peer.close()
    owner = _owner(tmp_path, root, "owner")
    manager = CutoverManager(owner.store, owner)
    with pytest.raises(LlmCoordError) as caught:
        manager.resume(root, "cutover-recovery", checkpoint=plan["checkpoint"])
    assert caught.value.code == ErrorCode.CUTOVER_CHECKPOINT_STALE
    recovery = _preview(manager, root, plan)
    assert (
        manager.resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=recovery["recovery_checkpoint"],
        )["regime"]
        == "workspace"
    )
    assert len(_acks(owner)) == 2
    owner.close()


def test_live_pin_blocks_acceptance_before_ack_is_written(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root, authority, manager, plan = _prepared(tmp_path, repository_factory)
    recovery = _preview(manager, root, plan)
    with authority.pin(root):
        with pytest.raises(LlmCoordError) as caught:
            manager.resume(
                root,
                "cutover-recovery",
                checkpoint=plan["checkpoint"],
                accept_current_history=recovery["recovery_checkpoint"],
            )
        assert caught.value.code == ErrorCode.CUTOVER_BLOCKED
    assert _acks(authority) == []
    authority.close()


def test_service_recovery_rpc_is_read_only_until_the_explicit_resume_ack(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., Any],
    request_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    async def scenario() -> None:
        root = repository_factory(tmp_path, {"source.py": "VALUE = 1\n"})
        service = service_factory(tmp_path)
        service.initialize()
        ping = await service.handle(request_factory("system.ping", {}))
        assert {
            "workspace.authority.maintenance.v1",
            "workspace.cutover.recovery.v1",
        } <= set(ping["capabilities"])
        await service.handle(request_factory("repo.add", {"path": str(root)}))
        manager = CutoverManager(
            service.store, service.physical_authority, fault=_crash_at("intent_saved")
        )
        plan = manager.preview(root)
        with pytest.raises(RuntimeError, match="crash"):
            manager.begin(root, plan["checkpoint"], intent_id="rpc-recovery")
        service.cutovers = CutoverManager(service.store, service.physical_authority)
        monkeypatch.setattr(
            module, "_source_checkpoint", lambda physical: pytest.fail("source read")
        )
        params = {
            "path": str(root),
            "intent_id": "rpc-recovery",
            "checkpoint": plan["checkpoint"],
        }
        recovery = await service.handle(
            request_factory("workspace.cutover.recovery", params)
        )
        assert recovery["metadata_only"] and recovery["ready"]
        assert _acks(service.physical_authority) == []
        result = await service.handle(
            request_factory(
                "workspace.cutover.resume",
                {
                    **params,
                    "accept_current_history": recovery["recovery_checkpoint"],
                },
            )
        )
        assert result["regime"] == "workspace"
        assert len(_acks(service.physical_authority)) == 1
        service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("wrong", ["intent", "checkpoint"])
def test_recovery_preview_requires_original_intent_and_checkpoint(
    tmp_path: Path, repository_factory: Callable[..., Path], wrong: str
) -> None:
    root, authority, manager, plan = _prepared(tmp_path, repository_factory)
    with pytest.raises(LlmCoordError) as caught:
        manager.recovery(
            root,
            "wrong" if wrong == "intent" else "cutover-recovery",
            checkpoint="wrong" if wrong == "checkpoint" else plan["checkpoint"],
        )
    assert caught.value.code == (
        ErrorCode.CUTOVER_BLOCKED
        if wrong == "intent"
        else ErrorCode.CUTOVER_CHECKPOINT_STALE
    )
    assert _acks(authority) == []
    authority.close()


def test_plain_recovery_preview_names_the_observed_foreign_gap(tmp_path: Path) -> None:
    from typing import cast

    from llm_cli.protocol.client import DaemonClient

    client = _Client(
        {
            "regime": "transition",
            "generation": 0,
            "ready": True,
            "checkpoint": "original",
            "intent_id": "intent",
            "mappings": [],
            "dispositions": [],
            "blockers": [],
            "recovery_checkpoint": "recovery-token",
            "ownership_gaps": [
                {"checkout_id": "checkout", "sequence": 7, "previous_profile": "peer"}
            ],
        }
    )
    args = build_parser().parse_args(
        [
            "workspace",
            "cutover",
            "recovery",
            str(tmp_path),
            "--intent",
            "intent",
            "--checkpoint",
            "original",
        ]
    )
    result = dispatch(args, cast(DaemonClient, client))
    assert "Cutover recovery preview" in result
    assert "Recovery checkpoint: recovery-token" in result
    assert 'checkout event 7; previous profile="peer"' in result


def test_direct_recovery_preview_discloses_pending_foreign_owner_and_is_not_ready(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
) -> None:
    root, owner, _, plan = _prepared(tmp_path, repository_factory)
    owner.close()
    peer = _owner(tmp_path, root, "peer")
    with peer.pin(root):
        pass
    peer.close()
    owner = _owner(tmp_path, root, "owner")
    manager = CutoverManager(owner.store, owner)
    marker = private_read(_marker(owner, root))
    recovery = _preview(manager, root, plan)
    assert not recovery["ready"]
    assert recovery["ownership_gaps"] == []
    assert recovery["pending_owner"]["owner_profile"] == "peer"
    assert recovery["pending_owner"]["marker_generation"] == marker["generation"]
    assert "--profile owner workspace cutover resume" in recovery["reacquire_command"]
    assert "--accept-current-history" not in recovery["reacquire_command"]
    assert any(
        b["reason"] == "foreign_ownership_gap_not_observed"
        for b in recovery["blockers"]
    )
    assert private_read(_marker(owner, root)) == marker and _acks(owner) == []
    with pytest.raises(LlmCoordError) as caught:
        manager.resume(root, "cutover-recovery", checkpoint=plan["checkpoint"])
    assert caught.value.code == ErrorCode.CUTOVER_CHECKPOINT_STALE
    fresh = _preview(manager, root, plan)
    assert fresh["ready"] and fresh["pending_owner"] is None
    assert fresh["reacquire_command"] is None
    assert fresh["ownership_gaps"][0]["previous_profile"] == "peer"
    assert (
        manager.resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=fresh["recovery_checkpoint"],
        )["regime"]
        == "workspace"
    )
    assert len(_acks(owner)) == 1
    owner.close()


def test_same_profile_name_in_another_database_is_a_foreign_ownership_gap(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root, owner, _, plan = _prepared(tmp_path, repository_factory)
    owner.close()
    another_installation = tmp_path / "another-installation"
    another_installation.mkdir(mode=0o700)
    peer = _owner(another_installation, root, "owner")
    # Two configured data roots still share the real account's authority domain.
    peer.root = owner.root
    with peer.pin(root):
        peer_instance = peer.installation.instance_id
    peer.close()
    owner = _owner(tmp_path, root, "owner")
    manager = CutoverManager(owner.store, owner)
    recovery = _preview(manager, root, plan)
    assert not recovery["ready"]
    assert recovery["pending_owner"]["owner_profile"] == "owner"
    assert recovery["pending_owner"]["database_instance_id"] == peer_instance
    with pytest.raises(LlmCoordError) as caught:
        manager.resume(root, "cutover-recovery", checkpoint=plan["checkpoint"])
    assert caught.value.code == ErrorCode.CUTOVER_CHECKPOINT_STALE
    recovery = _preview(manager, root, plan)
    (gap,) = recovery["ownership_gaps"]
    assert gap["previous_profile"] == "owner"
    assert gap["previous_database_instance_id"] == peer_instance
    assert recovery["ready"]
    assert (
        manager.resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=recovery["recovery_checkpoint"],
        )["regime"]
        == "workspace"
    )
    assert json.loads(_acks(owner)[0]["ownership_gaps_json"]) == [gap]
    owner.close()


def test_legacy_handoff_without_database_identity_needs_explicit_acknowledgement(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root, owner, manager, plan = _prepared(tmp_path, repository_factory)
    with owner.store.connection() as connection:
        checkout = connection.execute("SELECT checkout_id FROM checkouts").fetchone()[0]
        owner.store.append_checkout_event(
            connection,
            checkout_id=checkout,
            event_type="checkout.authority_handoff",
            caused_by_session_id=None,
            payload={"previous_profile": "owner", "history_gap": True, "paths": []},
            now=owner.store._now(None),
        )
    with pytest.raises(LlmCoordError) as caught:
        manager.resume(root, "cutover-recovery", checkpoint=plan["checkpoint"])
    assert caught.value.code == ErrorCode.CUTOVER_CHECKPOINT_STALE
    recovery = _preview(manager, root, plan)
    (gap,) = recovery["ownership_gaps"]
    assert gap["previous_profile"] == "owner"
    assert gap["previous_database_instance_id"] is None
    assert recovery["ready"]
    assert (
        manager.resume(
            root,
            "cutover-recovery",
            checkpoint=plan["checkpoint"],
            accept_current_history=recovery["recovery_checkpoint"],
        )["regime"]
        == "workspace"
    )
    owner.close()
