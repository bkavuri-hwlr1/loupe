"""Review is bounded, screened, read-only and explicitly configured."""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from llm_cli.agent.reviewer import REVIEW_TOOLS, Reviewer
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import TaskCancelled, ToolOutcome
from llm_cli.cli.render import EventRenderer, render_event
from llm_cli.config.loader import load_settings
from llm_cli.errors import LlmCoordError
from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult
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
