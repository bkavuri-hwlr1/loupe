from __future__ import annotations

import getpass
import json
import warnings
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_cli.cli import auth
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.providers import accounts, anthropic_provider, openai_provider


def _paths(tmp_path: Path, profile: str = "test") -> AppPaths:
    return AppPaths.resolve(profile, environ={}, home=tmp_path)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_credentials_private_profile_local_and_not_exposed(
    tmp_path: Path, provider: str
) -> None:
    first, second = _paths(tmp_path, "first"), _paths(tmp_path, "second")
    secret = "test-private-key-123"
    assert accounts.account_status(first, provider)["authenticated"] is False
    assert not first.state_dir.exists()
    result = accounts.save_api_key(first, provider, secret)
    path = first.state_dir / "auth" / f"{provider}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert accounts.load_api_key(first, provider) == secret
    assert accounts.load_api_key(second, provider) is None
    assert result == {
        "provider": provider,
        "billing": "api",
        "authenticated": True,
        "source": "saved",
    }
    assert secret not in json.dumps(result)
    assert secret not in json.dumps(accounts.list_accounts(first))
    assert accounts.logout_account(first, provider)["authenticated"] is False
    assert not path.exists()


@pytest.mark.parametrize("unsafe", ["symlink", "permissions", "hardlink", "malformed"])
def test_unsafe_key_files_are_rejected_without_exposing_content(
    tmp_path: Path, unsafe: str
) -> None:
    paths = _paths(tmp_path)
    secret = "sensitive-test-credential"
    accounts.save_api_key(paths, "openai", secret)
    path = paths.state_dir / "auth" / "openai.json"
    if unsafe == "symlink":
        other = path.with_suffix(".other")
        path.rename(other)
        path.symlink_to(other)
    elif unsafe == "permissions":
        path.chmod(0o644)
    elif unsafe == "hardlink":
        path.with_suffix(".other").hardlink_to(path)
    else:
        path.write_text(secret)
    with pytest.raises(LlmCoordError) as failure:
        accounts.load_api_key(paths, "openai")
    assert secret not in str(failure.value)


@pytest.mark.parametrize("invalid", ["", "spaces in key", "key\nnewline", "x" * 8193])
def test_invalid_keys_are_not_saved_or_echoed(tmp_path: Path, invalid: str) -> None:
    paths = _paths(tmp_path)
    with pytest.raises(LlmCoordError) as failure:
        accounts.save_api_key(paths, "openai", invalid)
    assert not paths.state_dir.exists()
    if invalid:
        assert invalid not in str(failure.value)


def test_saved_login_overrides_daemon_environment_and_logout_keeps_env_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "old-process-key")
    assert accounts.account_status(paths, "openai")["source"] == "environment"
    accounts.save_api_key(paths, "openai", "new-login-key")
    assert accounts.load_api_key(paths, "openai") == "new-login-key"
    assert accounts.logout_account(paths, "openai") == {
        "provider": "openai",
        "billing": "api",
        "authenticated": True,
        "source": "environment",
    }
    assert accounts.load_api_key(paths, "openai") == "old-process-key"
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
    assert accounts.account_status(paths, "anthropic")["authenticated"] is True


def test_preference_private_profile_local_and_separate_from_config(
    tmp_path: Path,
) -> None:
    paths, other = _paths(tmp_path, "first"), _paths(tmp_path, "second")
    assert accounts.load_preference(paths) == {}
    paths.config_dir.mkdir(parents=True)
    paths.config_file.write_text("default_provider = 'anthropic'\n")
    before = paths.config_file.read_bytes()
    accounts.save_preference(paths, "openai", "model-test")
    assert accounts.load_preference(paths) == {
        "provider": "openai",
        "model": "model-test",
    }
    assert accounts.load_preference(other) == {}
    assert (paths.state_dir / "preferences.json").stat().st_mode & 0o777 == 0o600
    assert paths.config_file.read_bytes() == before


