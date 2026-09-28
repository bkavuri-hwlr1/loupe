"""Local authentication commands. Credentials never travel over daemon RPC."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import warnings
from collections.abc import Callable
from pathlib import Path

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.providers.accounts import (
    account_status,
    load_preference,
    logout_account,
    save_api_key,
    save_preference,
)
from llm_cli.providers.codex_auth import CredentialStore, browser_login, device_login


def _read_secret(prompt: str) -> str:
    # getpass otherwise falls back to echoing input, which could expose a key.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise LlmCoordError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "API-key login needs a terminal that can hide your input; "
                "run Loupe in an interactive terminal",
            ) from None


def login_provider(
    provider: str,
    paths: AppPaths,
    *,
    notify: Callable[[str], None],
    read_secret: Callable[[str], str] | None = None,
    device: bool = False,
    from_codex: bool = False,
) -> dict[str, object]:
    """Sign in locally from either a subcommand or the terminal's /login flow."""
    if provider not in {"codex", "anthropic", "openai"}:
        raise LlmCoordError(
            ErrorCode.PROVIDER_UNAVAILABLE, "choose Codex, Anthropic, or OpenAI"
        )
    if provider != "codex" and (device or from_codex):
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            "browser, device-code, and Codex-import login are available for Codex; "
            "Anthropic and OpenAI use API keys",
        )
    try:
        if provider == "codex":
            store = CredentialStore(paths)
            if from_codex:
                source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
                result = store.import_codex_access(source / "auth.json")
            else:
                login = device_login if device else browser_login
                result = login(store, notify)
        else:
            label = "Anthropic" if provider == "anthropic" else "OpenAI"
            notify(f"Connect {label} with an API key. Your input will be hidden.")
            secret = (read_secret or _read_secret)(f"{label} API key: ")
            if not secret.strip():
                notify("Login cancelled.")
                return {"provider": provider, "cancelled": True}
            result = save_api_key(paths, provider, secret)
        try:
            preference = load_preference(paths)
        except LlmCoordError:
            # A fresh login can repair malformed settings. Never copy an
            # unvalidated model from the damaged preference file.
            preference = {}
        model = (
            preference.get("model") if preference.get("provider") == provider else None
        )
        effort = (
            preference.get("effort") if preference.get("provider") == provider else None
        )
        save_preference(
            paths,
            provider,
            model if isinstance(model, str) else None,
            effort if isinstance(effort, str) else None,
        )
        return result
    except (KeyboardInterrupt, EOFError):
        notify("Login cancelled.")
        return {"provider": provider, "cancelled": True}


def auth_command(arguments: argparse.Namespace, paths: AppPaths) -> dict[str, object]:
    provider = arguments.provider
    if arguments.auth_command == "status":
        return account_status(paths, provider)
    if arguments.auth_command == "logout":
        return logout_account(paths, provider)

    def notify(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    return login_provider(
        provider,
        paths,
        notify=notify,
        device=arguments.device,
        from_codex=arguments.from_codex,
    )
