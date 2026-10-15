"""Independent review observes exact checked proposals before publication."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_shared_agent_execution import (
    ScriptedProvider,
    _assert_completed,
    _call,
    _finish,
    _start,
    _tools,
)
from test_verified_workflow import setup

from llm_cli.agent.driver import RunRequest
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.limits import ExecutionLimits
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import TaskCancelled
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.providers.base import ModelTurn, ToolCallResult


class ReviewProvider(ScriptedProvider):
    def __init__(self, steps: Sequence[Any], report: str | Exception) -> None:
        super().__init__("independent-review", steps)
        self.report = report
        self.reviewer = ScriptedProvider("independent-review-helper", [])
        self.review_sessions = 0

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> ScriptedProvider:
        if system.startswith("You are an independent read-only code reviewer"):
            self.review_sessions += 1
            report = self.report

            def result(_: Sequence[ToolCallResult]) -> ModelTurn:
                if isinstance(report, Exception):
                    raise report
                return ModelTurn(
                    text=report, usage={"prompt_tokens": 11, "output_tokens": 7}
                )

            self.reviewer.steps = [result]
            return self.reviewer.session(system=system, tools=tools, state=state)
        return super().session(system=system, tools=tools, state=state)


def _first_edit() -> ModelTurn:
    return _tools(
        _call("read_file", path="a.py"),
        _call("write_file", path="a.py", content="value = 2\n"),
    )


_FINDING = json.dumps(
    {
        "findings": [
            {"path": "a.py", "line": 1, "message": "Use value 3 for the edge case."}
        ]
    }
)


@pytest.mark.parametrize("natural", [False, True])
def test_findings_are_handed_back_once_then_repaired_checked_and_published(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    natural: bool,
) -> None:
    def repair(results: Sequence[ToolCallResult]) -> ModelTurn:
        assert results[-1].is_error
        assert "a.py:1" in results[-1].content
        assert "only review/repair handback" in results[-1].content
        return _tools(_call("write_file", path="a.py", content="value = 3\n"))

    # Natural-answer handback arrives as a user nudge rather than tool results.
    next_step: Any = (
        _tools(_call("write_file", path="a.py", content="value = 3\n"))
        if natural
        else repair
    )
    provider = ReviewProvider(
        [
            _first_edit(),
            ModelTurn(text="fixed") if natural else _finish(),
            next_step,
            _finish(),
        ],
        _FINDING,
    )

    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        service.settings = replace(service.settings, agent_review="on")
        credentials = await setup(
            service,
            root,
            provider,
            code="print('matching evidence'); assert 'value = 1' not in "
            "__import__('pathlib').Path('a.py').read_text()",
        )
        await asyncio.wait_for(
            await _start(service, root, credentials, "task", ("a.py",)), 30
        )
        _assert_completed(service, "task")
        assert (root / "a.py").read_text() == "value = 3\n"
        assert provider.review_sessions == 1
        data = json.loads(str(provider.reviewer.history[0]["content"]))["proposal"]
        assert "-value = 1" in data["diff"] and "+value = 2" in data["diff"]
        assert data["line_counts"] == {"a.py": 1}
        assert data["files"][0]["original"] == "value = 1\n"
        assert data["files"][0]["candidate"] == "value = 2\n"
        assert data["verification"] == "passed"
        assert [check["state"] for check in data["checks"]] == ["passed"]
        assert "matching evidence" in data["checks"][0]["output"]
        assert [check["state"] for check in service.workflow.checks("task")] == [
            "passed",
            "passed",
        ]
        execution = service.store.get_execution("task", 1)
        assert execution is not None
        assert execution.usage["review_prompt_tokens"] == 11
        assert execution.usage["review_output_tokens"] == 7
        assert set(provider.reviewer.offered_tools) == {
            "list_files",
            "read_file",
            "search_text",
            "read_diff",
        }
        if natural:
            assert any(
                "only review/repair handback" in str(h) for h in provider.history
            )
        service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("setting", "report", "state", "sessions"),
    [
        ("on", '{"findings": []}', "completed", 1),
        ("off", _FINDING, None, 0),
        (
            "on",
            LlmCoordError(ErrorCode.PROVIDER_UNAVAILABLE, "private failure"),
            "failed",
            1,
        ),
        ("on", RuntimeError("private failure"), "failed", 1),
        ("on", "not valid JSON", "failed", 1),
        (
            "on",
            '{"findings": [{"path":"../outside", "line":1,"message":"bad"}]}',
            "failed",
            1,
        ),
    ],
)
def test_clean_failed_and_disabled_reviews_preserve_publication_rules(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    setting: str,
    report: str | Exception,
    state: str | None,
    sessions: int,
) -> None:
    provider = ReviewProvider([_first_edit(), _finish()], report)

    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        service.settings = replace(service.settings, agent_review=setting)
        credentials = await setup(service, root, provider, code="print('passed')")
        await asyncio.wait_for(
            await _start(service, root, credentials, "task", ("a.py",)), 30
        )
        _assert_completed(service, "task")
        assert (root / "a.py").read_text() == "value = 2\n"
        assert provider.review_sessions == sessions
        events = [
            e.payload
            for e in service.store.list_task_events("task")
            if e.event_type == "review.finished"
        ]
        assert [event["state"] for event in events] == ([state] if state else [])
        assert "private failure" not in str(events)
        service.close()

    asyncio.run(scenario())


def test_failed_checks_do_not_start_review_or_publish(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    provider = ReviewProvider(
        [
            _first_edit(),
            _finish(),
            _tools(_call("finish_task", answer="Checks fail", outcome="partial")),
        ],
        '{"findings": []}',
    )

    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        service.settings = replace(service.settings, agent_review="on")
        credentials = await setup(
            service, root, provider, code="raise ValueError('failed')"
        )
        await asyncio.wait_for(
            await _start(service, root, credentials, "task", ("a.py",)), 30
        )
        assert (root / "a.py").read_text() == "value = 1\n"
        assert service.workflow.get("task")["status"] == "awaiting_review"
        assert provider.review_sessions == 0
        service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("crash_status", ["started", "findings"])
def test_review_attempt_and_undelivered_findings_survive_restart(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    crash_status: str,
) -> None:
    class Crash(BaseException):
        pass

    def repair(results: Sequence[ToolCallResult]) -> ModelTurn:
        if crash_status == "findings":
            assert "a.py:1" in results[-1].content
        return _tools(_call("write_file", path="a.py", content="value = 3\n"))

    provider = ReviewProvider([_first_edit(), _finish(), repair, _finish()], _FINDING)
    root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
    saved: dict[str, Any] = {}

    def checkpoint(state: Mapping[str, object]) -> None:
        saved.clear()
        saved.update(copy.deepcopy(dict(state)))
        if saved.get("review", {}).get("status") == crash_status:
            raise Crash

    def context() -> Mapping[str, object]:
        return {"line_counts": {"a.py": 1}, "diff": "+value = 2\n", "checks": []}

    request = RunRequest(
        task_id="task",
        attempt=1,
        instructions="fix value",
        scopes=("a.py",),
        worktree=root,
        base_oid="base",
        workspace_mode="shared",
        review_context=context,
        checkpoint=checkpoint,
    )
    with pytest.raises(Crash):
        CodingAgentHarness(provider, review="on").run(
            request, SharedToolBroker(worktree=root, scopes=("a.py",))
        )
    before = provider.review_sessions
    resumed = SharedToolBroker(worktree=root, scopes=("a.py",))
    result = CodingAgentHarness(provider, review="on").run(
        replace(request, resume_state=saved, checkpoint=None),
        resumed,
    )
    assert result.outcome == "completed"
    assert resumed.candidates()[0].content == b"value = 3\n"
    assert provider.review_sessions == before


@pytest.mark.parametrize("mode", ["auto", "normal", "plan"])
def test_tasks_without_edits_do_not_run_review(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    mode: str,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "safe\n"})
    provider = ReviewProvider([ModelTurn(text="The requested explanation.")], _FINDING)
    result = CodingAgentHarness(provider, review="on").run(
        RunRequest(
            task_id="task",
            attempt=1,
            instructions="explain",
            scopes=("a.py",),
            worktree=root,
            base_oid="base",
            workspace_mode="shared",
            agent_mode=mode,
            review_context=lambda: (_ for _ in ()).throw(AssertionError("unexpected")),
        ),
        SharedToolBroker(worktree=root, scopes=("a.py",), agent_mode=mode),
    )
    assert result.outcome == "completed"
    assert provider.review_sessions == 0


def test_cancel_after_review_response_retains_usage_without_publication(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
    provider = ReviewProvider([_first_edit(), _finish()], '{"findings": []}')
    stopped = False
    saved: list[Mapping[str, object]] = []

    def cancel_after_turn(_: Sequence[ToolCallResult] = ()) -> ModelTurn:
        nonlocal stopped
        stopped = True
        return ModelTurn(text='{"findings": []}', usage={"prompt_tokens": 19})

    monkeypatch.setattr(provider.reviewer, "_next", cancel_after_turn)
    broker = SharedToolBroker(
        worktree=root, scopes=("a.py",), cancelled=lambda: stopped
    )
    with pytest.raises(TaskCancelled):
        CodingAgentHarness(provider, review="on").run(
            RunRequest(
                task_id="task",
                attempt=1,
                instructions="fix",
                scopes=("a.py",),
                worktree=root,
                base_oid="base",
                workspace_mode="shared",
                checkpoint=lambda value: saved.append(copy.deepcopy(dict(value))),
                review_context=lambda: {"diff": "diff", "line_counts": {"a.py": 1}},
            ),
            broker,
        )
    assert saved[-1]["usage_total"]["review_prompt_tokens"] == 19
    assert (root / "a.py").read_text() == "value = 1\n"
    assert broker.candidates()[0].content == b"value = 2\n"


def test_daemon_cancel_preserves_review_usage_on_the_execution_record(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ReviewProvider([_first_edit(), _finish()], '{"findings": []}')

    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        service.settings = replace(service.settings, agent_review="on")

        def cancel_after_turn(_: Sequence[ToolCallResult] = ()) -> ModelTurn:
            service.workflow.update("task", 1, stop_requested=1)
            return ModelTurn(text='{"findings": []}', usage={"prompt_tokens": 19})

        monkeypatch.setattr(provider.reviewer, "_next", cancel_after_turn)
        credentials = await setup(service, root, provider, code="print('passed')")
        await asyncio.wait_for(
            await _start(service, root, credentials, "task", ("a.py",)), 30
        )
        execution = service.store.get_execution("task", 1)
        assert execution is not None
        assert execution.usage["review_prompt_tokens"] == 19
        assert (root / "a.py").read_text() == "value = 1\n"
        service.close()

    asyncio.run(scenario())


def test_review_response_cannot_finish_after_the_task_deadline(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
    provider = ReviewProvider([_first_edit(), _finish()], '{"findings": []}')
    clock = 0
    saved: list[Mapping[str, object]] = []

    def late_turn(_: Sequence[ToolCallResult] = ()) -> ModelTurn:
        nonlocal clock
        clock = 100
        return ModelTurn(text='{"findings": []}', usage={"prompt_tokens": 19})

    monkeypatch.setattr(provider.reviewer, "_next", late_turn)
    broker = SharedToolBroker(worktree=root, scopes=("a.py",))
    with pytest.raises(LlmCoordError, match="wall-clock budget"):
        CodingAgentHarness(
            provider,
            review="on",
            clock=lambda: clock,
            limits=ExecutionLimits(wall_clock_seconds=10),
        ).run(
            RunRequest(
                task_id="task",
                attempt=1,
                instructions="fix",
                scopes=("a.py",),
                worktree=root,
                base_oid="base",
                workspace_mode="shared",
                checkpoint=lambda value: saved.append(copy.deepcopy(dict(value))),
                review_context=lambda: {"diff": "diff", "line_counts": {"a.py": 1}},
            ),
            broker,
        )
    assert saved[-1]["usage_total"]["review_prompt_tokens"] == 19
    assert not broker.usage.finished
    assert (root / "a.py").read_text() == "value = 1\n"
