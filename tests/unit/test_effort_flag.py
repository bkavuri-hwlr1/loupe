"""--effort chooses reasoning effort for one new conversation."""

from __future__ import annotations

import io
from pathlib import Path
from typing import cast

import pytest
from test_shell import Client, Menu, calls, output, start
from test_shell import client as client
from test_shell import menu as menu

from llm_cli.cli import app, shell
from llm_cli.coordination.models import EFFORT_LEVELS
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.protocol.client import DaemonClient
from llm_cli.providers.accounts import load_preference, save_preference


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--effort", "high"], "high"),
        (["chat", "--effort", "xhigh"], "xhigh"),
        (["--effort", "low", "chat"], "low"),
        (["chat"], None),
    ],
)
def test_effort_flag_parses_globally_and_for_chat(
    argv: list[str], expected: str | None
) -> None:
    assert app.build_parser().parse_args(argv).effort == expected


def test_effort_choices_cover_every_level_and_reject_others() -> None:
    assert set(app._EFFORT_CHOICES) == EFFORT_LEVELS | {"default"}
    with pytest.raises(SystemExit):
        app.build_parser().parse_args(["--effort", "extreme"])


def test_global_effort_survives_default_chat_selection(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TerminalInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    selected: list[str | None] = []
    monkeypatch.setattr(app.sys, "stdin", TerminalInput())
    monkeypatch.setattr(app, "DaemonClient", lambda paths: client)
    monkeypatch.setattr(
        app, "run_session", lambda *args, **kw: selected.append(kw["effort"])
    )
    app.main(["--effort", "max"])
    assert selected == ["max"]


def test_effort_flag_is_not_silently_ignored_on_other_commands(
    client: Client,
) -> None:
    args = app.build_parser().parse_args(["--effort", "high", "task", "list"])
    with pytest.raises(LlmCoordError, match="--effort is available for chat"):
        app.dispatch(args, cast(DaemonClient, client))
    assert client.calls == []


@pytest.mark.parametrize(("flag", "expected"), [("low", "low"), ("default", None)])
def test_explicit_effort_overrides_saved_choice_for_this_conversation_only(
    client: Client, menu: Menu, flag: str, expected: str | None
) -> None:
    save_preference(client.paths, "codex", "gpt-6-sol", "xhigh")
    menu.ready.add("codex")
    chat = start(client, "Inspect\n/exit\n", effort=flag)
    assert chat.effort == expected
    chat.run()
    assert calls(client, "session.open")[0].get("effort") == expected
    assert load_preference(client.paths)["effort"] == "xhigh"


def test_unsupported_explicit_effort_fails_before_any_work(
    client: Client, menu: Menu
) -> None:
    menu.ready.add("codex")
    with pytest.raises(LlmCoordError) as raised:
        start(client, provider="codex", model="gpt-6-sol", effort="ultra")
    assert raised.value.code is ErrorCode.CONFIG_INVALID
    assert "Effort levels for gpt-6-sol: low, medium, high, xhigh, max" in str(
        raised.value.message
    )
    assert client.calls == []


def test_explicit_effort_cannot_silently_change_a_resumed_conversation(
    client: Client, tmp_path: Path
) -> None:
    with pytest.raises(LlmCoordError, match="use /effort after resuming"):
        shell.ChatShell(
            cast(DaemonClient, client),
            repository=tmp_path,
            scopes=["*"],
            provider=None,
            model=None,
            effort="high",
            resume_session_id="session-1",
            workspace_mode=None,
            stdin=io.StringIO(),
            stream=io.StringIO(),
            plain=True,
        )
    assert client.calls == []


def test_explicit_effort_without_an_account_says_how_to_apply_it(
    client: Client, menu: Menu
) -> None:
    chat = start(client, effort="high")
    assert chat.provider is None
    assert chat.effort is None
    assert "--effort needs a connected AI" in output(chat)
