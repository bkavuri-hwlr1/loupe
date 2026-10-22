"""Retention preserves recovery/history across both execution contracts."""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_retention import OLD, _checkpoint, _execution, _workflow
from test_retention import store as store

from llm_cli.storage.control import ControlStore
from llm_cli.storage.retention import prune_task_output

_FINISHED = {
    "version": 1,
    "phase": "finished",
    "finalization": None,
    "session": {"input": ["saved conversation" * 2000]},
    "tool_usage": {
        "calls": 4,
        "shared_workspace_state": {
            "files": [{"path": "docs/a.md", "content": "published source" * 2000}],
        },
    },
}


def _workspace_execution(
    store: ControlStore,
    *,
    state: str = "published",
    checkpoint: dict[str, Any] | None = _FINISHED,
) -> str:
    repository = store.list_repositories()[0]
    checkout = store.ensure_checkout(
        repository=repository,
        canonical_path=repository.main_worktree_path,
        git_common_dir=repository.git_common_dir,
        now=OLD,
    )
    workspace = store.ensure_shared_workspace(checkout, now=OLD)
    session, _, sequence = store.open_session(
        session_id="workspace-session",
        checkout_id=checkout.checkout_id,
        resume_token_hash="a" * 64,
        provider="fixture",
        model="fixture",
        workspace_id=workspace.workspace_id,
        now=OLD,
    )
    store.acknowledge_session(session.session_id, sequence=sequence, now=OLD)
    store.create_task(
        repository_id=repository.repository_id,
        task_id="workspace-task",
        session_id=session.session_id,
        title="retained workspace history",
        now=OLD,
    )
    execution = store.create_workspace_execution(
        task_id="workspace-task",
        attempt=1,
        session_id=session.session_id,
        workspace_id=workspace.workspace_id,
        scopes=("docs/",),
        physical_key="p" * 64,
        regime_generation=2,
        driver="coding-agent",
        worktree_path=workspace.canonical_path,
        base_oid="a" * 40,
        boot_id="stopped",
        now=OLD,
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE workspace_executions SET state=? WHERE execution_id=?",
            (state, execution.execution_id),
        )
        connection.execute(
            "UPDATE tasks SET state='completed',coordination_state='released',"
            "finished_at=?,updated_at=? "
            "WHERE task_id='workspace-task'",
            (OLD, OLD),
        )
        if checkpoint is not None:
            connection.execute(
                "INSERT INTO workspace_execution_checkpoints(execution_id,driver,"
                "checkpoint_json,created_at,updated_at) "
                "VALUES (?,'coding-agent',?,?,?)",
                (execution.execution_id, json.dumps(checkpoint), OLD, OLD),
            )
    store.record_task_event(
        task_id="workspace-task",
        claim_id=None,
        event_type="model.tool_result",
        payload={"content": "disposable tool output" * 1000},
        now=OLD,
    )
    return execution.execution_id


