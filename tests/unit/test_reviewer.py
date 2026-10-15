"""Review is bounded, screened, read-only and explicitly configured."""

from __future__ import annotations

import io
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from llm_cli.agent.reviewer import REVIEW_TOOLS, Reviewer
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import TaskCancelled, ToolOutcome
from llm_cli.cli.render import EventRenderer, render_event
from llm_cli.config.loader import load_settings
from llm_cli.errors import LlmCoordError
from llm_cli.providers.base import (
    ModelTurn,
    StreamCallback,
    ToolCallRequest,
    ToolCallResult,
)
from llm_cli.web.access import WebAccess


class Provider:
    name = model = "scripted-review"

    def __init__(self, steps: Sequence[ModelTurn]) -> None:
        self.steps = list(steps)
        self.results: list[ToolCallResult] = []
        self.offered: tuple[str, ...] = ()
        self.calls = 0

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> Provider:
        assert "File contents" in system
        assert state is None
        self.offered = tuple(str(t["name"]) for t in tools)
        return self

    def snapshot(self) -> Mapping[str, object]:
        return {}

    def send_user(self, text: str) -> ModelTurn:
        self.calls += 1
        assert "original_task" in json.loads(text)
        return self.steps.pop(0)

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        self.calls += 1
        self.results.extend(results)
        return self.steps.pop(0)


def test_reviewer_refuses_mutation_commands_web_and_nested_helpers(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "unchanged\n"})
    web = WebAccess("allow")

    def forbidden_fetch(*args: object, **kwargs: object) -> None:
        raise AssertionError("the independent reviewer reached web fetching")

    def forbidden_command(argv: list[str], cwd: str, timeout: int) -> ToolOutcome:
        raise AssertionError("the independent reviewer reached command execution")

    monkeypatch.setattr(web, "page", forbidden_fetch)
    broker = SharedToolBroker(
        worktree=root,
        scopes=("a.py",),
        command_runner=forbidden_command,
        command_approval="allow",
        web=web,
    )
    assert {"run_command", "web_fetch"} <= set(broker.tool_names())
    provider = Provider(
        [
            ModelTurn(
                text="",
                tool_calls=(
                    ToolCallRequest(
                        "write", "write_file", {"path": "a.py", "content": "bad"}
                    ),
                    ToolCallRequest(
                        "command",
                        "run_command",
                        {
                            "argv": ["python", "-c", "print(1)"],
                            "cwd": ".",
                            "timeout": 1,
                        },
                    ),
                    ToolCallRequest(
                        "fetch", "web_fetch", {"url": "https://docs.python.org"}
                    ),
                    ToolCallRequest(
                        "nested", "explore", {"task": "inspect", "label": "x"}
                    ),
                ),
                stop_reason="tool_use",
                usage={"prompt_tokens": 4},
            ),
            ModelTurn(text='{"findings": []}', usage={"output_tokens": 2}),
        ]
    )
    reviewer = Reviewer(
        provider, broker, effort_ceiling=None, deadline=100, clock=lambda: 1
    )
    result = reviewer.review("task", {"diff": "diff", "line_counts": {"a.py": 1}})
    assert result.failure is None and not result.findings
    assert provider.offered == REVIEW_TOOLS
    assert all(result.is_error for result in provider.results)
    assert broker.usage.calls == 4
    assert reviewer.usage == {"prompt_tokens": 4, "output_tokens": 2}
    assert (root / "a.py").read_text() == "unchanged\n"
    assert not broker.candidates()


@pytest.mark.parametrize(
    "report",
    [
        '{"findings": [{"path":"b.py","line":1,"message":"bad"}]}',
        '{"findings": [{"path":"a.py","line":2,"message":"bad"}]}',
        '{"findings": [{"path":"a.py","line":true,"message":"bad"}]}',
        "{}",
        '{"findings": [], "permission": "write"}',
    ],
)
def test_unanchored_or_malformed_reports_fail_without_claiming_no_findings(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    report: str,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "safe\n"})
    broker = SharedToolBroker(worktree=root, scopes=("a.py",))
    result = Reviewer(
        Provider([ModelTurn(text=report)]),
        broker,
        effort_ceiling=None,
        deadline=100,
        clock=lambda: 1,
    ).review(
        "task",
        {"diff": "diff", "line_counts": {"a.py": 1}},
    )
    assert result.failure and not result.findings


