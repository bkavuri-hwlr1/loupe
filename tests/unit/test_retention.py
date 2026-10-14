from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from llm_cli.agent.harness import _emit_transcript, _LiveEvents
from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.storage.control import ControlStore
from llm_cli.storage.retention import prune_task_output, retention_age_ms

DAY = 86_400_000
NOW = 100 * DAY
OLD = DAY


@pytest.fixture
def store(tmp_path: Path) -> ControlStore:
    store = ControlStore(tmp_path / "control.sqlite3", clock=lambda: NOW / 1_000)
    store.initialize()
    store.register_repository(
        repo_key="a" * 64,
        repository_id="repo",
        display_name="fixture",
        git_common_dir=str(tmp_path / ".git"),
        main_worktree_path=str(tmp_path),
        target_ref="refs/heads/main",
        now=OLD,
    )
    return store


def _task(store: ControlStore, name: str = "old", state: str = "completed") -> None:
    store.create_task(repository_id="repo", task_id=name, title="inspectable", now=OLD)
    with store.connection() as connection:
        connection.execute(
            "UPDATE tasks SET state=?,finished_at=?,updated_at=? WHERE task_id=?",
            (state, OLD, OLD, name),
        )
    for kind in (
        "model.text.delta",
        "model.reasoning.delta",
        "model.tool.delta",
        "check.output",
        "command.output",
        "model.finished",
        "execution.published",
    ):
        store.record_task_event(
            task_id=name,
            claim_id=None,
            event_type=kind,
            payload={"answer": "complete answer", "summary": "saved summary"},
            now=OLD,
        )


def _workflow(store: ControlStore, task_id: str = "old") -> None:
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO task_workflows(task_id,attempt,status,created_at) "
            "VALUES (?,1,'published',?)",
            (task_id, OLD),
        )
        connection.execute(
            "INSERT INTO check_runs(run_id,task_id,attempt,name,state,candidate_digest,"
            "source_digest,config_digest,output,exit_code,created_at) "
            "VALUES (?, ?,1,'pytest','passed','candidate','source','config',?,0,?)",
            (f"check-{task_id}", task_id, "long check output" * 1000, OLD),
        )


def test_preview_apply_keeps_task_answers_summaries_and_check_metadata(
    store: ControlStore,
) -> None:
    _task(store)
    _workflow(store)
    _task(store, "fresh")
    _task(store, "running", "running")
    _task(store, "review", "reviewing")
    with store.connection() as connection:
        connection.execute(
            "UPDATE tasks SET finished_at=?,updated_at=? WHERE task_id='fresh'",
            (NOW, NOW),
        )
    before = store.list_task_events("old")
    preview = prune_task_output(store, older_than="30d")
    assert preview["dry_run"] is True
    assert preview["event_chunks"] == 5
    assert preview["check_outputs"] == 1
    assert preview["vacuumed"] is False
    assert store.list_task_events("old") == before
    applied = prune_task_output(store, older_than="30d", apply=True)
    assert applied["vacuumed"] is True
    assert store.get_task("old").title == "inspectable"  # type: ignore[union-attr]
    events = store.list_task_events("old")
    assert [event.event_type for event in events] == [
        "task.created",
        "model.finished",
        "execution.published",
        "task.output_pruned",
    ]
    assert events[1].payload == {
        "answer": "complete answer",
        "summary": "saved summary",
    }
    assert events[1].sequence == before[6].sequence
    for name in ("fresh", "running", "review"):
        assert len(store.list_task_events(name)) == 8
    with store.connection() as connection:
        row = connection.execute(
            "SELECT output,state,exit_code,truncated FROM check_runs"
        ).fetchone()
        assert tuple(row) == ("", "passed", 0, 1)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    repeat = prune_task_output(store, older_than="30d", apply=True)
    assert repeat["event_chunks"] == repeat["check_outputs"] == 0
    assert store.list_task_events("old") == events


@pytest.mark.parametrize(
    ("status", "proposal", "protected"),
    [
        ("awaiting_review", None, True),
        ("awaiting_review", "[]", True),
        ("cancelled", "[]", True),
        ("failed", "[]", True),
        ("running", "[]", True),
        # A task that stopped without a held proposal has nothing to decide.
        ("running", None, False),
        ("failed", None, False),
        ("cancelled", None, False),
        # A published proposal kept for undo needs no output or checkpoint.
        ("published", "[]", False),
        ("undone", "[]", False),
    ],
)
def test_held_proposals_are_protected_and_settled_workflows_prune(
    store: ControlStore, status: str, proposal: str | None, protected: bool
) -> None:
    _task(store)
    _workflow(store)
    with store.connection() as connection:
        connection.execute(
            "UPDATE task_workflows SET status=?,proposal_json=?", (status, proposal)
        )
    result = prune_task_output(store, older_than="30d", apply=True)
    assert result["event_chunks"] == (0 if protected else 5)
    assert len(store.list_task_events("old")) == (8 if protected else 4)
    with store.connection() as connection:
        assert tuple(
            connection.execute(
                "SELECT status,proposal_json FROM task_workflows"
            ).fetchone()
        ) == (status, proposal)


