"""The local lobby starts immediately and connects durable work only on demand."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from llm_cli.build import code_identity
from llm_cli.cli import session, shell
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient
from llm_cli.providers.accounts import load_preference, save_preference
from llm_cli.providers.catalog import ModelOption


class Client:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tasks: list[dict[str, Any]] = []
        self.sessions: dict[str, dict[str, Any]] = {}
        self.repository_error: LlmCoordError | None = None
        identity = code_identity()
        self.daemon_identity: dict[str, Any] = {
            "code_fingerprint": identity["fingerprint"],
            "code_path": identity["path"],
        }

    def call(
        self, method: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        params = params or {}
        self.calls.append((method, params))
        if method == "system.ping":
            return self.daemon_identity
        if method == "repo.status":
            if self.repository_error:
                raise self.repository_error
            return {}
        if method == "session.open":
            record = {
                "provider": params["provider"],
                "model": params["model"] or "default-model",
                "effort": params.get("effort"),
                "agent_mode": params.get("mode", "auto"),
            }
            self.sessions[params["session_id"]] = record
            return {"session": record, "bootstrap_sequence": 1}
        if method == "session.ack":
            return {"cursor": {"transport_received_sequence": params["sequence"]}}
        if method == "task.list":
            return self.tasks
        if method == "task.show":
            return next(
                task for task in self.tasks if task["task_id"] == params["task_id"]
            )
        if method == "task.run":
            self.tasks.append({**params, "state": "completed"})
            return {}
        if method == "session.events":
            return []
        if method in {"repo.add", "session.set_intent", "session.close"}:
            return {}
        raise AssertionError(f"Unexpected daemon call: {method}")

    def stream(self, method: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        assert method == "task.attach"
        return iter(())


class Menu:
    def __init__(self, ready: set[str]) -> None:
        self.ready = ready
        self.choices: list[str | None] = []
        self.calls: list[tuple[str | None, bool]] = []
        self.logouts: list[str] = []
        self.logout_failed = False

    def choose(self, provider: str | None = None, *, login: bool = False) -> str | None:
        self.calls.append((provider, login))
        selected = self.choices.pop(0)
        if selected:
            self.ready.add(selected)
        return selected

    def status(self) -> None:
        pass

    def logout(self, provider: str) -> str | None:
        self.logouts.append(provider)
        if self.logout_failed:
            return None
        self.ready.discard(provider)
        return provider


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
def menu(monkeypatch: pytest.MonkeyPatch) -> Menu:
    ready: set[str] = set()
    result = Menu(ready)
    monkeypatch.setattr(shell, "ConnectionMenu", lambda *args, **kwargs: result)
    monkeypatch.setattr(
        shell,
        "account_status",
        lambda paths, provider: {
            "provider": provider,
            "authenticated": provider in ready,
        },
    )
    return result


def start(
    client: Client,
    source: str = "",
    *,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> shell.ChatShell:
    return shell.ChatShell(
        cast(DaemonClient, client),
        repository=client.paths.state_dir.parent,
        scopes=["*"],
        provider=provider,
        model=model,
        effort=effort,
        resume_session_id=None,
        workspace_mode=None,
        stdin=io.StringIO(source),
        stream=io.StringIO(),
        plain=True,
    )


def calls(client: Client, method: str) -> list[dict[str, Any]]:
    return [params for called, params in client.calls if called == method]


def output(chat: shell.ChatShell) -> str:
    assert isinstance(chat.stream, io.StringIO)
    return chat.stream.getvalue()


def test_login_help_and_exit_lobby_needs_no_daemon_or_login(
    client: Client, menu: Menu
) -> None:
    chat = start(client, "/help\n/status\n/changes\n/tasks\n/history\n/clear\n/exit\n")
    assert chat.run() == 0
    assert client.calls == []
    assert menu.calls == []
    assert "/login" in output(chat)
    assert "Leaving Loupe" in output(chat)
    assert not client.paths.state_dir.exists()


@pytest.mark.parametrize("active", [False, True], ids=["idle", "active"])
def test_double_ctrl_c_exits_and_preserves_only_active_sessions(
    client: Client, menu: Menu, monkeypatch: pytest.MonkeyPatch, active: bool
) -> None:
    chat = start(client, provider="test", model="model")
    assert chat._ensure_session()
    assert chat.credentials is not None
    session_id = chat.credentials.session_id
    if active:
        client.tasks = [
            {"task_id": "live", "session_id": session_id, "state": "running"}
        ]

    def interrupt() -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(chat.composer, "read", interrupt)
    assert chat.run() == 0
    assert "Ctrl+C again" in output(chat)
    assert "Leaving Loupe" in output(chat)
    assert bool(calls(client, "session.close")) is not active
    assert not calls(client, "task.cancel")
    if active:
        assert f"--resume {session_id}" in output(chat)
        assert session.session_resume_secret(client.paths, session_id)


@pytest.mark.parametrize(
    ("command", "provider", "expected"),
    [
        ("/login", "codex", (None, True)),
        ("/login anthropic", "anthropic", ("anthropic", True)),
        ("/provider", "openai", (None, False)),
    ],
)
def test_provider_choice_happens_inside_cli_and_is_remembered(
    client: Client,
    menu: Menu,
    command: str,
    provider: str,
    expected: tuple[str | None, bool],
) -> None:
    menu.choices = [provider]
    chat = start(client, command + "\n/exit\n")
    chat.run()
    assert menu.calls == [expected]
    assert client.calls == []
    assert load_preference(client.paths) == {"provider": provider, "model": None}
    next_chat = start(client, "/exit\n")
    assert next_chat.provider == provider
    next_chat.run()
    assert len(menu.calls) == 1


def test_declining_login_keeps_prompt_in_history_without_submitting(
    client: Client, menu: Menu
) -> None:
    menu.choices = [None]
    chat = start(client, "Please inspect this project\n/help\n/exit\n")
    chat.run()
    assert client.calls == []
    assert chat.composer.prompts == ["Please inspect this project"]
    assert "Use /login when you're ready" in output(chat)


@pytest.mark.parametrize("unregistered", [False, True])
def test_first_prompt_uses_saved_selection_and_registers_only_when_needed(
    client: Client, menu: Menu, unregistered: bool
) -> None:
    save_preference(client.paths, "openai", "saved-model")
    menu.ready.add("openai")
    if unregistered:
        client.repository_error = LlmCoordError(
            ErrorCode.REPOSITORY_NOT_FOUND, "not registered"
        )
    chat = start(client, "Inspect the project\n/exit\n")
    assert client.calls == []
    chat.run()
    assert menu.calls == []
    assert len(calls(client, "repo.status")) == 1
    assert len(calls(client, "repo.add")) == int(unregistered)
    opened = calls(client, "session.open")
    assert len(opened) == 1
    assert (opened[0]["provider"], opened[0]["model"]) == ("openai", "saved-model")
    submitted = calls(client, "task.run")
    assert len(submitted) == 1
    assert submitted[0]["session_id"] == opened[0]["session_id"]
    assert (submitted[0]["provider"], submitted[0]["model"]) == (
        "openai",
        "saved-model",
    )
    assert not client.paths.session_secret_file(opened[0]["session_id"]).exists()


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("codex", None, ("codex", "saved-model", "xhigh")),
        ("codex", "saved-model", ("codex", "saved-model", "xhigh")),
        ("codex", "other-model", ("codex", "other-model", None)),
        ("openai", None, ("openai", None, None)),
    ],
)
def test_explicit_provider_keeps_saved_model_and_effort_only_when_they_match(
    client: Client,
    menu: Menu,
    provider: str,
    model: str | None,
    expected: tuple[str, str | None, str | None],
) -> None:
    save_preference(client.paths, "codex", "saved-model", "xhigh")
    menu.ready.add(provider)
    chat = start(client, "Inspect the project\n/exit\n", provider=provider, model=model)
    assert (chat.provider, chat.model, chat.effort) == expected
    chat.run()
    opened = calls(client, "session.open")
    assert len(opened) == 1
    assert opened[0].get("effort") == expected[2]


@pytest.mark.parametrize(
    ("daemon", "expected"),
    [
        ({"code_path": "/elsewhere/llm_cli"}, "running Loupe from /elsewhere/llm_cli"),
        ({}, "still running code from before your last update"),
        (None, "running an older version of Loupe"),
    ],
    ids=["other-installation", "stale-code", "older-daemon"],
)
def test_daemon_running_different_code_is_reported_once(
    client: Client,
    menu: Menu,
    daemon: dict[str, str] | None,
    expected: str,
) -> None:
    menu.ready.add("openai")
    if daemon is None:
        client.daemon_identity = {}
    else:
        client.daemon_identity = {
            "code_fingerprint": "0" * 64,
            "code_path": daemon.get("code_path", client.daemon_identity["code_path"]),
        }
    chat = start(client, "First\n/changes\nSecond\n/exit\n", provider="openai")
    chat.run()
    assert output(chat).count(expected) == 1
    assert "loupe daemon restart" in output(chat)
    assert len(calls(client, "system.ping")) == 1
    assert len(calls(client, "task.run")) == 2


def test_daemon_running_this_code_is_not_reported(client: Client, menu: Menu) -> None:
    menu.ready.add("openai")
    chat = start(client, "Inspect\n/exit\n", provider="openai")
    chat.run()
    assert "background service" not in output(chat)
    assert len(calls(client, "system.ping")) == 1


def test_unreachable_daemon_identity_does_not_block_the_prompt(
    client: Client, menu: Menu, monkeypatch: pytest.MonkeyPatch
) -> None:
    menu.ready.add("openai")
    original = client.call

    def call(method: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        if method == "system.ping":
            raise LlmCoordError(ErrorCode.DAEMON_UNAVAILABLE, "not running")
        return original(method, params, **kw)

    monkeypatch.setattr(client, "call", call)
    chat = start(client, "Inspect\n/exit\n", provider="openai")
    chat.run()
    assert "background service" not in output(chat)
    assert len(calls(client, "task.run")) == 1


def test_explicit_provider_ignores_a_broken_saved_preference(
    client: Client, menu: Menu, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise LlmCoordError(ErrorCode.CONFIG_INVALID, "saved preference is malformed")

    monkeypatch.setattr(shell, "load_preference", broken)
    chat = start(client, provider="codex", model="chosen-model")
    assert (chat.provider, chat.model, chat.effort) == ("codex", "chosen-model", None)
    assert "need attention" not in output(chat)


@pytest.mark.parametrize(
    "failure",
    [
        LlmCoordError(
            ErrorCode.REPOSITORY_NOT_FOUND,
            "ambiguous",
            {"registered_targets": ["main", "release"]},
        ),
        LlmCoordError(ErrorCode.REPOSITORY_UNSAFE, "not a Git repository"),
    ],
)
def test_repository_failures_preserve_lobby_without_registering_another_target(
    client: Client, menu: Menu, failure: LlmCoordError
) -> None:
    client.repository_error = failure
    chat = start(client, "Inspect\n/help\n/exit\n", provider="test")
    chat.run()
    assert not calls(client, "repo.add")
    assert not calls(client, "session.open")
    assert not calls(client, "task.run")
    assert failure.message in output(chat)
    assert "/cd PATH" in output(chat)
    assert "Leaving Loupe" in output(chat)


def test_cd_outside_git_keeps_login_and_help_available(
    client: Client, menu: Menu
) -> None:
    project = client.paths.state_dir.parent / "empty project"
    project.mkdir()
    menu.choices = ["anthropic"]
    chat = start(client, f'/cd "{project}"\n/login\n/help\n/exit\n')
    chat.run()
    assert chat.repository == project
    assert chat.provider == "anthropic"
    assert client.calls == []
    assert menu.calls == [(None, True)]


@pytest.mark.parametrize(
    ("change", "next_provider", "next_model"),
    [
        ("/model second-model", "codex", "second-model"),
        ("/provider openai", "openai", "default-model"),
    ],
)
def test_switching_provider_or_model_closes_idle_session_and_opens_fresh_context(
    client: Client, menu: Menu, change: str, next_provider: str, next_model: str
) -> None:
    menu.ready.update({"codex", "openai"})
    menu.choices = ["openai"]
    chat = start(
        client,
        f"First prompt\n{change}\nSecond prompt\n/exit\n",
        provider="codex",
        model="first-model",
    )
    chat.run()
    opened = calls(client, "session.open")
    assert len(opened) == 2
    assert opened[0]["session_id"] != opened[1]["session_id"]
    assert opened[0]["provider"] == "codex"
    submitted = calls(client, "task.run")
    assert [(task["provider"], task["model"]) for task in submitted] == [
        ("codex", "first-model"),
        (next_provider, next_model),
    ]
    assert [task["session_id"] for task in submitted] == [
        record["session_id"] for record in opened
    ]
    closed = calls(client, "session.close")
    assert [record["session_id"] for record in closed] == [
        record["session_id"] for record in opened
    ]
    methods = [method for method, _ in client.calls]
    assert methods.index("session.close") < methods.index(
        "session.open", methods.index("session.open") + 1
    )
    assert "fresh conversation" in output(chat)
    assert load_preference(client.paths) == {
        "provider": next_provider,
        "model": next_model if change.startswith("/model") else None,
    }


@pytest.mark.parametrize(
    "command",
    [
        "/provider openai",
        "/login",
        "/logout",
        "/model another-model",
        "/model",
        "/effort",
        "/effort high",
    ],
)
def test_any_active_own_task_blocks_switches_and_logout_even_if_last_task_finished(
    client: Client, menu: Menu, command: str
) -> None:
    menu.ready.add("codex")
    chat = start(client, provider="codex", model="first-model")
    assert chat._ensure_session()
    assert chat.credentials is not None
    credentials = chat.credentials
    client.tasks = [
        {
            "task_id": "completed-last",
            "state": "completed",
            "session_id": credentials.session_id,
        },
        {
            "task_id": "active-older",
            "state": "running",
            "session_id": credentials.session_id,
        },
    ]
    chat.last_task = "completed-last"
    with pytest.raises(LlmCoordError) as caught:
        chat._command(command)
    assert caught.value.code is ErrorCode.TASK_NOT_MUTABLE
    assert chat.credentials == credentials
    assert (chat.provider, chat.model) == ("codex", "first-model")
    assert menu.calls == []
    assert menu.logouts == []
    assert calls(client, "session.close") == []
    assert (
        session.session_resume_secret(client.paths, credentials.session_id)
        == credentials.resume_secret
    )


def test_active_other_sessions_do_not_block_an_idle_provider_change(
    client: Client, menu: Menu
) -> None:
    menu.ready.update({"codex", "openai"})
    menu.choices = ["openai"]
    chat = start(client, provider="codex")
    assert chat._ensure_session()
    client.tasks = [
        {"task_id": "other-task", "state": "running", "session_id": "other-session"}
    ]
    chat._command("/provider openai")
    assert chat.provider == "openai"
    assert chat.credentials is None
    assert len(calls(client, "session.close")) == 1


@pytest.mark.parametrize(
    ("provider", "alias"),
    [("codex", "chatgpt"), ("codex", "CODEX"), ("anthropic", "claude")],
)
def test_logout_alias_closes_the_matching_idle_conversation(
    client: Client, menu: Menu, provider: str, alias: str
) -> None:
    menu.ready.add(provider)
    chat = start(client, provider=provider)
    assert chat._ensure_session()
    assert chat.credentials is not None
    session_id = chat.credentials.session_id
    chat._command(f"/logout {alias}")
    assert chat.credentials is None
    assert chat.provider is None
    assert menu.logouts == [provider]
    assert calls(client, "session.close")[0]["session_id"] == session_id
    assert not client.paths.session_secret_file(session_id).exists()


def test_failed_logout_keeps_the_selected_account(client: Client, menu: Menu) -> None:
    menu.ready.add("codex")
    menu.logout_failed = True
    chat = start(client, provider="codex", model="selected-model")
    chat._command("/logout chatgpt")
    assert (chat.provider, chat.model) == ("codex", "selected-model")
    assert menu.ready == {"codex"}
    assert menu.logouts == ["codex"]


def test_unknown_logout_provider_does_not_close_current_conversation(
    client: Client, menu: Menu
) -> None:
    menu.ready.add("codex")
    chat = start(client, provider="codex")
    assert chat._ensure_session()
    credentials = chat.credentials
    with pytest.raises(LlmCoordError):
        chat._command("/logout unknown-provider")
    assert chat.credentials == credentials
    assert calls(client, "session.close") == []
    assert menu.logouts == []


def test_broken_saved_preference_and_account_do_not_lock_user_out_of_login(
    client: Client, menu: Menu, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise LlmCoordError(
            ErrorCode.PROVIDER_UNAVAILABLE, "saved account is malformed"
        )

    monkeypatch.setattr(shell, "load_preference", broken)
    monkeypatch.setattr(shell, "account_status", broken)
    chat = start(client, "/help\n/exit\n")
    assert chat.run() == 0
    assert client.calls == []
    assert "/login" in output(chat)


class Models:
    def __init__(self, option: ModelOption | None, effort: str | None) -> None:
        self.option = option
        self.effort = effort
        self.model_calls: list[tuple[str, str | None, bool]] = []
        self.effort_calls: list[tuple[ModelOption, str | None]] = []

    def choose_model(
        self, provider: str, current_model: str | None = None, *, refresh: bool = False
    ) -> ModelOption | None:
        self.model_calls.append((provider, current_model, refresh))
        return self.option

    def choose_effort(
        self, option: ModelOption, current_effort: str | None = None
    ) -> str | None:
        self.effort_calls.append((option, current_effort))
        return self.effort


@pytest.mark.parametrize("provider", ["codex", "openai", "anthropic"])
def test_account_model_and_effort_picker_persist_and_reach_new_session(
    client: Client, menu: Menu, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    menu.ready.add(provider)
    option = ModelOption("account-model", "Account model", efforts=("low", "high"))
    picker = Models(option, "high")
    monkeypatch.setattr(shell, "ModelMenu", lambda *args, **kwargs: picker)
    chat = start(client, provider=provider)
    chat._command("/model --refresh")
    assert picker.model_calls[0][0] == provider
    assert picker.model_calls[0][2] is True
    assert (chat.model, chat.effort) == ("account-model", "high")
    assert client.calls == []
    assert load_preference(client.paths) == {
        "provider": provider,
        "model": "account-model",
        "effort": "high",
    }
    reopened = start(client)
    assert (reopened.model, reopened.effort) == ("account-model", "high")
    assert reopened._ensure_session()
    assert calls(client, "session.open")[0]["effort"] == "high"
    assert reopened.credentials is not None
    assert reopened.credentials.effort == "high"
    reopened._command("/status")
    assert "effort: high" in output(reopened)


@pytest.mark.parametrize("cancel_model", [False, True])
def test_cancelling_model_or_effort_keeps_current_conversation_and_preference(
    client: Client, menu: Menu, monkeypatch: pytest.MonkeyPatch, cancel_model: bool
) -> None:
    menu.ready.add("openai")
    save_preference(client.paths, "openai", "old-model", effort="high")
    option = ModelOption("new-model", "New model", efforts=("low", "high"))
    picker = Models(None if cancel_model else option, None)
    monkeypatch.setattr(shell, "ModelMenu", lambda *args, **kwargs: picker)
    chat = start(client)
    assert chat._ensure_session()
    credentials = chat.credentials
    chat._command("/model")
    assert chat.credentials == credentials
    assert calls(client, "session.close") == []
    assert load_preference(client.paths) == {
        "provider": "openai",
        "model": "old-model",
        "effort": "high",
    }


def test_nonreasoning_selection_drops_previous_effort_without_showing_effort_picker(
    client: Client, menu: Menu, monkeypatch: pytest.MonkeyPatch
) -> None:
    menu.ready.add("openai")
    save_preference(client.paths, "openai", "reasoning-model", effort="high")
    picker = Models(ModelOption("gpt-4.1", "GPT-4.1"), None)
    monkeypatch.setattr(shell, "ModelMenu", lambda *args, **kwargs: picker)
    chat = start(client)
    chat._command("/model")
    assert chat.model == "gpt-4.1"
    assert chat.effort is None
    assert picker.effort_calls == []
    assert "effort" not in load_preference(client.paths)


def test_effort_validates_for_current_model_and_starts_a_fresh_idle_session(
    client: Client, menu: Menu, monkeypatch: pytest.MonkeyPatch
) -> None:
    menu.ready.add("openai")
    option = ModelOption("account-model", "Account model", efforts=("low", "high"))
    monkeypatch.setattr(shell, "model_option", lambda *args, **kwargs: option)
    chat = start(client, provider="openai", model="account-model")
    assert chat._ensure_session()
    credentials = chat.credentials
    with pytest.raises(ValueError, match="Effort levels"):
        chat._command("/effort ultra")
    assert chat.credentials == credentials
    assert calls(client, "session.close") == []
    chat._command("/effort high")
    assert chat.effort == "high"
    assert chat.credentials is None
    assert len(calls(client, "session.close")) == 1
    assert chat._ensure_session()
    assert [call.get("effort") for call in calls(client, "session.open")] == [
        None,
        "high",
    ]
    chat._command("/effort default")
    assert chat.effort is None
    assert "effort" not in load_preference(client.paths)


def test_no_connected_provider_keeps_model_and_effort_commands_local(
    client: Client, menu: Menu
) -> None:
    chat = start(client, "/model\n/effort\n/exit\n")
    assert chat.run() == 0
    assert client.calls == []
    assert "Choose an AI" in output(chat)


def test_viewing_unknown_effort_capabilities_does_not_clear_saved_choice(
    client: Client, menu: Menu, monkeypatch: pytest.MonkeyPatch
) -> None:
    menu.ready.add("codex")
    save_preference(client.paths, "codex", "new-model", effort="high")
    option = ModelOption("new-model", "New model")
    picker = Models(option, "default")
    monkeypatch.setattr(shell, "ModelMenu", lambda *args, **kwargs: picker)
    monkeypatch.setattr(shell, "model_option", lambda *args, **kwargs: option)
    chat = start(client)
    chat._command("/effort")
    assert chat.effort == "high"
    assert load_preference(client.paths)["effort"] == "high"
    assert client.calls == []


def test_selecting_current_explicit_model_still_saves_the_choice(
    client: Client, menu: Menu
) -> None:
    chat = start(client, provider="codex", model="chosen-model")
    chat._command("/model chosen-model")
    assert load_preference(client.paths) == {
        "provider": "codex",
        "model": "chosen-model",
    }
    assert client.calls == []


def test_reauthentication_preserves_current_session_settings_over_older_preferences(
    client: Client, menu: Menu, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_preference(client.paths, "codex", "different-model")
    menu.ready.add("anthropic")
    chat = start(client, provider="anthropic", model="resumed-model")
    chat.effort = "high"
    assert chat._ensure_session()
    credentials = chat.credentials

    def reconnect(*args: Any, **kwargs: Any) -> str:
        save_preference(client.paths, "anthropic")
        return "anthropic"

    monkeypatch.setattr(menu, "choose", reconnect)
    chat._command("/login anthropic")
    assert chat.credentials == credentials
    assert calls(client, "session.close") == []
    assert load_preference(client.paths) == {
        "provider": "anthropic",
        "model": "resumed-model",
        "effort": "high",
    }
