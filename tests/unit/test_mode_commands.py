"""Mode controls preserve session identity and fail closed on older daemons."""

from __future__ import annotations

from typing import Any, cast

import pytest
from test_shell import Client, calls, start
from test_shell import client as client

from llm_cli.cli import app, session
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.protocol.client import DaemonClient


def test_mode_choice_before_first_prompt_needs_no_daemon(client: Client) -> None:
    chat = start(client)
    assert chat.agent_mode == "normal"
    chat._command("/mode plan")
    assert chat.agent_mode == "plan"
    assert chat.composer.mode == "plan"
    assert client.calls == []
    with pytest.raises(ValueError, match="mode must"):
        chat._command("/mode guess")
    assert chat.agent_mode == "plan"


def test_shell_handles_repeated_mode_shortcuts_without_submitting(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = start(client)
    seen: list[str] = []

    def read() -> str:
        for _ in range(3):
            seen.append(chat.composer.mode)
            assert chat.composer.on_mode_cycle is not None
            chat.composer.on_mode_cycle()
        seen.append(chat.composer.mode)
        return "/exit"

    monkeypatch.setattr(chat.composer, "read", read)
    assert chat.run() == 0
    assert seen == ["normal", "auto", "plan", "normal"]
    assert client.calls == []
    assert "Mode:" not in chat.stream.getvalue()  # type: ignore[attr-defined]


@pytest.mark.parametrize("failure", ["busy", "rpc", "unconfirmed"])
def test_shortcut_refusal_keeps_mode_and_reopens_editor(
    client: Client, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    chat = start(client, provider="test")
    assert chat._ensure_session()
    assert chat.credentials is not None
    original_credentials = chat.credentials
    original_call = client.call
    if failure == "busy":
        client.tasks.append(
            {
                "task_id": "busy",
                "session_id": original_credentials.session_id,
                "state": "active_work",
            }
        )

    def call(method: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        if method == "session.set_mode":
            assert params is not None
            client.calls.append((method, params))
            if failure == "rpc":
                raise OSError("daemon disconnected")
            return {"session": client.sessions[params["session_id"]]}
        return original_call(method, params, **kw)

    seen: list[str] = []

    def read() -> str:
        seen.append(chat.composer.mode)
        assert chat.composer.on_mode_cycle is not None
        with pytest.raises((LlmCoordError, OSError)):
            chat.composer.on_mode_cycle()
        seen.append(chat.composer.mode)
        return "/detach"

    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr(chat.composer, "read", read)
    assert chat.run() == 0
    assert seen == ["normal", "normal"]
    assert chat.credentials == original_credentials
    assert not calls(client, "task.run")
    assert len(calls(client, "session.set_mode")) == (0 if failure == "busy" else 1)
    output = chat.stream.getvalue()  # type: ignore[attr-defined]
    assert "Mode: auto" not in output


def test_mode_changes_preserve_session_and_refuse_while_busy(client: Client) -> None:
    chat = start(client, provider="test")
    assert chat._ensure_session()
    assert chat.credentials is not None
    original = chat.credentials
    call = client.call

    def mode_call(
        method: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        if method == "session.set_mode":
            assert params is not None
            client.calls.append((method, params))
            record = client.sessions[params["session_id"]]
            record["agent_mode"] = params["mode"]
            return {"session": record}
        return call(method, params, **kwargs)

    client.call = mode_call  # type: ignore[method-assign]
    chat._command("/mode auto")
    assert chat.credentials.session_id == original.session_id
    assert chat.credentials.resume_secret == original.resume_secret
    assert chat.credentials.agent_mode == chat.agent_mode == "auto"
    assert len(calls(client, "session.open")) == 1
    client.tasks.append(
        {"task_id": "busy", "session_id": original.session_id, "state": "active_work"}
    )
    with pytest.raises(LlmCoordError, match="still running"):
        chat._command("/mode plan")
    assert len(calls(client, "session.set_mode")) == 1
    assert chat.agent_mode == "auto"


@pytest.mark.parametrize("mode", ["plan", "normal", "auto"])
def test_old_daemon_cannot_silently_ignore_requested_mode(
    client: Client, mode: str
) -> None:
    original = client.call

    def old_call(
        method: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        result = original(method, params, **kwargs)
        if method == "session.open":
            result["session"].pop("agent_mode")
        return result

    client.call = old_call  # type: ignore[method-assign]
    with pytest.raises(LlmCoordError, match="did not confirm") as caught:
        session.open_or_resume_session(
            cast(DaemonClient, client),
            repository=client.paths.state_dir.parent,
            provider="test",
            model="test-model",
            resume_session_id=None,
            agent_mode=mode,
        )
    assert caught.value.code is ErrorCode.PROTOCOL_MISMATCH
    assert "--resume" in caught.value.message
    assert not calls(client, "task.run")
    opened = calls(client, "session.open")[0]
    assert session.session_resume_secret(client.paths, opened["session_id"])


@pytest.mark.parametrize("flag", ["--claim-only", "--fixture-write"])
def test_one_shot_mode_refuses_unsupported_driver_before_opening_session(
    client: Client, flag: str
) -> None:
    args = app.build_parser().parse_args(
        ["run", "plan work", "--scope", "src/", "--mode", "plan", flag]
        + (["src/a.py=value"] if flag == "--fixture-write" else [])
    )
    with pytest.raises(LlmCoordError, match="cannot be combined"):
        app.dispatch(args, cast(DaemonClient, client))
    assert client.calls == []


def test_global_mode_survives_default_chat_selection(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from io import StringIO

    class TerminalInput(StringIO):
        def isatty(self) -> bool:
            return True

    selected: list[str | None] = []
    monkeypatch.setattr(app.sys, "stdin", TerminalInput())
    monkeypatch.setattr(app, "DaemonClient", lambda paths: client)
    monkeypatch.setattr(
        app, "run_session", lambda *args, **kw: selected.append(kw["agent_mode"])
    )
    app.main(["--mode", "plan"])
    assert selected == ["plan"]


def test_mode_flag_is_not_silently_ignored_on_other_commands(client: Client) -> None:
    args = app.build_parser().parse_args(["--mode", "plan", "task", "list"])
    with pytest.raises(LlmCoordError, match="--mode is available"):
        app.dispatch(args, cast(DaemonClient, client))
    assert client.calls == []


@pytest.mark.parametrize("command", [["chat"], ["session", "open"]])
def test_conflicting_flags_exit_cleanly_before_opening_session(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    monkeypatch.setattr(app, "DaemonClient", lambda paths: client)
    with pytest.raises(SystemExit) as caught:
        app.main([*command, "--mode", "plan", "--publish", "auto"])
    assert caught.value.code == app.EXIT_BY_ERROR[ErrorCode.CONFIG_INVALID]
    output = capsys.readouterr()
    assert "conflicts" in output.err
    assert "Traceback" not in output.err
    assert client.calls == []
