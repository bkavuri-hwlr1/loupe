"""Terminal conversations preserve output, question ownership, and resume state."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from llm_cli.cli import app, composer, session
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient


def _event(sequence: int, kind: str, **payload: object) -> dict[str, object]:
    return {"sequence": sequence, "event_type": kind, "payload": payload}


class Client:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.attaches: list[dict[str, Any]] = []
        self.batches: list[list[dict[str, object]]] = []
        self.states: list[str] = ["completed"]
        self.question: dict[str, object] = {}
        self.close_error: LlmCoordError | None = None
        self.run_error: BaseException | None = None
        self.answer_error: BaseException | None = None
        self.tasks: list[dict[str, object]] = []

    def call(
        self, method: str, params: dict[str, Any] | None = None, **kwargs: object
    ) -> object:
        self.calls.append((method, params or {}))
        if method == "task.question":
            return self.question
        if method == "task.show":
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"state": state}
        if method == "task.list":
            return self.tasks
        if method == "session.events":
            return []
        if method == "session.close" and self.close_error:
            raise self.close_error
        if method == "task.run" and self.run_error:
            raise self.run_error
        if method == "task.answer" and self.answer_error:
            raise self.answer_error
        if method in {
            "repo.status",
            "session.set_intent",
            "session.close",
            "task.run",
            "task.answer",
        }:
            return {}
        raise AssertionError(f"unexpected daemon call: {method}")

    def stream(
        self, method: str, params: dict[str, Any]
    ) -> Iterator[dict[str, object]]:
        assert method == "task.attach"
        self.attaches.append(params)
        if not self.batches:
            raise AssertionError("unexpected extra attach")
        return iter(self.batches.pop(0))


@pytest.fixture
def client(tmp_path: Path) -> Client:
    return Client(
        AppPaths(
            "test",
            tmp_path / "config",
            tmp_path / "data",
            tmp_path / "state",
            tmp_path / "run",
        )
    )


@pytest.fixture
def opened(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> session._SessionCredentials:
    credentials = session._SessionCredentials(
        "session_test", "private-resume-secret", "test", "test-model", 0
    )
    session._write_secret(
        client.paths, credentials.session_id, credentials.resume_secret
    )
    monkeypatch.setattr(
        session, "open_or_resume_session", lambda *args, **kwargs: credentials
    )
    return credentials


def test_follow_reconnects_after_idle_with_cursor_and_preserves_all_output(
    client: Client,
) -> None:
    client.batches = [
        [_event(1, "model.text.delta", turn_id="turn", text="Starting now. ")],
        [
            _event(2, "model.text.delta", turn_id="turn", text="Still working."),
            _event(
                3, "model.said", turn_id="turn", text="Starting now. Still working."
            ),
            _event(4, "model.finished", summary="Task finished."),
        ],
    ]
    client.states = ["running", "completed"]
    output = io.StringIO()
    session._follow(
        cast(DaemonClient, client),
        task_id="task",
        stream=output,
        input_stream=io.StringIO(),
        plain=True,
    )
    assert client.attaches == [
        {"task_id": "task", "after": 0},
        {"task_id": "task", "after": 1},
    ]
    assert output.getvalue().count("Starting now. Still working.") == 1
    assert "Task finished." in output.getvalue()


def test_read_only_turn_displays_answer_without_routine_task_metadata(
    client: Client,
    opened: session._SessionCredentials,
    tmp_path: Path,
) -> None:
    answer = "This repository implements a terminal coding agent."
    client.batches = [
        [
            _event(1, "model.finished", answer=answer, summary="Prepared an overview."),
            _event(2, "workflow.verification", status="Not verified"),
            _event(3, "execution.published", outcome="no_changes"),
        ]
    ]
    output = io.StringIO()
    task_id = session._run_one(
        cast(DaemonClient, client),
        repository=tmp_path,
        scopes=["*"],
        instruction="Summarize this repository",
        credentials=opened,
        stream=output,
        input_stream=io.StringIO(),
        plain=True,
    )
    assert task_id is not None
    assert answer in output.getvalue()
    for metadata in (
        task_id,
        "Elapsed",
        "Ctrl-C",
        "Not verified",
        "Prepared an overview",
    ):
        assert metadata not in output.getvalue()


def test_follow_skips_answered_history_and_answers_only_current_question_id(
    client: Client,
) -> None:
    client.question = {"pending": True, "question_id": "current"}
    client.batches = [
        [
            _event(1, "question.asked", question="Old question", question_id="old"),
            _event(
                2, "question.asked", question="Current question", question_id="current"
            ),
        ],
        [_event(3, "model.said", text="Answer received.")],
    ]
    source = io.StringIO("Use the new path\n")
    output = io.StringIO()
    session._follow(
        cast(DaemonClient, client),
        task_id="task",
        stream=output,
        input_stream=source,
        plain=True,
    )
    assert [params for method, params in client.calls if method == "task.answer"] == [
        {"task_id": "task", "question_id": "current", "answer": "Use the new path"}
    ]
    assert client.attaches[-1]["after"] == 2
    assert "Answer received." in output.getvalue()


def test_blank_answers_reprompt_and_are_never_submitted(client: Client) -> None:
    output = io.StringIO()
    assert session._answer(
        cast(DaemonClient, client),
        task_id="task",
        question="Which?",
        question_id="q",
        stream=output,
        input_stream=io.StringIO("\n   \n chosen \n"),
        plain=True,
    )
    assert client.calls == [
        ("task.answer", {"task_id": "task", "question_id": "q", "answer": "chosen"})
    ]
    assert output.getvalue().count("Enter an answer") == 2


@pytest.mark.parametrize("interrupted", [False, True], ids=["eof", "ctrl-c"])
def test_answer_interruption_keeps_question_pending(
    client: Client, interrupted: bool
) -> None:
    class Input(io.StringIO):
        def readline(self, size: int = -1) -> str:
            if interrupted:
                raise KeyboardInterrupt
            return ""

    output = io.StringIO()
    assert not session._answer(
        cast(DaemonClient, client),
        task_id="task",
        question="Which?",
        question_id="q",
        stream=output,
        input_stream=Input(),
        plain=True,
    )
    assert client.calls == []
    assert "Question left pending" in output.getvalue()
    assert "loupe --profile test task watch task" in output.getvalue()


def test_scope_command_is_exact_and_parses_quoted_paths_before_submission(
    client: Client,
    opened: session._SessionCredentials,
    tmp_path: Path,
) -> None:
    client.batches = [[]]
    output = io.StringIO()
    assert (
        session.run_session(
            cast(DaemonClient, client),
            repository=tmp_path,
            scopes=["initial/"],
            provider="test",
            resume_session_id=opened.session_id,
            input_stream=io.StringIO(
                '/scopex wrong/\n/scope "my docs/" src/\ninspect files\n/exit\n'
            ),
            output=output,
            plain=True,
        )
        == 0
    )
    assert [
        params["paths"]
        for method, params in client.calls
        if method == "session.set_intent"
    ] == [["initial/"], ["my docs/", "src/"]]
    submissions = [params for method, params in client.calls if method == "task.run"]
    assert len(submissions) == 1
    assert submissions[0]["scopes"] == ["my docs/", "src/"]
    assert submissions[0]["title"] == "inspect files"
    assert "Unknown command '/scopex'" in output.getvalue()
    assert not client.paths.session_secret_file(opened.session_id).exists()


def test_detach_retains_private_resume_secret_and_does_not_close(
    client: Client,
    opened: session._SessionCredentials,
    tmp_path: Path,
) -> None:
    output = io.StringIO()
    session.run_session(
        cast(DaemonClient, client),
        repository=tmp_path,
        scopes=["my docs/"],
        provider="test",
        resume_session_id=opened.session_id,
        input_stream=io.StringIO("/detach\n"),
        output=output,
        plain=True,
    )
    assert not any(method == "session.close" for method, _ in client.calls)
    assert (
        session.session_resume_secret(client.paths, opened.session_id)
        == opened.resume_secret
    )
    assert f"--resume {opened.session_id}" in output.getvalue()
    assert "--scope 'my docs/'" in output.getvalue()
    assert opened.resume_secret not in output.getvalue()


def test_exit_keeps_credentials_when_resumed_task_is_still_running(
    client: Client,
    opened: session._SessionCredentials,
    tmp_path: Path,
) -> None:
    client.tasks = [
        {"session_id": opened.session_id, "task_id": "live-task", "state": "running"}
    ]
    client.states = ["running"]
    session.run_session(
        cast(DaemonClient, client),
        repository=tmp_path,
        scopes=["src/"],
        resume_session_id=opened.session_id,
        input_stream=io.StringIO("/exit\n"),
        output=io.StringIO(),
        plain=True,
    )
    assert any(method == "task.list" for method, _ in client.calls)
    assert not any(method == "session.close" for method, _ in client.calls)
    assert (
        session.session_resume_secret(client.paths, opened.session_id)
        == opened.resume_secret
    )


def test_close_failure_preserves_the_only_resume_secret(
    client: Client,
    opened: session._SessionCredentials,
    tmp_path: Path,
) -> None:
    client.close_error = LlmCoordError(ErrorCode.DAEMON_UNAVAILABLE, "connection lost")
    output = io.StringIO()
    session.run_session(
        cast(DaemonClient, client),
        repository=tmp_path,
        scopes=["src/"],
        provider="test",
        resume_session_id=opened.session_id,
        input_stream=io.StringIO("/exit\n"),
        output=output,
        plain=True,
    )
    assert any(method == "session.close" for method, _ in client.calls)
    assert (
        session.session_resume_secret(client.paths, opened.session_id)
        == opened.resume_secret
    )
    assert "Session kept" in output.getvalue()
    assert f"--resume {opened.session_id}" in output.getvalue()
    assert opened.resume_secret not in output.getvalue()


@pytest.mark.parametrize("interrupted", [False, True], ids=["lost-response", "ctrl-c"])
def test_lost_task_submission_response_keeps_task_id_and_resume_credentials(
    client: Client,
    opened: session._SessionCredentials,
    tmp_path: Path,
    interrupted: bool,
) -> None:
    client.run_error = (
        KeyboardInterrupt()
        if interrupted
        else LlmCoordError(ErrorCode.DAEMON_UNAVAILABLE, "response was lost")
    )
    # The daemon accepted and started the task before the response was lost.
    client.states = ["running"]
    output = io.StringIO()
    session.run_session(
        cast(DaemonClient, client),
        repository=tmp_path,
        scopes=["src/"],
        provider="test",
        input_stream=io.StringIO("inspect the project\n/exit\n"),
        output=output,
        plain=True,
    )
    submission = next(params for method, params in client.calls if method == "task.run")
    assert ("task.show", {"task_id": submission["task_id"]}) in client.calls
    assert submission["task_id"] in output.getvalue()
    assert not any(method == "session.close" for method, _ in client.calls)
    assert (
        session.session_resume_secret(client.paths, opened.session_id)
        == opened.resume_secret
    )


@pytest.mark.parametrize("interrupted", [False, True], ids=["lost-response", "ctrl-c"])
@pytest.mark.parametrize("in_chat", [False, True], ids=["standalone", "chat"])
def test_uncertain_answer_delivery_detaches_with_reconnect_guidance(
    client: Client, interrupted: bool, in_chat: bool
) -> None:
    client.answer_error = (
        KeyboardInterrupt()
        if interrupted
        else LlmCoordError(ErrorCode.DAEMON_UNAVAILABLE, "response was lost")
    )
    output = io.StringIO()
    input_stream = io.StringIO("answer\n")
    assert not session._answer(
        cast(DaemonClient, client),
        task_id="task",
        question="Which?",
        question_id="q",
        stream=output,
        input_stream=input_stream,
        composer=(
            composer.Composer(input_stream, output, plain=True) if in_chat else None
        ),
        plain=True,
    )
    command = "/attach task" if in_chat else "loupe --profile test task watch task"
    assert command in output.getvalue()


def test_piped_composer_uses_no_tty_and_keeps_only_prompt_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        composer,
        "PromptSession",
        lambda *a, **kw: pytest.fail("opened terminal editor"),
    )
    editor = composer.Composer(io.StringIO("inspect\n/status\nanswer\n"), io.StringIO())
    assert editor.read() == "inspect"
    assert editor.read() == "/status"
    assert editor.read(answer=True) == "answer"
    assert editor.prompts == ["inspect"]
    with pytest.raises(EOFError):
        editor.read()


def test_bare_interactive_main_starts_chat_with_preserved_global_options(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(app.sys, "stdin", Tty())
    monkeypatch.setattr(app.AppPaths, "resolve", lambda profile: client.paths)
    monkeypatch.setattr(app, "DaemonClient", lambda paths: client)
    monkeypatch.setattr(
        app, "run_session", lambda client, **kwargs: seen.append(kwargs)
    )
    app.main(["--plain", "--profile", "test"])
    assert len(seen) == 1
    assert seen[0]["scopes"] == ["*"]
    assert seen[0]["plain"] is True
    assert seen[0]["repository"].is_absolute()
    assert client.calls == []


@pytest.mark.parametrize("arguments", [["--json"], ["--json", "chat"]])
def test_json_interactive_modes_fail_before_any_daemon_access(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app, "DaemonClient", lambda paths: pytest.fail("created client")
    )
    with pytest.raises(SystemExit) as caught:
        app.main(arguments)
    assert caught.value.code == 2


def test_demo_runs_without_daemon_calls(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(app.AppPaths, "resolve", lambda profile: client.paths)
    monkeypatch.setattr(app, "DaemonClient", lambda paths: client)
    app.main(["--plain", "demo"])
    output = capsys.readouterr().out
    assert "simulated" in output
    assert "Hello from Loupe!" in output
    assert client.calls == []
    assert client.attaches == []
