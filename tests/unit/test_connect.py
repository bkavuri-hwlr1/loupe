"""Account selection stays usable without putting credentials in the transcript."""

from __future__ import annotations

import getpass
import io
import warnings
from collections.abc import Callable
from pathlib import Path

import pytest

from llm_cli.cli import auth, connect
from llm_cli.cli.terminal import TerminalUI
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.providers import accounts


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppPaths:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return AppPaths(
        "test",
        tmp_path / "config",
        tmp_path / "data",
        tmp_path / "state",
        tmp_path / "run",
    )


def menu(
    paths: AppPaths,
    stdin: io.StringIO | None = None,
    *,
    read_secret: Callable[[str], str] | None = None,
) -> tuple[connect.ConnectionMenu, io.StringIO]:
    stream = io.StringIO()
    ui = TerminalUI(stream, plain=True)
    return (
        connect.ConnectionMenu(
            paths,
            ui,
            stdin if stdin is not None else io.StringIO(),
            stream,
            plain=True,
            read_secret=read_secret,
        ),
        stream,
    )


@pytest.mark.parametrize("choice", ["", "\n", "0\n", "later\n"])
def test_picker_can_skip_without_a_saved_account(paths: AppPaths, choice: str) -> None:
    picker, stream = menu(paths, io.StringIO(choice))
    assert picker.choose() is None
    assert accounts.load_preference(paths) == {}
    assert "Connect whenever you're ready with /login" in stream.getvalue()
    assert "ChatGPT subscription" in stream.getvalue()
    assert "separate Anthropic API billing" in stream.getvalue()
    assert "separate OpenAI API billing" in stream.getvalue()
    assert "\x1b" not in stream.getvalue()


def test_named_picker_reuses_credentials_and_preserves_selected_model(
    paths: AppPaths,
) -> None:
    accounts.save_api_key(paths, "anthropic", "existing-private-key")
    accounts.save_preference(paths, "anthropic", "chosen-model", "high")
    picker, stream = menu(paths, io.StringIO("Claude\n"))
    assert picker.choose() == "anthropic"
    assert accounts.load_preference(paths) == {
        "provider": "anthropic",
        "model": "chosen-model",
        "effort": "high",
    }
    assert "Anthropic is ready" in stream.getvalue()
    assert "existing-private-key" not in stream.getvalue()


def test_numeric_picker_recovers_from_invalid_choice_and_saves_private_login(
    paths: AppPaths,
) -> None:
    prompts: list[str] = []

    def read_secret(prompt: str) -> str:
        prompts.append(prompt)
        return "test-openai-private-key"

    picker, stream = menu(
        paths, io.StringIO("bad-choice\n3\n"), read_secret=read_secret
    )
    assert picker.choose() == "openai"
    assert prompts == ["OpenAI API key: "]
    assert accounts.load_api_key(paths, "openai") == "test-openai-private-key"
    assert accounts.load_preference(paths)["provider"] == "openai"
    assert "test-openai-private-key" not in stream.getvalue()
    assert "Choose codex, anthropic, or openai" in stream.getvalue()


def test_explicit_login_replaces_an_existing_key(paths: AppPaths) -> None:
    accounts.save_api_key(paths, "openai", "old-key")
    picker, stream = menu(paths, read_secret=lambda prompt: "replacement-key")
    assert picker.choose("openai", login=True) == "openai"
    assert accounts.load_api_key(paths, "openai") == "replacement-key"
    assert "replacement-key" not in stream.getvalue()


@pytest.mark.parametrize("cancellation", ["blank", "eof", "interrupt"])
def test_login_cancellation_preserves_the_previous_choice(
    paths: AppPaths, cancellation: str
) -> None:
    accounts.save_preference(paths, "openai", "previous-model")

    def read_secret(prompt: str) -> str:
        if cancellation == "eof":
            raise EOFError
        if cancellation == "interrupt":
            raise KeyboardInterrupt
        return ""

    picker, stream = menu(paths, read_secret=read_secret)
    assert picker.choose("anthropic", login=True) is None
    assert accounts.load_preference(paths) == {
        "provider": "openai",
        "model": "previous-model",
    }
    assert not accounts.account_status(paths, "anthropic")["authenticated"]
    assert "Login cancelled" in stream.getvalue()


def test_api_login_never_consumes_a_key_or_command_from_piped_input(
    paths: AppPaths,
) -> None:
    source = io.StringIO("sensitive-piped-key\n/exit\n")
    picker, stream = menu(paths, source)
    assert picker.choose("anthropic", login=True) is None
    assert source.read() == "sensitive-piped-key\n/exit\n"
    assert not accounts.account_status(paths, "anthropic")["authenticated"]
    assert "private terminal prompt" in stream.getvalue()
    assert "sensitive-piped-key" not in stream.getvalue()