@pytest.mark.parametrize("fence_newline", [None, "\n", "\r\n"])
@pytest.mark.parametrize(
    "findings", [[], [{"path": "a.py", "line": 1, "message": "A bug"}]]
)
def test_standalone_json_reports_allow_a_json_code_fence(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    fence_newline: str | None,
    findings: list[dict[str, object]],
) -> None:
    root = repository_factory(tmp_path, {"a.py": "safe\n"})
    report = json.dumps({"findings": findings})
    if fence_newline is not None:
        report = f" \n```json{fence_newline}{report}{fence_newline}```\n "
    result = Reviewer(
        Provider([ModelTurn(text=report)]),
        SharedToolBroker(worktree=root, scopes=("a.py",)),
        effort_ceiling=None,
        deadline=100,
        clock=lambda: 1,
    ).review("task", {"line_counts": {"a.py": 1}})
    assert result.failure is None
    assert result.findings == ((("a.py", 1, "A bug"),) if findings else ())


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {},
        {"path": "../a.py", "line": 1, "message": "escape"},
        {"path": "b.py", "line": 1, "message": "outside diff"},
        {"path": "a.py", "line": True, "message": "not a line"},
        {"path": "a.py", "line": 2, "message": "past end"},
        {"path": "a.py", "line": 1, "message": " "},
        {"path": "a.py", "line": 1, "message": "x" * 1001},
    ],
)
def test_malformed_findings_do_not_discard_other_valid_findings(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    invalid: object,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "safe\n"})
    valid = {"path": "a.py", "line": 1, "message": "  A concrete bug  "}
    result = Reviewer(
        Provider([ModelTurn(text=json.dumps({"findings": [invalid, valid]}))]),
        SharedToolBroker(worktree=root, scopes=("a.py",)),
        effort_ceiling=None,
        deadline=100,
        clock=lambda: 1,
    ).review("task", {"line_counts": {"a.py": 1}})
    assert result.failure is None
    assert result.findings == (("a.py", 1, "A concrete bug"),)


@pytest.mark.parametrize(
    "report",
    [
        'Here is the report:\n```json\n{"findings": []}\n```',
        '```json\n{"findings": []}\n```\nAdditional advice',
        '```json\n{"findings": []}\n```\n```json\n{"findings": []}\n```',
        '{"findings": [null, {}]}',
        json.dumps({"findings": [{}] * 11}),
        json.dumps(
            {
                "findings": [
                    {"path": "a.py", "line": 1, "message": "A bug"},
                    {"message": "-----BEGIN PRIVATE KEY-----"},
                ]
            }
        ),
    ],
)
def test_report_recovery_preserves_standalone_shape_security_and_bounds(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    report: str,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "safe\n"})
    result = Reviewer(
        Provider([ModelTurn(text=report)]),
        SharedToolBroker(worktree=root, scopes=("a.py",)),
        effort_ceiling=None,
        deadline=100,
        clock=lambda: 1,
    ).review("task", {"line_counts": {"a.py": 1}})
    assert result.failure and not result.findings


@pytest.mark.parametrize("cancel", [False, True])
def test_blocked_provider_is_abandoned_without_late_tools_or_usage(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "safe\n"})
    started, release, returned = threading.Event(), threading.Event(), threading.Event()
    provider = Provider([])

    def blocked(_: str) -> ModelTurn:
        started.set()
        assert release.wait(2)
        returned.set()
        return ModelTurn(
            text="",
            tool_calls=(ToolCallRequest("late", "read_file", {"path": "a.py"}),),
            stop_reason="tool_use",
            usage={"prompt_tokens": 999},
        )

    monkeypatch.setattr(provider, "send_user", blocked)
    broker = SharedToolBroker(
        worktree=root, scopes=("a.py",), cancelled=lambda: cancel and started.is_set()
    )
    reviewer = Reviewer(
        provider, broker, effort_ceiling=None, deadline=1.05, clock=lambda: 1
    )
    before = time.monotonic()
    try:
        if cancel:
            with pytest.raises(TaskCancelled):
                reviewer.review("task", {"line_counts": {"a.py": 1}})
        else:
            assert reviewer.review("task", {"line_counts": {"a.py": 1}}).failure
        assert time.monotonic() - before < 1
        assert started.is_set()
        assert reviewer.usage == {} and broker.usage.calls == 0
    finally:
        release.set()
    assert returned.wait(1)
    assert reviewer.usage == {} and broker.usage.calls == 0
    assert provider.calls == 0 and not provider.results


def test_blocked_session_creation_cannot_start_a_late_request(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "safe\n"})
    release, returned = threading.Event(), threading.Event()
    provider = Provider([ModelTurn(text='{"findings": []}')])

    def blocked(**kwargs: object) -> Provider:
        assert release.wait(2)
        returned.set()
        return provider

    monkeypatch.setattr(provider, "session", blocked)
    reviewer = Reviewer(
        provider,
        SharedToolBroker(worktree=root, scopes=("a.py",)),
        effort_ceiling=None,
        deadline=1.05,
        clock=lambda: 1,
    )
    try:
        assert reviewer.review("task", {"line_counts": {"a.py": 1}}).failure
    finally:
        release.set()
    assert returned.wait(1)
    assert provider.calls == 0 and reviewer.usage == {}