def test_uncertain_checks_are_protected(store: ControlStore) -> None:
    _task(store)
    _workflow(store)
    with store.connection() as connection:
        connection.execute("UPDATE check_runs SET state='uncertain'")
    assert prune_task_output(store, older_than="30d", apply=True)["check_outputs"] == 0


@pytest.mark.parametrize(
    "phase", ["awaiting_settlement", "finalization_pending", "finalizing", "broken"]
)
def test_unfinished_checkpoint_is_protected(store: ControlStore, phase: str) -> None:
    store.create_task(repository_id="repo", task_id="old", now=OLD)
    coordinator = RepositoryCoordinator(store)
    claim = coordinator.request_claim("old", ["README.md"], now=OLD)
    execution = store.create_execution(
        task_id="old",
        attempt=1,
        claim_id=claim.claim_id,
        driver="coding-agent",
        worktree_path="/fixture",
        base_oid="a" * 40,
        boot_id="dead",
        now=OLD,
    )
    coordinator.release_claim(
        claim.claim_id, reason="fixture", task_state="failed", now=OLD
    )
    with store.connection() as connection:
        connection.execute("UPDATE task_executions SET state='published'")
        connection.execute(
            "INSERT INTO execution_checkpoints(execution_id,driver,checkpoint_json,"
            "created_at,updated_at)"
            " VALUES (?, 'coding-agent', ?, ?, ?)",
            (execution.execution_id, json.dumps({"phase": phase}), OLD, OLD),
        )
    store.record_task_event(
        task_id="old",
        claim_id=None,
        event_type="model.text.delta",
        payload={"text": "saved"},
        now=OLD,
    )
    assert prune_task_output(store, older_than="30d", apply=True)["event_chunks"] == 0


@pytest.mark.parametrize("value", ["0d", "-1d", "30", "1h", "1000000d", "x", ""])
def test_invalid_age_never_changes_database(store: ControlStore, value: str) -> None:
    _task(store)
    before = store.list_task_events("old")
    with pytest.raises(ValueError, match="positive number of days"):
        prune_task_output(store, older_than=value, apply=True)
    assert store.list_task_events("old") == before
    assert retention_age_ms("30d") == 30 * DAY


def test_actual_multipart_previews_and_output_prune_but_full_answer_survives(
    store: ControlStore,
) -> None:
    _task(store)

    class Broker:
        def emit(self, kind: str, payload: dict[str, object]) -> None:
            store.record_task_event(
                task_id="old",
                claim_id=None,
                event_type=kind,
                payload=payload,
                now=OLD,
            )

    broker: Any = Broker()
    live = _LiveEvents(broker)
    live("model.text.delta", {"text": "provisional" * 10_000})
    live.flush()
    answer = "complete answer" * 10_000
    _emit_transcript(
        broker, "model.finished", {"answer": answer, "summary": "final summary"}
    )
    for kind in ("command.output", "check.output"):
        _emit_transcript(broker, kind, {"text": "verbose" * 10_000})
    before = store.list_task_events("old", limit=500)
    previews = [e for e in before if e.event_type == "model.answer.preview"]
    assert len(previews) > 1 and all("part" in e.payload for e in previews)
    finished = [
        e for e in before if e.event_type == "model.finished" and "part" in e.payload
    ]
    assert len(finished) > 1
    assert "".join(str(e.payload["answer"]) for e in finished) == answer
    result = prune_task_output(store, older_than="30d", apply=True)
    assert result["event_chunks"] > 5
    after = store.list_task_events("old", limit=500)
    assert not any(
        e.event_type in {"model.answer.preview", "command.output", "check.output"}
        for e in after
    )
    assert [
        e for e in after if e.event_type == "model.finished" and "part" in e.payload
    ] == finished
    assert all(e.payload["summary"] == "final summary" for e in finished)


def _execution(store: ControlStore, state: str, checkpoint: dict[str, Any]) -> str:
    store.create_task(repository_id="repo", task_id="old", now=OLD)
    coordinator = RepositoryCoordinator(store)
    claim = coordinator.request_claim("old", ["README.md"], now=OLD)
    execution = store.create_execution(
        task_id="old",
        attempt=1,
        claim_id=claim.claim_id,
        driver="coding-agent",
        worktree_path="/fixture",
        base_oid="a" * 40,
        boot_id="dead",
        now=OLD,
    )
    coordinator.release_claim(
        claim.claim_id, reason="fixture", task_state="failed", now=OLD
    )
    with store.connection() as connection:
        connection.execute("UPDATE task_executions SET state=?", (state,))
        connection.execute(
            "UPDATE tasks SET state=?,finished_at=?,updated_at=?",
            ("completed" if state == "published" else "failed", OLD, OLD),
        )
        connection.execute(
            "INSERT INTO execution_checkpoints(execution_id,driver,checkpoint_json,"
            "created_at,updated_at) VALUES (?, 'coding-agent', ?, ?, ?)",
            (execution.execution_id, json.dumps(checkpoint), OLD, OLD),
        )
    store.record_task_event(
        task_id="old",
        claim_id=None,
        event_type="model.tool_result",
        payload={"call_id": "c1", "tool": "read_file", "content": "x" * 10_000},
        now=OLD,
    )
    return execution.execution_id


