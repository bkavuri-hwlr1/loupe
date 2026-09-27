"""One-shot runs opt into questions without changing background defaults."""

from __future__ import annotations

from typing import Any, cast

import pytest

from llm_cli.cli import app
from llm_cli.cli.interrupts import ExitRequested
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = {"task": {"task_id": "accepted-task"}}

    def call(self, method: str, params: dict[str, Any], **_: Any) -> object:
        self.calls.append((method, params))
        return self.result


@pytest.mark.parametrize("explicit_follow", [False, True])
def test_interactive_run_enables_questions_and_follows_accepted_task(
    monkeypatch: pytest.MonkeyPatch, explicit_follow: bool
) -> None:
    client = _Client()
    followed: list[tuple[object, ...]] = []

    def watch(*arguments: object) -> object:
        followed.append(arguments)
        return app._RENDERED

    monkeypatch.setattr(app, "_watch", watch)
    args = app.build_parser().parse_args(
        ["--plain", "run", "pick a format", "--scope", "docs/", "--interactive"]
        + (["--follow"] if explicit_follow else [])
    )
    assert app.dispatch(args, cast(DaemonClient, client)) is app._RENDERED
    assert len(client.calls) == 1
    method, params = client.calls[0]
    assert method == "task.run"
    assert params["interactive"] is True
    assert params["scopes"] == ["docs/"]
    assert followed == [(client, "accepted-task", 0, False, True)]


@pytest.mark.parametrize("follow", [False, True])
def test_default_run_and_follow_remain_noninteractive(
    monkeypatch: pytest.MonkeyPatch, follow: bool
) -> None:
    client = _Client()
    followed: list[tuple[object, ...]] = []

    def watch(*arguments: object) -> object:
        followed.append(arguments)
        return app._RENDERED

    monkeypatch.setattr(app, "_watch", watch)
    args = app.build_parser().parse_args(
        ["run", "update docs", "--scope", "docs/"] + (["--follow"] if follow else [])
    )
    result = app.dispatch(args, cast(DaemonClient, client))
    assert client.calls[0][1]["interactive"] is False
    if follow:
        assert result is app._RENDERED
        assert followed == [(client, "accepted-task", 0, False, False)]
    else:
        assert result is client.result
        assert followed == []


@pytest.mark.parametrize("incompatible", ["--json", "--claim-only"])
def test_interactive_run_rejects_incompatible_flags_before_task_submission(
    monkeypatch: pytest.MonkeyPatch, incompatible: str
) -> None:
    client = _Client()

    def never_watch(*_: object) -> object:
        raise AssertionError("an invalid run must not start following a task")

    monkeypatch.setattr(app, "_watch", never_watch)
    args = app.build_parser().parse_args(
        (["--json"] if incompatible == "--json" else [])
        + ["run", "update docs", "--scope", "docs/", "--interactive"]
        + (["--claim-only"] if incompatible == "--claim-only" else [])
    )
    with pytest.raises(LlmCoordError, match=incompatible) as caught:
        app.dispatch(args, cast(DaemonClient, client))
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert client.calls == []


def test_double_interrupt_at_standalone_question_exits_without_cancelling_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class QuestionClient(_Client):
        paths: AppPaths

        def call(self, method: str, params: dict[str, Any], **kwargs: Any) -> object:
            if method == "task.question":
                self.calls.append((method, params))
                return {"pending": True, "question_id": "live-question"}
            return super().call(method, params, **kwargs)

        def stream(self, method: str, params: dict[str, Any]) -> Any:
            assert method == "task.attach"
            assert params["task_id"] == "accepted-task"
            yield {
                "sequence": 1,
                "event_type": "question.asked",
                "payload": {
                    "question_id": "live-question",
                    "question": "Which format?",
                },
            }

    client = QuestionClient()

    def connect(paths: AppPaths) -> QuestionClient:
        client.paths = paths
        return client

    monkeypatch.setattr(app, "DaemonClient", connect)

    def double_interrupt(*_: object, **__: object) -> str:
        raise ExitRequested()

    monkeypatch.setattr("llm_cli.cli.session.Composer.read", double_interrupt)
    with pytest.raises(SystemExit) as caught:
        app.main(
            ["--plain", "run", "pick a format", "--scope", "docs/", "--interactive"]
        )
    assert caught.value.code == 0
    assert [method for method, _ in client.calls] == ["task.run", "task.question"]