def _workspace_checkpoint(store: ControlStore, execution_id: str) -> object:
    with store.connection() as connection:
        row = connection.execute(
            "SELECT checkpoint_json FROM workspace_execution_checkpoints "
            "WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def test_both_ledgers_slim_only_settled_checkpoints_and_keep_answer_history(
    store: ControlStore,
) -> None:
    legacy = _execution(store, "published", _FINISHED)
    workspace = _workspace_execution(store)
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO workspace_execution_answers(execution_id,answer_id,version,"
            "payload_json,content_sha256,first_event_sequence,last_event_sequence,"
            "created_at) VALUES (?,'answer-original',1,'{}',?,1,1,?)",
            (workspace, "a" * 64, OLD),
        )
        connection.execute(
            "INSERT INTO workspace_execution_answer_revisions(execution_id,revision,"
            "answer_id,payload_json,content_sha256,first_event_sequence,"
            "last_event_sequence,created_at) "
            "VALUES (?,2,'answer-revision','{}',?,2,2,?)",
            (workspace, "b" * 64, OLD),
        )
        answers = [
            tuple(row)
            for row in connection.execute("SELECT * FROM all_execution_answers")
        ]
        revisions = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM all_execution_answer_revisions"
            )
        ]
    preview = prune_task_output(store, older_than="30d")
    assert preview["eligible_tasks"] == preview["checkpoints"] == 2
    assert _checkpoint(store, legacy) == _FINISHED
    assert _workspace_checkpoint(store, workspace) == _FINISHED
    result = prune_task_output(store, older_than="30d", apply=True)
    assert result["checkpoints"] == 2
    for current in (
        _checkpoint(store, legacy),
        _workspace_checkpoint(store, workspace),
    ):
        assert isinstance(current, dict)
        assert "session" not in current
        assert current["tool_usage"] == {"calls": 4}
    assert store.get_execution("workspace-task", 1).execution_contract == "workspace_v1"
    with store.connection() as connection:
        assert [
            tuple(row)
            for row in connection.execute("SELECT * FROM all_execution_answers")
        ] == answers
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM all_execution_answer_revisions"
            )
        ] == revisions
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    assert prune_task_output(store, older_than="30d", apply=True)["checkpoints"] == 0


@pytest.mark.parametrize(
    "state",
    [
        "preparing",
        "running",
        "validated",
        "publishing",
        "operator_attention",
    ],
)
def test_nonterminal_workspace_execution_protects_output_and_checkpoint(
    store: ControlStore,
    state: str,
) -> None:
    execution = _workspace_execution(store, state=state)
    events = store.list_task_events("workspace-task")
    assert prune_task_output(store, older_than="30d", apply=True)["eligible_tasks"] == 0
    assert store.list_task_events("workspace-task") == events
    assert _workspace_checkpoint(store, execution) == _FINISHED


@pytest.mark.parametrize(
    "checkpoint",
    [
        None,
        {"phase": None},
        {"phase": "finalization_pending"},
    ],
)
def test_missing_or_unsettled_workspace_checkpoint_preserves_all_evidence(
    store: ControlStore,
    checkpoint: dict[str, Any] | None,
) -> None:
    execution = _workspace_execution(store, checkpoint=checkpoint)
    events = store.list_task_events("workspace-task")
    original = _workspace_checkpoint(store, execution)
    result = prune_task_output(store, older_than="30d", apply=True)
    assert result["eligible_tasks"] == result["checkpoints"] == 0
    assert store.list_task_events("workspace-task") == events
    assert _workspace_checkpoint(store, execution) == original


def test_held_workspace_proposal_keeps_its_private_checkpoint(
    store: ControlStore,
) -> None:
    execution = _workspace_execution(store)
    _workflow(store, "workspace-task")
    with store.connection() as connection:
        connection.execute(
            "UPDATE task_workflows SET status='awaiting_review',proposal_json='[]' "
            "WHERE task_id='workspace-task'"
        )
    events = store.list_task_events("workspace-task")
    assert prune_task_output(store, older_than="30d", apply=True)["eligible_tasks"] == 0
    assert store.list_task_events("workspace-task") == events
    assert _workspace_checkpoint(store, execution) == _FINISHED


@pytest.mark.parametrize("batch,eligible", [("applying", 0), ("diverged", 1)])
def test_workspace_execution_batch_recovery_is_not_an_orphan(
    store: ControlStore,
    batch: str,
    eligible: int,
) -> None:
    execution = _workspace_execution(store, state="failed")
    with store.connection() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO workspace_batches(batch_id,session_id,workspace_id,"
            "checkout_id,canonical_path,git_common_dir,request_json,state,"
            "created_at,updated_at) VALUES (?,'s','w','c','/r','/r/.git','{}',?,?,?)",
            (execution, batch, OLD, OLD),
        )
    assert prune_task_output(store, older_than="30d")["eligible_tasks"] == eligible