def test_effort_preference_round_trip_and_old_preferences_remain_compatible(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    accounts.save_preference(paths, "openai", "test-model")
    assert accounts.load_preference(paths) == {
        "provider": "openai",
        "model": "test-model",
    }
    expected = {"provider": "openai", "model": "test-model", "effort": "high"}
    assert accounts.save_preference(paths, "openai", "test-model", "high") == expected
    assert accounts.load_preference(paths) == expected
    accounts.save_preference(paths, "openai", "test-model", None)
    assert "effort" not in accounts.load_preference(paths)


@pytest.mark.parametrize("invalid", ["unsupported", "high\n", "", 7, []])
def test_invalid_saved_effort_is_rejected(tmp_path: Path, invalid: object) -> None:
    paths = _paths(tmp_path)
    accounts.save_preference(paths, "openai", "test-model", "high")
    preference = paths.state_dir / "preferences.json"
    preference.write_text(
        json.dumps({"provider": "openai", "model": "test-model", "effort": invalid})
    )
    with pytest.raises(LlmCoordError, match="malformed"):
        accounts.load_preference(paths)


def test_invalid_effort_does_not_overwrite_preference(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    accounts.save_preference(paths, "openai", "test-model", "high")
    with pytest.raises(LlmCoordError, match="supported effort"):
        accounts.save_preference(paths, "openai", "test-model", "unsupported")
    assert accounts.load_preference(paths)["effort"] == "high"


def test_default_effort_is_canonicalized_when_saving_and_loading(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    expected = {"provider": "openai", "model": "test-model"}
    assert (
        accounts.save_preference(paths, "openai", "test-model", "default") == expected
    )
    assert accounts.load_preference(paths) == expected
    preference = paths.state_dir / "preferences.json"
    assert "effort" not in json.loads(preference.read_text())
    preference.write_text(json.dumps({**expected, "effort": "default"}))
    assert accounts.load_preference(paths) == expected


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_login_returns_metadata_and_remembers_provider_without_secret_output(
    tmp_path: Path, provider: str
) -> None:
    paths = _paths(tmp_path)
    notices: list[str] = []
    prompts: list[str] = []

    def read_secret(prompt: str) -> str:
        prompts.append(prompt)
        return "hidden-login-test-key"

    result = auth.login_provider(
        provider, paths, notify=notices.append, read_secret=read_secret
    )
    assert result["authenticated"] is True
    assert len(prompts) == 1 and "API key" in prompts[0]
    assert accounts.load_preference(paths)["provider"] == provider
    assert "hidden-login-test-key" not in json.dumps([result, notices, prompts])


@pytest.mark.parametrize("provider", ["codex", "anthropic", "openai"])
def test_reauthentication_preserves_selected_model(
    tmp_path: Path, provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    accounts.save_preference(paths, provider, "selected-model", "high")
    monkeypatch.setattr(
        auth, "browser_login", lambda *_: {"provider": "codex", "authenticated": True}
    )
    result = auth.login_provider(
        provider, paths, notify=lambda _: None, read_secret=lambda _: "replacement-key"
    )
    assert result["authenticated"] is True
    assert accounts.load_preference(paths) == {
        "provider": provider,
        "model": "selected-model",
        "effort": "high",
    }


def test_login_to_different_provider_resets_selected_model(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    accounts.save_preference(paths, "codex", "codex-specific-model", "high")
    auth.login_provider(
        "anthropic", paths, notify=lambda _: None, read_secret=lambda _: "new-key"
    )
    assert accounts.load_preference(paths) == {"provider": "anthropic", "model": None}


@pytest.mark.parametrize(
    "corrupt", ["{invalid JSON", '{"provider":"openai","model":42}']
)
def test_login_repairs_corrupt_preference(tmp_path: Path, corrupt: str) -> None:
    paths = _paths(tmp_path)
    accounts.save_preference(paths, "openai", "previous-model")
    (paths.state_dir / "preferences.json").write_text(corrupt)
    result = auth.login_provider(
        "openai", paths, notify=lambda _: None, read_secret=lambda _: "new-key"
    )
    assert result["authenticated"] is True
    assert accounts.load_preference(paths) == {"provider": "openai", "model": None}


@pytest.mark.parametrize("cancel", [KeyboardInterrupt, EOFError])
def test_cancelled_login_leaves_preference_and_credentials_untouched(
    tmp_path: Path, cancel: type[BaseException]
) -> None:
    paths = _paths(tmp_path)
    accounts.save_preference(paths, "codex")

    def interrupted(prompt: str) -> str:
        raise cancel

    result = auth.login_provider(
        "openai", paths, notify=lambda _: None, read_secret=interrupted
    )
    assert result == {"provider": "openai", "cancelled": True}
    assert accounts.load_preference(paths)["provider"] == "codex"
    assert accounts.load_api_key(paths, "openai") is None


def test_secret_input_never_falls_back_to_echoing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(prompt: str) -> str:
        warnings.warn("Cannot hide input", getpass.GetPassWarning, stacklevel=2)
        pytest.fail("must not continue into echoing input")

    monkeypatch.setattr(auth.getpass, "getpass", unavailable)
    with pytest.raises(LlmCoordError, match="hide your input"):
        auth._read_secret("API key: ")


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_existing_daemon_uses_new_login_for_each_new_task(
    tmp_path: Path,
    provider: str,
    service_factory: Callable[[Path], DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_factory(tmp_path)
    constructors: list[dict[str, object]] = []

    def construct(**kwargs: object) -> SimpleNamespace:
        constructors.append(kwargs)
        return SimpleNamespace(**kwargs)

    module = openai_provider if provider == "openai" else anthropic_provider
    sdk_name = "OpenAI" if provider == "openai" else "Anthropic"
    monkeypatch.setattr(
        module, "_load_sdk", lambda: SimpleNamespace(**{sdk_name: construct})
    )
    # A provider can even be constructed before login: first use reads the file.
    first_task = service.providers.create(provider, None)
    accounts.save_api_key(service.paths, provider, "first-test-key")
    first = first_task.session(system="test", tools=[])
    assert "first-test-key" not in json.dumps(first.snapshot())
    accounts.save_api_key(service.paths, provider, "replacement-test-key")
    second_task = service.providers.create(provider, None)
    second = second_task.session(system="test", tools=[])
    assert "replacement-test-key" not in json.dumps(second.snapshot())
    assert constructors == [
        {"api_key": "first-test-key"},
        {"api_key": "replacement-test-key"},
    ]
