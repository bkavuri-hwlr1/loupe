"""A final response consumes one durable, tools-disabled settlement attempt."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from test_agent_loop import (
    ScriptedProvider,
    _broker,
    _call,
    _request,
    _tools,
)
from test_agent_loop import worktree as worktree

from llm_cli.agent.driver import FinalizationRequest, RunResult
from llm_cli.agent.finalization import FinalizationState, SettlementFacts
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.providers.base import ModelTurn


def _prepare_draft(
    worktree: Path,
    *,
    draft: str = "Provisional answer before settlement.",
    final_turn: ModelTurn | None = None,
    clock: Callable[[], float] | None = None,
    content: str = "new\n",
) -> tuple[
    CodingAgentHarness,
    ScriptedProvider,
    RunResult,
    list[Mapping[str, object]],
    Callable[[Mapping[str, object]], bool],
]:
    turns = [
        _tools(_call("write_file", path="docs/guide.md", content=content)),
        _tools(_call("validate_changes")),
        _tools(_call("finish_task", answer=draft, summary="Updated the guide.")),
    ]
    if final_turn is not None:
        turns.append(final_turn)
    provider = ScriptedProvider(turns)
    snapshots: list[Mapping[str, object]] = []

    def persist(state: Mapping[str, object]) -> bool:
        snapshots.append(copy.deepcopy(state))
        return True

    harness = CodingAgentHarness(provider, **({"clock": clock} if clock else {}))
    request = replace(
        _request(worktree),
        workspace_mode="shared",
        publication_aware_finalization=True,
        checkpoint=persist,
    )
    result = harness.run(request, _broker(worktree, events=[]))
    prepared = snapshots[-1]
    assert prepared["phase"] == "awaiting_settlement"
    assert prepared["accepted_answer"] is None
    assert prepared["finalization"] == FinalizationState("prepared", 0).to_dict()
    return harness, provider, result, snapshots, persist


def _request_finalization(
    result: RunResult,
    saved: Mapping[str, object],
    persist: Callable[[Mapping[str, object]], bool],
    facts: SettlementFacts,
    *,
    use_model: bool = True,
    cancelled: Callable[[], bool] | None = None,
) -> FinalizationRequest:
    return FinalizationRequest(
        task_id="task-agent",
        attempt=1,
        instructions="update the guide",
        draft=result.answer,
        summary=result.summary,
        facts=facts,
        resume_state=saved,
        checkpoint=persist,
        use_model=use_model,
        cancelled=cancelled,
    )


def test_settled_mutation_gets_one_tools_disabled_final_turn(worktree: Path) -> None:
    facts = SettlementFacts(
        publication="published",
        verification="passed",
        completion="completed",
        changed_paths=("docs/guide.md",),
    )
    harness, provider, draft, snapshots, persist = _prepare_draft(
        worktree,
        final_turn=ModelTurn(
            text="Updated the guide, published it, and verified the result.",
            usage={"input_tokens": 11, "output_tokens": 12},
            context_tokens=23,
        ),
    )
    request = _request_finalization(draft, snapshots[-1], persist, facts)

    pending = harness.prepare_finalization(request)
    result = harness.finalize(replace(request, resume_state=pending))

    assert result.answer == (
        "Updated the guide, published it, and verified the result."
    )
    assert result.outcome == "completed"
    assert provider.offered_tools == ()
    assert provider.restored_state == pending["session"]
    assert len(provider.user_messages) == 2
    assert '"publication": "published"' in provider.user_messages[-1]
    finished = snapshots[-1]
    assert finished["phase"] == "finished"
    # The next task inherits the size including the settled reply.
    assert finished["context_tokens"] == 23
    assert finished["finalization"] == FinalizationState(
        "completed", 1, facts
    ).to_dict()
    accepted = finished["accepted_answer"]
    assert isinstance(accepted, Mapping)
    assert accepted["text"] == result.answer


def test_trusted_no_change_accepts_draft_without_another_model_call(
    worktree: Path,
) -> None:
    original = (worktree / "docs/guide.md").read_text()
    harness, provider, draft, snapshots, persist = _prepare_draft(
        worktree,
        content=original,
    )
    facts = SettlementFacts(
        publication="no_changes",
        verification="not_applicable",
        completion="completed",
    )
    before = len(provider.user_messages)
    request = _request_finalization(
        draft,
        snapshots[-1],
        persist,
        facts,
        use_model=False,
    )

    result = harness.finalize(request)

    assert result.answer == draft.answer
    assert result.outcome == "completed"
    assert len(provider.user_messages) == before
    assert snapshots[-1]["finalization"] == FinalizationState(
        "completed", 0, facts
    ).to_dict()


def test_in_flight_recovery_never_reissues_the_provider_request(
    worktree: Path,
) -> None:
    unsafe_draft = "Provisional claim: these changes were published."
    facts = SettlementFacts(
        publication="held_for_review",
        verification="failed",
        completion="completed",
        changed_paths=("docs/guide.md",),
    )
    harness, provider, draft, snapshots, persist = _prepare_draft(
        worktree,
        draft=unsafe_draft,
        final_turn=ModelTurn(text="must not be requested"),
    )
    request = _request_finalization(draft, snapshots[-1], persist, facts)
    pending = harness.prepare_finalization(request)
    in_flight = {
        **pending,
        "phase": "finalizing",
        "finalization": FinalizationState("in_flight", 1, facts).to_dict(),
    }
    persist(in_flight)
    before = len(provider.user_messages)

    result = harness.finalize(replace(request, resume_state=in_flight))

    assert result.outcome == "partial"
    assert len(provider.user_messages) == before
    assert unsafe_draft not in result.answer
    assert "retained for review" in result.answer
    assert "Required verification failed" in result.answer
    assert snapshots[-1]["finalization"] == FinalizationState(
        "interrupted", 1, facts
    ).to_dict()


def test_cancellation_after_settlement_consumes_attempt_without_dispatch(
    worktree: Path,
) -> None:
    facts = SettlementFacts(
        publication="held_for_review",
        verification="interrupted",
        completion="cancelled",
        changed_paths=("docs/guide.md",),
    )
    harness, provider, draft, snapshots, persist = _prepare_draft(worktree)
    request = _request_finalization(draft, snapshots[-1], persist, facts)
    pending = harness.prepare_finalization(request)
    before = len(provider.user_messages)

    result = harness.finalize(
        replace(request, resume_state=pending, cancelled=lambda: True)
    )

    assert result.outcome == "partial"
    assert len(provider.user_messages) == before
    assert "The task was cancelled" in result.answer
    states = [
        FinalizationState.from_mapping(state["finalization"]).status
        for state in snapshots
        if isinstance(state.get("finalization"), Mapping)
    ]
    assert states[-3:] == ["pending", "in_flight", "interrupted"]


def test_expired_absolute_deadline_prevents_final_response_dispatch(
    worktree: Path,
) -> None:
    now = [100.0]
    facts = SettlementFacts(
        publication="published",
        verification="passed",
        completion="completed",
        changed_paths=("docs/guide.md",),
    )
    harness, provider, draft, snapshots, persist = _prepare_draft(
        worktree,
        clock=lambda: now[0],
    )
    request = _request_finalization(draft, snapshots[-1], persist, facts)
    pending = harness.prepare_finalization(request)
    now[0] = float(pending["deadline_at"]) + 1.0
    before = len(provider.user_messages)

    result = harness.finalize(replace(request, resume_state=pending))

    assert result.outcome == "partial"
    assert len(provider.user_messages) == before
    assert "published to the shared checkout" in result.answer
    assert snapshots[-1]["finalization"] == FinalizationState(
        "interrupted", 1, facts
    ).to_dict()
