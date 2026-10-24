"""Workspace slash commands retain conversation credentials and confirm bootstrap."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from test_shell import Client, Menu, calls, output, start
from test_shell import client as client
from test_shell import menu as menu

from llm_cli.cli import session
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.protocol.client import DaemonClient


class SwitchClient(Client):
    target = "isolated"
    pending: dict[str, Any] | None = None

    def call(
        self, method: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        params = params or {}
        if method.startswith("workspace."):
            self.calls.append((method, params))
            if method == "workspace.switch_mode":
                self.target = params["workspace"]
                return {
                    "state": "confirming",
                    "workspace": {"mode": self.target},
                    "bootstrap_sequence": 7,
                }
            if method == "workspace.confirm_switch":
                return {
                    "state": "confirmed",
                    "session": {
                        "workspace_mode": self.target,
                        "workspace_binding_generation": 2,
                    },
                    "workspace_notices": ["Runtime remains private."],
                }
            if method == "workspace.rollback_switch":
                return {"state": "rolled_back"}
        if method == "session.resume":
            self.calls.append((method, params))
            return {
                "session": self.sessions[params["session_id"]],
                "cursor": {"transport_received_sequence": 1},
                "workspace_switch": self.pending,
            }
        return super().call(method, params, **kwargs)


def test_slash_switch_keeps_session_secret_and_updates_binding(
    client: Client,
    menu: Menu,
) -> None:
    daemon = SwitchClient(client.paths)
    menu.ready.add("test")
    chat = start(daemon, provider="test", model="first")
    assert chat._ensure_session()
    before = chat.credentials
    chat._workspace_command(["isolated", "--approve-secret", ".env"])
    assert chat.credentials.session_id == before.session_id
    assert chat.credentials.resume_secret == before.resume_secret
    assert chat.credentials.workspace_mode == "isolated"
    assert chat.credentials.workspace_binding_generation == 2
    request = calls(daemon, "workspace.switch_mode")[0]
    assert request["approved_runtime_files"] == [".env"]
    assert calls(daemon, "workspace.confirm_switch")[0]["sequence"] == 7
    assert "Runtime remains private." in output(chat)
    assert len(calls(daemon, "session.open")) == 1


def test_changed_runtime_disposition_is_explicit_in_rpc(
    client: Client,
    menu: Menu,
) -> None:
    daemon = SwitchClient(client.paths)
    menu.ready.add("test")
    chat = start(daemon, provider="test", model="first")
    chat._workspace_command(["shared", "--discard"])
    assert calls(daemon, "workspace.switch_mode")[0]["disposition"] == "discard"
    with pytest.raises(ValueError, match="Usage"):
        chat._workspace_command(["shared", "--retain", "--discard"])


def test_attention_resume_stays_usable_for_owner_rollback(client: Client) -> None:
    daemon = SwitchClient(client.paths)
    opened = session.open_or_resume_session(
        cast(DaemonClient, daemon),
        repository=client.paths.state_dir.parent,
        provider="test",
        model="first",
        resume_session_id=None,
    )
    daemon.pending = {
        "state": "attention_required",
        "switch_intent_id": "uncertain",
        "request_json": json.dumps({"target": "isolated"}),
    }
    resumed = session.open_or_resume_session(
        cast(DaemonClient, daemon),
        repository=client.paths.state_dir.parent,
        provider=None,
        model=None,
        resume_session_id=opened.session_id,
    )
    assert resumed.session_id == opened.session_id
    assert "/workspace rollback uncertain" in resumed.workspace_notices[0]
    assert not calls(daemon, "workspace.confirm_switch")


def test_old_daemon_cannot_skip_the_target_binding_confirmation(client: Client) -> None:
    class OldClient(SwitchClient):
        def call(
            self, method: str, params: dict[str, Any] | None = None, **kwargs: Any
        ) -> Any:
            value = super().call(method, params, **kwargs)
            if method == "workspace.confirm_switch":
                value["session"].pop("workspace_binding_generation")
            return value

    daemon = OldClient(client.paths)
    credentials = session._SessionCredentials("existing", "secret", "test", "first", 1)
    with pytest.raises(LlmCoordError) as caught:
        session.switch_workspace(cast(DaemonClient, daemon), credentials, "isolated")
    assert caught.value.code is ErrorCode.PROTOCOL_MISMATCH
