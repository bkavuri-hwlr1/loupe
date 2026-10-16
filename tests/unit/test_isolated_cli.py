"""Isolated runtime consent is explicit, exact, and limited to one session."""

from __future__ import annotations

import io
from typing import Any, cast

import pytest
from test_shell import Client, Menu, calls, output
from test_shell import client as client
from test_shell import menu as menu

from llm_cli.cli import app, session, shell
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.protocol.client import DaemonClient


class IsolatedClient(Client):
    def call(
        self, method: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        result = super().call(method, params, **kwargs)
        if method == "session.open":
            assert params is not None
            result["session"]["workspace_mode"] = params["workspace"]
            result["approved_runtime_files"] = params.get("approved_runtime_files", [])
            result["workspace_notices"] = ["node_modules needs a rebuild; left absent."]
        return result


def test_session_open_passes_exact_consent_and_returns_hydration_notice(
    client: Client,
) -> None:
    daemon = IsolatedClient(client.paths)
    args = app.build_parser().parse_args(
        [
            "session",
            "open",
            "--provider",
            "test",
            "--workspace",
            "isolated",
            "--approve-secret",
            "config/.env",
            "--approve-secret",
            ".env",
            "--approve-secret",
            ".env",
        ]
    )
    result = app.dispatch(args, cast(DaemonClient, daemon))
    assert calls(daemon, "session.open")[0]["approved_runtime_files"] == [
        ".env",
        "config/.env",
    ]
    assert result["workspace_mode"] == "isolated"
    assert result["workspace_notices"] == ["node_modules needs a rebuild; left absent."]


@pytest.mark.parametrize(
    "workspace,resume", [("shared", None), (None, None), ("isolated", "old-session")]
)
def test_secret_consent_cannot_be_ignored_on_shared_or_resumed_sessions(
    client: Client, workspace: str | None, resume: str | None
) -> None:
    with pytest.raises(LlmCoordError, match="requires a new --workspace isolated"):
        session.open_or_resume_session(
            cast(DaemonClient, client),
            repository=client.paths.state_dir.parent,
            provider="test",
            model="test",
            resume_session_id=resume,
            workspace_mode=workspace,
            approved_runtime_files=(".env",),
        )
    assert not client.calls
    assert not client.paths.state_dir.exists()


@pytest.mark.parametrize("path", ["*", "../.env", "/tmp/.env", "config/", "a\0b"])
def test_runtime_consent_rejects_scopes_and_unsafe_paths_before_open(
    client: Client, path: str
) -> None:
    with pytest.raises(LlmCoordError):
        session.open_or_resume_session(
            cast(DaemonClient, client),
            repository=client.paths.state_dir.parent,
            provider="test",
            model="test",
            resume_session_id=None,
            workspace_mode="isolated",
            approved_runtime_files=(path,),
        )
    assert not client.calls


@pytest.mark.parametrize("confirmation", ["workspace", "consent"])
def test_old_daemon_cannot_silently_ignore_isolation_or_consent(
    client: Client, confirmation: str
) -> None:
    class OldDaemon(IsolatedClient):
        def call(
            self, method: str, params: dict[str, Any] | None = None, **kwargs: Any
        ) -> Any:
            result = super().call(method, params, **kwargs)
            if method == "session.open":
                if confirmation == "workspace":
                    result["session"].pop("workspace_mode")
                else:
                    result.pop("approved_runtime_files")
            return result

    daemon = OldDaemon(client.paths)
    with pytest.raises(LlmCoordError) as caught:
        session.open_or_resume_session(
            cast(DaemonClient, daemon),
            repository=client.paths.state_dir.parent,
            provider="test",
            model="test",
            resume_session_id=None,
            workspace_mode="isolated",
            approved_runtime_files=(".env",),
        )
    assert caught.value.code is ErrorCode.PROTOCOL_MISMATCH
    opened = calls(daemon, "session.open")[0]
    assert session.session_resume_secret(client.paths, opened["session_id"])
    assert not calls(daemon, "session.ack")
    assert not calls(daemon, "task.run")


def test_chat_reports_omissions_and_does_not_reuse_consent_after_model_change(
    client: Client, menu: Menu
) -> None:
    daemon = IsolatedClient(client.paths)
    chat = shell.ChatShell(
        cast(DaemonClient, daemon),
        repository=client.paths.state_dir.parent,
        scopes=["*"],
        provider="test",
        model="first",
        resume_session_id=None,
        workspace_mode="isolated",
        approved_runtime_files=(".env",),
        stdin=io.StringIO(),
        stream=io.StringIO(),
        plain=True,
    )
    assert chat._ensure_session()
    assert "node_modules needs a rebuild; left absent." in output(chat)
    chat._switch("test", "second")
    assert chat._ensure_session()
    opens = calls(daemon, "session.open")
    assert opens[0]["approved_runtime_files"] == [".env"]
    assert "approved_runtime_files" not in opens[1]


def test_chat_cli_passes_runtime_consent(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[dict[str, Any]] = []
    monkeypatch.setattr(
        app, "run_session", lambda *args, **kwargs: requested.append(kwargs)
    )
    args = app.build_parser().parse_args(
        ["chat", "--workspace", "isolated", "--approve-secret", ".env"]
    )
    app.dispatch(args, cast(DaemonClient, client))
    assert requested[0]["approved_runtime_files"] == (".env",)
