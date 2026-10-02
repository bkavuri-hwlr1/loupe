"""Friendly provider selection with a separate, non-echoing credential prompt."""

from __future__ import annotations

import getpass
import warnings
from collections.abc import Callable
from typing import TextIO

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from llm_cli.cli import auth
from llm_cli.cli.terminal import TerminalUI
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.providers import accounts

_LABELS = {
    "codex": "Codex",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
}
_DESCRIPTIONS = {
    "codex": "Sign in with ChatGPT · uses your ChatGPT subscription",
    "anthropic": "Claude API key · separate Anthropic API billing",
    "openai": "OpenAI API key · separate OpenAI API billing",
}
_ALIASES = {"chatgpt": "codex", "claude": "anthropic"}
_SKIP = frozenset({"", "0", "skip", "later", "cancel"})


def normalize_provider(provider: str) -> str | None:
    """Resolve the same account names in menus and conversation commands."""
    value = provider.strip().lower()
    if value in _SKIP:
        return None
    value = _ALIASES.get(value, value)
    if value not in accounts.SUPPORTED_PROVIDERS:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            "Choose codex, anthropic, or openai. Enter 0 to connect later.",
        )
    return value


class ConnectionMenu:
    """Choose accounts in the transcript without recording secrets in history."""

    def __init__(
        self,
        paths: AppPaths,
        ui: TerminalUI,
        stdin: TextIO,
        stream: TextIO,
        plain: bool = False,
        *,
        read_secret: Callable[[str], str] | None = None,
    ) -> None:
        self.paths = paths
        self.ui = ui
        self.stdin = stdin
        self.stream = stream
        self.plain = plain or ui.plain
        self._secret_reader = read_secret

    def choose(self, provider: str | None = None, *, login: bool = False) -> str | None:
        """Select a usable account, logging in only when needed or requested."""

        try:
            selected = (
                normalize_provider(provider) if provider is not None else self._pick()
            )
            if selected is None:
                self.ui.notice("Connect whenever you're ready with /login.")
                return None
            current = self._account(selected)
            if login or not current.get("authenticated"):
                self.ui.message_heading(f"Connect {_LABELS[selected]}", style="brand")
                self.ui.notice(_DESCRIPTIONS[selected], style="")
                result = auth.login_provider(
                    selected,
                    self.paths,
                    notify=lambda message: self.ui.notice(message, style=""),
                    read_secret=self._read_secret,
                )
                if result.get("cancelled"):
                    self.ui.notice("Login cancelled. You can try again with /login.")
                    return None
                current = accounts.account_status(self.paths, selected)
            if not current.get("authenticated"):
                self.ui.notice(
                    "No account connected yet. Use /login when you're ready."
                )
                return None
            preference = self._preference()
            model = (
                preference.get("model")
                if preference.get("provider") == selected
                else None
            )
            effort = (
                preference.get("effort")
                if preference.get("provider") == selected
                else None
            )
            accounts.save_preference(
                self.paths,
                selected,
                model=model if isinstance(model, str) else None,
                effort=effort if isinstance(effort, str) else None,
            )
            self.ui.notice(
                f"{_LABELS[selected]} is ready. I'll remember this choice next time.",
                style="success",
            )
            return selected
        except (KeyboardInterrupt, EOFError):
            self.ui.notice("\nLogin cancelled. You can try again with /login.")
        except LlmCoordError as exc:
            self.ui.error(exc.message)
            self.ui.notice("You can try again with /login.")
        except OSError:
            self.ui.error("Could not access your local account settings.")
            self.ui.notice("You can try again with /login.")
        return None

    def status(self) -> None:
        """Show connection metadata; credential values are never rendered."""

        try:
            preference = self._preference()
            connected = [
                self._account(provider) for provider in accounts.SUPPORTED_PROVIDERS
            ]
            self.ui.message_heading("Your accounts", style="brand")
            for account in connected:
                provider = str(account.get("provider", ""))
                if provider not in _LABELS:
                    continue
                ready = bool(account.get("authenticated"))
                state = (
                    "sign in again"
                    if account.get("needs_login")
                    else "connected"
                    if ready
                    else "not connected"
                )
                selected = (
                    " · selected" if preference.get("provider") == provider else ""
                )
                self.ui.notice(
                    f"  {_LABELS[provider]} · {state}{selected}",
                    style="success" if ready else "muted",
                )
                self.ui.notice(f"    {_DESCRIPTIONS[provider]}")
            self.ui.notice(
                "\n/login to connect · /provider to switch · /logout PROVIDER"
            )
        except LlmCoordError as exc:
            self.ui.error(exc.message)
        except OSError:
            self.ui.error("Could not read your local account settings.")

    def logout(self, provider: str) -> str | None:
        """Return the removed account only when its saved login was removed."""

        try:
            selected = normalize_provider(provider)
            if selected is None:
                self.ui.notice(
                    "Usage: /logout codex, /logout anthropic, or /logout openai"
                )
                return None
            current = accounts.logout_account(self.paths, selected)
            self.ui.notice(f"Saved {_LABELS[selected]} login removed.", style="success")
            if current.get("authenticated"):
                self.ui.notice(
                    f"{_LABELS[selected]} is still connected through your environment. "
                    "Remove its API key environment variable to disconnect fully."
                )
            return selected
        except LlmCoordError as exc:
            self.ui.error(exc.message)
        except OSError:
            self.ui.error("Could not update your local account settings.")
        return None

    def _pick(self) -> str | None:
        providers = accounts.SUPPORTED_PROVIDERS
        statuses = {provider: self._account(provider) for provider in providers}
        rows: list[tuple[str, str, str]] = []
        for index, provider in enumerate(providers, start=1):
            status = statuses[provider]
            label = _LABELS[provider]
            if status.get("needs_login"):
                label += " · sign in again"
            elif status.get("authenticated"):
                label += " · connected"
            rows.append((str(index), label, _DESCRIPTIONS[provider]))
        self.ui.notice("")
        if self.plain or self.ui.console.width < 60:
            self.ui.rule("Connect your AI", title_style="brand")
            for number, label, description in rows:
                self.ui.notice(f"  {number}  {label}", style="")
                self.ui.notice(f"     {description}")
            self.ui.notice("  0  Connect later")
        else:
            options = Table.grid(padding=(0, 2))
            options.add_column(style="brand", no_wrap=True)
            options.add_column(overflow="fold")
            for number, label, description in rows:
                options.add_row(number, Text(label, style="bold"))
                options.add_row("", Text(description, style="muted"))
                options.add_row("", "")
            options.add_row("0", Text("Connect later", style="muted"))
            self.ui.console.print(
                Panel(
                    options,
                    title=Text("Connect your AI", style="brand"),
                    title_align="left",
                    border_style="#a78bfa",
                    padding=(1, 2),
                )
            )
        self.ui.notice("Choose a number or name. Enter skips for now.")
        while True:
            self.ui.delta("  Provider > ")
            answer = self.stdin.readline()
            if not answer:
                self.ui.notice("")
                return None
            # Piped input is intentionally supported for provider names only.
            value = answer.strip().lower()
            if value in _SKIP:
                return None
            if value in {str(index) for index in range(1, len(providers) + 1)}:
                return providers[int(value) - 1]
            try:
                return normalize_provider(value)
            except LlmCoordError as exc:
                self.ui.error(exc.message)

    def _account(self, provider: str) -> dict[str, object]:
        try:
            return accounts.account_status(self.paths, provider)
        except (LlmCoordError, OSError):
            # One broken saved login must not lock users out of other accounts
            # or prevent a fresh /login from repairing that account.
            return {"provider": provider, "authenticated": False, "needs_login": True}

    def _preference(self) -> dict[str, object]:
        try:
            return accounts.load_preference(self.paths)
        except (LlmCoordError, OSError):
            # A new usable selection replaces an unreadable preference safely.
            return {}

    def _read_secret(self, prompt: str) -> str:
        if self._secret_reader is not None:
            return self._secret_reader(prompt)
        if not self.stdin.isatty():
            raise LlmCoordError(
                ErrorCode.SESSION_AUTH_REQUIRED,
                "API keys need a private terminal prompt. Run loupe in a terminal "
                "and use /login, or set ANTHROPIC_API_KEY or OPENAI_API_KEY.",
            )
        try:
            # getpass normally falls back to echoing input when terminal control
            # fails. Turn its warning into an exception before that fallback.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                return getpass.getpass(prompt, stream=self.stream)
        except getpass.GetPassWarning as exc:
            raise LlmCoordError(
                ErrorCode.SESSION_AUTH_REQUIRED,
                "This terminal cannot hide API key input. Set ANTHROPIC_API_KEY "
                "or OPENAI_API_KEY, then use /provider.",
            ) from exc


__all__ = ["ConnectionMenu", "normalize_provider"]