class TtyInput(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_tty_api_login_uses_getpass_outside_the_command_input_stream(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = TtyInput("/exit\n")
    picker, stream = menu(paths, source)

    def get_secret(prompt: str, *, stream: object) -> str:
        assert prompt == "Anthropic API key: "
        assert stream is picker.stream
        return "hidden-key"

    monkeypatch.setattr(connect.getpass, "getpass", get_secret)
    assert picker.choose("anthropic") == "anthropic"
    assert source.read() == "/exit\n"
    assert accounts.load_api_key(paths, "anthropic") == "hidden-key"
    assert "hidden-key" not in stream.getvalue()


def test_getpass_echo_fallback_is_rejected_before_reading_input(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    picker, stream = menu(paths, TtyInput("/exit\n"))

    def insecure_getpass(prompt: str, *, stream: object) -> str:
        warnings.warn(
            "Password input may be echoed.", getpass.GetPassWarning, stacklevel=2
        )
        pytest.fail("Echo fallback must never run")

    monkeypatch.setattr(connect.getpass, "getpass", insecure_getpass)
    assert picker.choose("openai") is None
    assert "cannot hide API key input" in stream.getvalue()
    assert not accounts.account_status(paths, "openai")["authenticated"]


def test_codex_login_uses_the_shared_browser_auth_flow(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[object] = []

    def browser_login(
        store: object, notify: Callable[[str], None]
    ) -> dict[str, object]:
        called.append(store)
        notify("Browser sign-in cancelled.")
        raise KeyboardInterrupt

    monkeypatch.setattr(auth, "browser_login", browser_login)
    picker, stream = menu(paths)
    assert picker.choose("ChatGPT", login=True) is None
    assert len(called) == 1
    assert "Browser sign-in cancelled" in stream.getvalue()
    assert accounts.load_preference(paths) == {}


def test_status_and_logout_never_render_key_values(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts.save_api_key(paths, "anthropic", "saved-private-key")
    accounts.save_preference(paths, "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-private-key")
    picker, stream = menu(paths)
    picker.status()
    assert picker.logout("claude") == "anthropic"
    text = stream.getvalue()
    assert "Anthropic · connected · selected" in text
    assert "Saved Anthropic login removed" in text
    assert "still connected through your environment" in text
    assert "saved-private-key" not in text
    assert "environment-private-key" not in text
    assert accounts.account_status(paths, "anthropic")["source"] == "environment"


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("account write refused"),
        LlmCoordError(ErrorCode.PROVIDER_UNAVAILABLE, "account is unavailable"),
    ],
)
def test_failed_logout_does_not_report_a_removed_account(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    accounts.save_api_key(paths, "anthropic", "saved-private-key")

    def refuse_logout(paths: AppPaths, provider: str) -> dict[str, object]:
        raise failure

    monkeypatch.setattr(accounts, "logout_account", refuse_logout)
    picker, stream = menu(paths)
    assert picker.logout("claude") is None
    assert "login removed" not in stream.getvalue()
    assert accounts.account_status(paths, "anthropic")["authenticated"]


def test_unknown_explicit_provider_does_not_consume_commands(paths: AppPaths) -> None:
    source = io.StringIO("/exit\n")
    picker, stream = menu(paths, source)
    assert picker.choose("unknown") is None
    assert source.read() == "/exit\n"
    assert "Choose codex, anthropic, or openai" in stream.getvalue()


def test_broken_account_does_not_block_picker_or_status_for_other_providers(
    paths: AppPaths,
) -> None:
    accounts.save_api_key(paths, "anthropic", "old-key")
    (paths.state_dir / "auth" / "anthropic.json").write_text("malformed-private-value")
    accounts.save_api_key(paths, "openai", "usable-key")
    picker, stream = menu(paths, io.StringIO("3\n"))
    assert picker.choose() == "openai"
    picker.status()
    assert "Anthropic · sign in again" in stream.getvalue()
    assert "OpenAI · connected · selected" in stream.getvalue()
    assert "malformed-private-value" not in stream.getvalue()


def test_reauthentication_repairs_a_broken_saved_account_and_preference(
    paths: AppPaths,
) -> None:
    accounts.save_api_key(paths, "anthropic", "old-key")
    accounts.save_preference(paths, "anthropic")
    (paths.state_dir / "auth" / "anthropic.json").write_text("broken credential")
    (paths.state_dir / "preferences.json").write_text("broken preference")
    picker, stream = menu(paths, read_secret=lambda prompt: "new-private-key")
    assert picker.choose("anthropic", login=True) == "anthropic"
    assert accounts.load_api_key(paths, "anthropic") == "new-private-key"
    assert accounts.load_preference(paths) == {"provider": "anthropic", "model": None}
    assert "new-private-key" not in stream.getvalue()


def test_selecting_a_usable_account_repairs_an_unreadable_preference(
    paths: AppPaths,
) -> None:
    accounts.save_api_key(paths, "openai", "existing-key")
    accounts.save_preference(paths, "anthropic")
    (paths.state_dir / "preferences.json").write_text("broken preference")
    picker, _ = menu(paths)
    assert picker.choose("openai") == "openai"
    assert accounts.load_preference(paths) == {"provider": "openai", "model": None}
