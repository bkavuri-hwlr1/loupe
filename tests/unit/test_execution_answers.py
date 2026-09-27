"""An accepted final answer and its replay events form one durable commit."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pytest

from llm_cli.agent.finalization import FinalizationState, SettlementFacts
from llm_cli.agent.limits import MAX_ANSWER_CHARACTERS, MAX_TASK_SUMMARY_CHARACTERS
from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import ExecutionRecord, TaskEventRecord
from llm_cli.protocol.envelopes import Response
from llm_cli.protocol.framing import encode_frame
from llm_cli.storage.connection import immediate_transaction
from llm_cli.storage.control import ControlStore

_NOW = 1_750_000_000_000


@pytest.fixture
def execution_store(tmp_path: Path) -> tuple[ControlStore, ExecutionRecord]:
    store = ControlStore(tmp_path / "control.sqlite3")
    store.initialize()
    store.register_repository(
        repository_id="repo_answer",
        repo_key="a" * 64,
        display_name="fixture",
        git_common_dir=str(tmp_path / "repo" / ".git"),
        main_worktree_path=str(tmp_path / "repo"),
        target_ref="refs/heads/main",
        now=_NOW,
    )
    task = store.create_task(
        repository_id="repo_answer", task_id="task_answer", now=_NOW
    )
    claim = RepositoryCoordinator(store).request_claim(task.task_id, ["*"], now=_NOW)
    execution = store.create_execution(
        task_id=task.task_id,
        attempt=1,
        claim_id=claim.claim_id,
        driver="coding_agent",
        worktree_path=str(tmp_path / "repo"),
        base_oid="a" * 40,
        boot_id="boot-answer",
        now=_NOW,
    )
    return store, execution


def _finished(
    text: str = "The repository coordinates local coding agents.",
) -> dict[str, object]:
    return {
        "phase": "finished",
        "accepted_answer": {
            "version": 1,
            "answer_id": "task_answer:1:answer",
            "text": text,
            "summary": "Answered the repository question.",
            "outcome": "completed",
        },
        "tool_usage": {"calls": 3, "finished": True},
        "usage_total": {"input_tokens": 42, "output_tokens": 100},
    }


def _save(
    store: ControlStore, execution: ExecutionRecord, state: Mapping[str, object]
) -> None:
    saved = store.save_execution_checkpoint(
        execution_id=execution.execution_id,
        driver=execution.driver,
        checkpoint=state,
        now=_NOW,
    )
    assert saved.terminal_event_persisted


def _answer_events(store: ControlStore, task_id: str) -> tuple[TaskEventRecord, ...]:
    return tuple(
        event
        for event in store.list_task_events(task_id)
        if event.event_type == "model.finished"
    )


def test_full_answer_survives_storage_and_event_chunks(
    execution_store: tuple[ControlStore, ExecutionRecord],
) -> None:
    store, execution = execution_store
    answer = "Overview 🦊\n" * 2_000 + "The final sentence survives."
    summary = "Metadata is retained too.\n" * 40
    state = _finished(answer)
    assert isinstance(state["accepted_answer"], dict)
    state["accepted_answer"]["summary"] = summary
    _save(store, execution, state)
    stored = store.get_execution_answer(execution.execution_id)
    assert stored is not None
    assert stored.payload["answer"] == answer
    assert stored.payload["summary"] == summary
    assert stored.version == 1
    assert stored.payload["message_id"] == "task_answer:1:answer"
    events = _answer_events(store, execution.task_id)
    assert len(events) > 1
    assert "".join(str(event.payload["answer"]) for event in events) == answer
    assert all(event.payload["summary"] == summary for event in events)
    assert [event.payload["part"] for event in events] == list(range(len(events)))
    assert sum(bool(event.payload["final"]) for event in events) == 1
    assert stored.first_event_sequence == events[0].sequence
    assert stored.last_event_sequence == events[-1].sequence
    for event in events:
        encode_frame(
            Response(request_id="answer", ok=True, result=asdict(event)).to_dict()
        )
    # The separate execution metadata also remains complete.
    long_metadata = summary * 10
    updated = store.update_execution(
        execution.execution_id, state="validated", summary=long_metadata
    )
    assert updated.summary == long_metadata


def test_finished_checkpoint_retry_after_restart_has_one_answer_projection(
    execution_store: tuple[ControlStore, ExecutionRecord],
) -> None:
    store, execution = execution_store
    state = _finished()
    _save(store, execution, state)
    original = _answer_events(store, execution.task_id)
    restarted = ControlStore(store.path)
    saved = restarted.get_execution_checkpoint(execution.execution_id)
    assert saved is not None and saved.terminal_event_persisted
    _save(restarted, execution, saved.checkpoint)
    assert _answer_events(restarted, execution.task_id) == original
    assert len(original) == 1
    assert not restarted.list_task_events(
        execution.task_id, after_sequence=original[-1].sequence
    )


def test_concurrent_checkpoint_retries_do_not_duplicate_the_accepted_answer(
    execution_store: tuple[ControlStore, ExecutionRecord],
) -> None:
    store, execution = execution_store
    state = _finished()
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(_save, store, execution, state) for _ in range(8)]
        for future in futures:
            future.result(timeout=5)
    assert len(_answer_events(store, execution.task_id)) == 1


def test_failed_event_projection_rolls_back_answer_and_finished_checkpoint(
    execution_store: tuple[ControlStore, ExecutionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, execution = execution_store
    preceding = store.save_execution_checkpoint(
        execution_id=execution.execution_id,
        driver=execution.driver,
        checkpoint={"phase": "turn"},
    )
    assert not preceding.terminal_event_persisted
    append = ControlStore.append_task_event

    def fail_second_part(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        claim_id: str | None,
        event_type: str,
        payload: Mapping[str, object],
        now: int,
    ) -> str:
        if event_type == "model.finished" and payload.get("part") == 1:
            raise sqlite3.OperationalError("injected event persistence failure")
        return append(
            connection,
            task_id=task_id,
            claim_id=claim_id,
            event_type=event_type,
            payload=payload,
            now=now,
        )

    with monkeypatch.context() as patch:
        patch.setattr(ControlStore, "append_task_event", staticmethod(fail_second_part))
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            _save(store, execution, _finished("Full answer.\n" * 1_000))
    assert store.get_execution_checkpoint(execution.execution_id) == preceding
    assert store.get_execution_answer(execution.execution_id) is None
    assert not _answer_events(store, execution.task_id)
    _save(store, execution, _finished("Full answer.\n" * 1_000))
    assert store.get_execution_answer(execution.execution_id) is not None


@pytest.mark.parametrize("change", ["replace", "discard", "rewind"])
def test_accepted_answer_cannot_be_replaced_or_retracted(
    execution_store: tuple[ControlStore, ExecutionRecord], change: str
) -> None:
    store, execution = execution_store
    _save(store, execution, _finished())
    preceding = store.get_execution_checkpoint(execution.execution_id)
    events = _answer_events(store, execution.task_id)
    changed = _finished("A replacement answer.")
    if change == "discard":
        changed.pop("accepted_answer")
    if change == "rewind":
        changed["phase"] = "turn"
    with pytest.raises(ValueError):
        _save(store, execution, changed)
    assert store.get_execution_checkpoint(execution.execution_id) == preceding
    assert _answer_events(store, execution.task_id) == events


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", " "),
        ("version", True),
        ("version", 2),
        ("outcome", []),
        ("answer_id", ""),
        ("text", "a" * (MAX_ANSWER_CHARACTERS + 1)),
        ("summary", "a" * (MAX_TASK_SUMMARY_CHARACTERS + 1)),
    ],
)
def test_invalid_answer_is_rejected_without_partial_records(
    execution_store: tuple[ControlStore, ExecutionRecord], field: str, value: object
) -> None:
    store, execution = execution_store
    state = _finished()
    assert isinstance(state["accepted_answer"], dict)
    state["accepted_answer"][field] = value
    with pytest.raises(ValueError):
        _save(store, execution, state)
    assert store.get_execution_checkpoint(execution.execution_id) is None
    assert store.get_execution_answer(execution.execution_id) is None
    assert not _answer_events(store, execution.task_id)


def test_legacy_checkpoint_has_no_invented_answer_or_terminal_event(
    execution_store: tuple[ControlStore, ExecutionRecord],
) -> None:
    store, execution = execution_store
    legacy = {"phase": "finished", "final_summary": "Old task summary."}
    saved = store.save_execution_checkpoint(
        execution_id=execution.execution_id,
        driver=execution.driver,
        checkpoint=legacy,
    )
    assert not saved.terminal_event_persisted
    assert saved.checkpoint == legacy
    assert store.get_execution_answer(execution.execution_id) is None
    assert not _answer_events(store, execution.task_id)


def _pending_finalization() -> dict[str, object]:
    return {
        "phase": "awaiting_settlement",
        "accepted_answer": None,
        "finalization": FinalizationState("prepared", 0).to_dict(),
        "tool_usage": {"calls": 3, "finished": True},
        "usage_total": {"input_tokens": 42, "output_tokens": 100},
    }


def _settlement() -> SettlementFacts:
    return SettlementFacts(
        publication="published",
        verification="passed",
        completion="completed",
        changed_paths=("src/app.py",),
    )


def test_finalization_attempt_is_consumed_before_dispatch_and_cannot_reset(
    execution_store: tuple[ControlStore, ExecutionRecord],
) -> None:
    store, execution = execution_store
    pending = _pending_finalization()
    store.save_execution_checkpoint(
        execution_id=execution.execution_id,
        driver=execution.driver,
        checkpoint=pending,
    )
    in_flight = {
        **pending,
        "phase": "finalization_pending",
        "finalization": FinalizationState("pending", 0, _settlement()).to_dict(),
    }
    store.save_execution_checkpoint(
        execution_id=execution.execution_id,
        driver=execution.driver,
        checkpoint=in_flight,
    )
    in_flight = {
        **in_flight,
        "phase": "finalizing",
        "finalization": FinalizationState("in_flight", 1, _settlement()).to_dict(),
    }
    store.save_execution_checkpoint(
        execution_id=execution.execution_id,
        driver=execution.driver,
        checkpoint=in_flight,
    )

    with pytest.raises(ValueError, match=r"move backwards|reset"):
        store.save_execution_checkpoint(
            execution_id=execution.execution_id,
            driver=execution.driver,
            checkpoint=pending,
        )
    saved = store.get_execution_checkpoint(execution.execution_id)
    assert saved is not None
    assert saved.checkpoint == in_flight
    assert store.get_execution_answer(execution.execution_id) is None


def test_successful_finalization_commits_answer_and_terminal_state_together(
    execution_store: tuple[ControlStore, ExecutionRecord],
) -> None:
    store, execution = execution_store
    pending = _pending_finalization()
    in_flight = {
        **pending,
        "phase": "finalization_pending",
        "finalization": FinalizationState("pending", 0, _settlement()).to_dict(),
    }
    store.save_execution_checkpoint(
        execution_id=execution.execution_id,
        driver=execution.driver,
        checkpoint=pending,
    )
    store.save_execution_checkpoint(
        execution_id=execution.execution_id,
        driver=execution.driver,
        checkpoint=in_flight,
    )
    in_flight = {
        **in_flight,
        "phase": "finalizing",
        "finalization": FinalizationState("in_flight", 1, _settlement()).to_dict(),
    }
    store.save_execution_checkpoint(
        execution_id=execution.execution_id,
        driver=execution.driver,
        checkpoint=in_flight,
    )
    finished = _finished("The change was published and its required checks passed.")
    finished["finalization"] = FinalizationState(
        "completed", 1, _settlement()
    ).to_dict()
    _save(store, execution, finished)

    answer = store.get_execution_answer(execution.execution_id)
    checkpoint = store.get_execution_checkpoint(execution.execution_id)
    assert answer is not None
    assert checkpoint is not None and checkpoint.terminal_event_persisted
    assert checkpoint.checkpoint["finalization"] == finished["finalization"]


def test_no_change_settlement_accepts_draft_without_model_attempt(
    execution_store: tuple[ControlStore, ExecutionRecord],
) -> None:
    store, execution = execution_store
    pending = _pending_finalization()
    store.save_execution_checkpoint(
        execution_id=execution.execution_id,
        driver=execution.driver,
        checkpoint=pending,
    )
    facts = SettlementFacts(
        publication="no_changes",
        verification="not_applicable",
        completion="completed",
    )
    finished = _finished("No file content changed; here is the requested answer.")
    finished["finalization"] = FinalizationState("completed", 0, facts).to_dict()
    _save(store, execution, finished)

    saved = store.get_execution_checkpoint(execution.execution_id)
    assert saved is not None
    assert saved.checkpoint["finalization"] == finished["finalization"]


@pytest.mark.parametrize(
    "invalid",
    ["phase", "attempts", "facts"],
)
def test_invalid_pending_finalization_is_rejected_atomically(
    execution_store: tuple[ControlStore, ExecutionRecord],
    invalid: str,
) -> None:
    store, execution = execution_store
    state = _pending_finalization()
    if invalid == "phase":
        state["phase"] = "finished"
    else:
        finalization = state["finalization"]
        assert isinstance(finalization, dict)
        finalization[invalid] = (
            2 if invalid == "attempts" else _settlement().to_dict()
        )
    with pytest.raises(ValueError):
        store.save_execution_checkpoint(
            execution_id=execution.execution_id,
            driver=execution.driver,
            checkpoint=state,
        )
    assert store.get_execution_checkpoint(execution.execution_id) is None


def test_sessionless_execution_does_not_require_a_conversation_to_settle(
    execution_store: tuple[ControlStore, ExecutionRecord],
) -> None:
    store, execution = execution_store
    with store.connection() as connection:
        store.promote_execution_conversation(
            connection, execution.execution_id, now=_NOW
        )
    assert store.get_execution_checkpoint(execution.execution_id) is None


@pytest.mark.parametrize("invalid", [None, "provider", "model", "phase", "missing"])
def test_isolated_session_conversation_requires_matching_finished_checkpoint(
    execution_store: tuple[ControlStore, ExecutionRecord], invalid: str | None
) -> None:
    store, execution = execution_store
    repository = store.get_repository("repo_answer")
    assert repository is not None
    checkout = store.ensure_checkout(
        repository=repository,
        canonical_path=repository.main_worktree_path,
        git_common_dir=repository.git_common_dir,
    )
    workspace = store.ensure_shared_workspace(checkout)
    session, _, _ = store.open_session(
        session_id="session_answer",
        checkout_id=checkout.checkout_id,
        resume_token_hash="b" * 64,
        provider="scripted",
        model="answer-model",
        workspace_id=workspace.workspace_id,
        workspace_mode="isolated",
    )
    with store.connection() as connection, immediate_transaction(connection):
        connection.execute(
            "UPDATE tasks SET session_id = ? WHERE task_id = ?",
            (session.session_id, execution.task_id),
        )
    state: dict[str, object] = {
        "phase": "finished",
        "provider": session.provider,
        "model": session.model,
        "session": {"messages": [{"role": "assistant", "content": "Full answer."}]},
        "coordination_sequence": 3,
    }
    if invalid and invalid != "missing":
        state[invalid] = "different"
    if invalid != "missing":
        store.save_execution_checkpoint(
            execution_id=execution.execution_id,
            driver=execution.driver,
            checkpoint=state,
        )
    with store.connection() as connection, immediate_transaction(connection):
        if invalid:
            with pytest.raises(ValueError, match="matching finished conversation"):
                store.promote_execution_conversation(
                    connection, execution.execution_id, now=_NOW
                )
        else:
            # Shared callers retain their exact workspace identity requirement.
            with pytest.raises(ValueError, match="matching finished conversation"):
                store.promote_shared_conversation(
                    connection, execution.execution_id, now=_NOW
                )
            store.promote_execution_conversation(
                connection, execution.execution_id, now=_NOW
            )
    conversation = store.session_conversation(session.session_id)
    if invalid:
        assert conversation is None
    else:
        assert conversation is not None
        assert conversation[2]["session"] == state["session"]
        assert conversation[2]["coordination_sequence"] == 3