def _checkpoint(store: ControlStore, execution_id: str) -> dict[str, Any]:
    with store.connection() as connection:
        row = connection.execute(
            "SELECT checkpoint_json FROM execution_checkpoints WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
    return dict(json.loads(row[0]))


_USAGE = {
    "calls": 12,
    "bytes_read": 4096,
    "shared_workspace_state": {"files": [{"path": "a.py", "content": "x" * 50_000}]},
}


def test_settled_checkpoints_drop_saved_conversation_and_published_files(
    store: ControlStore,
) -> None:
    finished = {
        "version": 1,
        "phase": "finished",
        "provider": "anthropic",
        "model": "model",
        "session": {"input": ["conversation" * 10_000]},
        "tool_usage": _USAGE,
        "usage_total": {"prompt_tokens": 10, "output_tokens": 2},
        "accepted_answer": {"answer": "complete answer"},
        "finalization": None,
    }
    execution_id = _execution(store, "published", finished)
    preview = prune_task_output(store, older_than="30d")
    assert preview["checkpoints"] == 1
    assert preview["checkpoint_bytes"] > 150_000
    assert preview["event_chunks"] == 1
    assert _checkpoint(store, execution_id) == finished
    applied = prune_task_output(store, older_than="30d", apply=True)
    assert applied["checkpoints"] == 1
    slim = _checkpoint(store, execution_id)
    assert slim == {
        key: value for key, value in finished.items() if key != "session"
    } | {"tool_usage": {"calls": 12, "bytes_read": 4096}}
    assert [e.event_type for e in store.list_task_events("old")][-1] == (
        "task.output_pruned"
    )
    assert store.list_task_events("old")[-1].payload["checkpoints"] == 1
    assert not any(
        e.event_type == "model.tool_result" for e in store.list_task_events("old")
    )
    repeat = prune_task_output(store, older_than="30d", apply=True)
    assert repeat["checkpoints"] == repeat["event_chunks"] == 0


def test_failed_turn_keeps_pending_files_but_not_its_conversation(
    store: ControlStore,
) -> None:
    interrupted = {
        "version": 1,
        "phase": "tool_results",
        "session": {"input": ["conversation" * 1_000]},
        "tool_usage": _USAGE,
    }
    execution_id = _execution(store, "failed", interrupted)
    assert prune_task_output(store, older_than="30d", apply=True)["checkpoints"] == 1
    assert _checkpoint(store, execution_id) == {
        "version": 1,
        "phase": "tool_results",
        "tool_usage": _USAGE,
    }


def test_interrupted_answer_keeps_its_conversation_for_retry(
    store: ControlStore,
) -> None:
    from llm_cli.agent.finalization import FinalizationState, SettlementFacts

    facts = SettlementFacts(
        publication="published", verification="passed", completion="completed"
    )
    checkpoint = {
        "version": 1,
        "phase": "finished",
        "session": {"input": ["conversation"]},
        "tool_usage": {"calls": 1},
        "accepted_answer": {"answer": "facts only"},
        "finalization": FinalizationState(
            status="interrupted", attempts=1, facts=facts
        ).to_dict(),
    }
    execution_id = _execution(store, "published", checkpoint)
    result = prune_task_output(store, older_than="30d", apply=True)
    assert result["eligible_tasks"] == 1 and result["checkpoints"] == 0
    assert _checkpoint(store, execution_id) == checkpoint


@pytest.mark.parametrize(
    ("batch", "protected"), [("diverged", False), ("applying", True)]
)
def test_refused_shared_batches_settle_but_applying_ones_are_protected(
    store: ControlStore, batch: str, protected: bool
) -> None:
    execution_id = _execution(
        store, "failed", {"version": 1, "phase": "finished", "finalization": None}
    )
    with store.connection() as connection:
        # Only the batch state matters here; its owners are not under test.
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO workspace_batches(batch_id,session_id,workspace_id,"
            "checkout_id,canonical_path,git_common_dir,request_json,state,"
            "created_at,updated_at) VALUES (?,'s','w','c','/r','/r/.git','{}',?,?,?)",
            (execution_id, batch, OLD, OLD),
        )
    result = prune_task_output(store, older_than="30d")
    assert result["eligible_tasks"] == (0 if protected else 1)
