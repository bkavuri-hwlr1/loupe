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


@pytest.mark.parametrize("state", ["awaiting_review", "running", "failed"])
def test_workflow_not_settled_is_protected(store: ControlStore, state: str) -> None:
    _task(store)
    _workflow(store)
    with store.connection() as connection:
        connection.execute("UPDATE task_workflows SET status=?", (state,))
    assert prune_task_output(store, older_than="30d", apply=True)["event_chunks"] == 0
    assert len(store.list_task_events("old")) == 8


def test_retained_proposal_and_uncertain_checks_are_protected(
    store: ControlStore,
) -> None:
    _task(store)
    _workflow(store)
    with store.connection() as connection:
        connection.execute("UPDATE task_workflows SET proposal_json='[]'")
    assert prune_task_output(store, older_than="30d", apply=True)["event_chunks"] == 0
    with store.connection() as connection:
        connection.execute("UPDATE task_workflows SET proposal_json=NULL")
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