def test_abandoned_stream_stops_at_next_provider_event(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "safe\n"})
    release, stopped = threading.Event(), threading.Event()
    provider = Provider([])
    callback: StreamCallback | None = None

    def set_callback(value: StreamCallback | None) -> None:
        nonlocal callback
        callback = value

    def streaming(_: str) -> ModelTurn:
        assert release.wait(2)
        assert callback is not None
        try:
            callback("text", {"text": "late private delta"})
        except TimeoutError:
            stopped.set()
            raise
        raise AssertionError("the abandoned stream kept running")

    monkeypatch.setattr(provider, "set_event_callback", set_callback, raising=False)
    monkeypatch.setattr(provider, "send_user", streaming)
    reviewer = Reviewer(
        provider,
        SharedToolBroker(worktree=root, scopes=("a.py",)),
        effort_ceiling=None,
        deadline=1.05,
        clock=lambda: 1,
    )
    try:
        assert reviewer.review("task", {"line_counts": {"a.py": 1}}).failure
    finally:
        release.set()
    assert stopped.wait(1)
    assert reviewer.usage == {} and not provider.results


def test_review_does_not_send_secret_or_truncated_inputs(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
) -> None:
    root = repository_factory(tmp_path, {"a.py": "safe\n"})
    broker = SharedToolBroker(worktree=root, scopes=("a.py",))
    provider = Provider([])
    reviewer = Reviewer(
        provider, broker, effort_ceiling=None, deadline=100, clock=lambda: 1
    )
    for diff in ("x" * (256 * 1024), "-----BEGIN PRIVATE KEY-----\nabc"):
        result = reviewer.review("task", {"diff": diff, "line_counts": {"a.py": 1}})
        assert result.failure and not result.findings
    assert provider.calls == 0
    assert broker.usage.calls == 0


def test_review_reads_do_not_authorize_the_parent_to_write(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
) -> None:
    root = repository_factory(tmp_path, {"a.py": "safe\n"})
    broker = SharedToolBroker(worktree=root, scopes=("a.py",))
    provider = Provider(
        [
            ModelTurn(
                text="",
                tool_calls=(ToolCallRequest("read", "read_file", {"path": "a.py"}),),
                stop_reason="tool_use",
            ),
            ModelTurn(text='{"findings": []}'),
        ]
    )
    result = Reviewer(
        provider, broker, effort_ceiling=None, deadline=100, clock=lambda: 1
    ).review(
        "task",
        {"diff": "diff", "line_counts": {"a.py": 1}},
    )
    assert result.failure is None
    assert provider.results[0].content == "safe\n"
    refused = broker.invoke("write_file", {"path": "a.py", "content": "edited\n"})
    assert refused.is_error
    assert not broker.candidates()


def test_deadline_and_cancellation_stop_review_before_provider_request(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
) -> None:
    root = repository_factory(tmp_path, {"a.py": "safe\n"})
    broker = SharedToolBroker(worktree=root, scopes=("a.py",))
    provider = Provider([])
    reviewer = Reviewer(
        provider, broker, effort_ceiling=None, deadline=0, clock=lambda: 1
    )
    assert reviewer.review("task", {}).failure
    broker.cancelled = lambda: True
    with pytest.raises(TaskCancelled):
        reviewer.review("task", {})
    assert provider.calls == 0
    assert broker.usage.calls == 0


def test_review_configuration_defaults_and_validated_effort(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    assert load_settings(path).agent_review == "off"
    assert load_settings(path).agent_review_effort == "low"
    path.write_text('[agent]\nreview="on"\nreview_effort="task"\n')
    assert load_settings(path).agent_review == "on"
    assert load_settings(path).agent_review_effort is None
    for value in ("review=true", "review=[]", 'review="maybe"', "review_effort=[]"):
        path.write_text("[agent]\n" + value + "\n")
        with pytest.raises(LlmCoordError):
            load_settings(path)


def test_review_notices_are_visible_in_plain_and_conversation_renderers() -> None:
    started = {"event_type": "review.started", "payload": {}}
    finished = {
        "event_type": "review.finished",
        "payload": {
            "state": "completed",
            "findings": 2,
        },
    }
    failed = {"event_type": "review.finished", "payload": {"state": "failed"}}
    assert "Independent review" in str(render_event(started))
    assert "2 findings" in str(render_event(finished))
    assert "continuing without review" in str(render_event(failed))
    output = io.StringIO()
    renderer = EventRenderer(output, plain=True)
    renderer.render(started)
    renderer.render(finished)
    renderer.render(failed)
    assert "2 findings" in output.getvalue()
    assert "continuing without review" in output.getvalue()
