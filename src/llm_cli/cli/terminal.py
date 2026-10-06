"""Scrollback-friendly Loupe presentation and terminal-safe text.

Only our presentation layer generates terminal escapes. Provider text and tool
output are treated as data, including when an escape spans streamed chunks.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar, TextIO

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import CodeBlock, Markdown, MarkdownElement
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

_THEME = Theme(
    {
        "brand": "bold #a78bfa",
        "assistant": "bold #a78bfa",
        "reasoning": "#a78bfa",
        "tool": "bold #6ee7b7",
        "success": "#6ee7b7",
        "error": "bold red",
        "warning": "yellow",
        "muted": "dim",
    }
)
_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


class TextSanitizer:
    """Strip terminal controls, preserving normal text, tabs and line breaks."""

    def __init__(self) -> None:
        self._state = "text"

    def feed(self, value: str) -> str:
        result: list[str] = []
        for character in value:
            if self._state == "escape":
                if character == "[":
                    self._state = "csi"
                elif character in "]PX^_":
                    self._state = "string"
                elif " " <= character <= "/":
                    self._state = "escape_intermediate"
                else:
                    self._state = "text"
                continue
            if self._state == "escape_intermediate":
                if "0" <= character <= "~":
                    self._state = "text"
                continue
            if self._state == "csi":
                if "@" <= character <= "~":
                    self._state = "text"
                continue
            if self._state == "string":
                if character in {"\x07", "\x9c"}:
                    self._state = "text"
                elif character == "\x1b":
                    self._state = "string_escape"
                continue
            if self._state == "string_escape":
                self._state = "text" if character == "\\" else "string"
                continue
            if character == "\x1b":
                self._state = "escape"
            elif character == "\x9b":
                self._state = "csi"
            elif character in {"\x90", "\x9d", "\x98", "\x9e", "\x9f"}:
                self._state = "string"
            elif character in {"\n", "\t"} or (
                ord(character) >= 32
                and not 127 <= ord(character) <= 159
                and character not in _BIDI_CONTROLS
            ):
                result.append(character)
        return "".join(result)


def safe_text(value: object) -> str:
    """Return complete text without active terminal or bidi control sequences."""

    return TextSanitizer().feed(str(value))


def _scope_label(scopes: Sequence[str]) -> str:
    return "whole repository" if list(scopes) == ["*"] else ", ".join(scopes)


def _model_label(provider: str, model: str) -> str:
    if not provider:
        return "Not connected · /login when you're ready"
    return f"{provider}/{model}" if model else provider


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


class _AnswerCodeBlock(CodeBlock):
    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        # Rich's default code padding consumes the entire content area in very
        # narrow panes. Use the same unboxed, wrapping syntax as other code output.
        yield Syntax(
            str(self.text).rstrip("\n"),
            self.lexer_name,
            theme=self.theme,
            word_wrap=True,
            padding=0,
            background_color="default",
        )


class _AnswerMarkdown(Markdown):
    elements: ClassVar[dict[str, type[MarkdownElement]]] = {
        **Markdown.elements,
        "fence": _AnswerCodeBlock,
        "code_block": _AnswerCodeBlock,
    }


class TerminalUI:
    """Small reusable UI primitives; writes normally, never clears scrollback."""

    def __init__(self, stream: TextIO, *, plain: bool = False) -> None:
        self.stream = stream
        self.plain = plain or not stream.isatty() or os.environ.get("TERM") == "dumb"
        self._last_activity: str | None = None
        self.console = Console(
            file=stream,
            force_terminal=not self.plain,
            no_color=self.plain or "NO_COLOR" in os.environ,
            color_system=None if self.plain else "auto",
            theme=_THEME,
            markup=False,
            highlight=False,
            emoji=False,
        )

    def banner(
        self,
        *,
        repository: Path | str,
        scopes: Sequence[str],
        provider: str,
        model: str,
        session_id: str,
        workspace_mode: str = "shared",
        effort: str | None = None,
        agent_mode: str = "normal",
    ) -> None:
        if self.plain or self.console.width < 60:
            self.stream.write(
                "\nLoupe · your coding workspace\n"
                f"  repository: {safe_text(repository)}\n"
                f"  model: {safe_text(_model_label(provider, model))}\n"
                f"  effort: {safe_text(effort or 'provider default')}\n"
                f"  scopes: {safe_text(_scope_label(scopes))}\n"
                f"  session: {safe_text(session_id)}\n"
                f"  workspace: {safe_text(workspace_mode)}\n"
                f"  mode: {safe_text(agent_mode)}\n"
                "  /help commands · /exit quit\n\n"
            )
            self.stream.flush()
            return
        metadata = Table.grid(padding=(0, 2), expand=False)
        metadata.add_column(style="muted", no_wrap=True)
        metadata.add_column(overflow="fold")
        metadata.add_row("Workspace", safe_text(repository))
        metadata.add_row("Model", safe_text(_model_label(provider, model)))
        if provider:
            metadata.add_row("Effort", safe_text(effort or "provider default"))
        metadata.add_row("Scope", safe_text(_scope_label(scopes)))
        metadata.add_row("Session", safe_text(session_id))
        metadata.add_row("Workspace type", safe_text(workspace_mode))
        metadata.add_row("Mode", safe_text(agent_mode))
        self.console.print()
        self.console.print(
            Panel(
                metadata,
                title=Text("Loupe", style="brand"),
                title_align="left",
                subtitle=Text("your coding workspace", style="muted"),
                subtitle_align="right",
                border_style="#a78bfa",
                padding=(1, 2),
            )
        )
        self.notice(
            "  Enter send · Alt+Enter new line · Shift+Tab mode · / search commands"
        )
        self.console.print()

    def help(self) -> None:
        self.heading("Commands", style="brand")
        for command, description in (
            ("/login [PROVIDER]", "Connect an AI account"),
            ("/provider", "Choose Codex, Anthropic, or OpenAI"),
            ("/accounts", "Show connected accounts"),
            ("/logout [PROVIDER]", "Remove a saved account"),
            ("/cd PATH", "Choose a project folder"),
            ("/help", "Show available commands"),
            ("/status", "Show this session and workspace"),
            (
                "/mode [plan|normal|auto]",
                "Choose read-only planning, review, or auto edits",
            ),
            ("/scope PATH...", "View or update the paths Loupe may change"),
            ("/changes", "Show changes from other sessions"),
            ("/tasks", "List recent tasks"),
            ("/attach [TASK_ID]", "Follow a running or recent task"),
            ("/history", "Show conversation history"),
            ("/model [MODEL_ID]", "Browse this account's models and effort levels"),
            ("/model --refresh", "Refresh the account's model list"),
            ("/effort [LEVEL]", "Choose thinking effort for the current model"),
            ("/diff [TASK_ID]", "Inspect prepared changes"),
            ("/apply [TASK_ID]", "Publish reviewed changes"),
            ("/compact", "Summarize the conversation to free context"),
            ("/usage", "Show token usage and how full the context is"),
            ("/clear", "Clear the terminal display"),
            ("/detach", "Leave this session available to resume"),
            ("/exit, /quit", "Close this session"),
        ):
            self.notice(f"  {command:<20} {description}", style="")
        self.notice("  Enter send · Alt+Enter new line · Ctrl+C detach running task")
        self.notice(
            "  Shift+Tab cycles plan → normal → auto while idle; keeps your draft"
        )
        self.notice("  Ctrl+C twice within 2 seconds exits; active tasks keep running")

    def status(
        self,
        *,
        repository: Path | str,
        scopes: Sequence[str],
        provider: str,
        model: str,
        session_id: str,
        workspace_mode: str = "shared",
        effort: str | None = None,
        agent_mode: str = "normal",
    ) -> None:
        for label, value in (
            ("repository", repository),
            ("model", _model_label(provider, model)),
            ("effort", effort or "provider default"),
            ("scopes", _scope_label(scopes)),
            ("workspace", workspace_mode),
            ("mode", agent_mode),
            ("session", session_id),
        ):
            self.notice(f"  {label}: {value}", style="")

    def notice(self, text: str, *, style: str = "muted") -> None:
        cleaned = safe_text(text)
        if self.plain:
            self.stream.write(cleaned + "\n")
        else:
            self.console.print(Text(cleaned, style=style), overflow="fold", crop=False)
        self.stream.flush()

    def error(self, message: str) -> None:
        self.notice(f"  ✗ {message}", style="error")

    def activity(self, text: str | None) -> None:
        """Print concise activity once when a transient status row is unavailable."""

        cleaned = (
            safe_text(text).replace("\n", " ").replace("\t", " ") if text else None
        )
        if cleaned and cleaned != self._last_activity:
            self.notice(cleaned)
        self._last_activity = cleaned

    def progress(self, text: str | None) -> None:
        """Show the task plan's current step; scrollback already lists the plan."""

    def preview(self, text: str | None) -> None:
        """Without a transient region, announce a draft answer but not its text.

        A provisional answer may still be revised, so only a live status region
        can show it without committing it to scrollback.
        """

        if text:
            self.activity("Writing the answer…")

    def user(self, text: str) -> None:
        self.message_heading("You", style="bold")
        self.body(text)

    def rule(self, title: str = "", *, title_style: str = "") -> None:
        label = Text(
            safe_text(title).replace("\n", " "),
            style="" if self.console.no_color else title_style,
        )
        if self.plain:
            # Rich normally adds a Unicode ellipsis when it shortens labels.
            label.expand_tabs()
            label.truncate(max(0, self.console.width - 2), overflow="crop")
        self.console.rule(
            label,
            characters="-" if self.plain else "─",
            style="" if self.console.no_color else "muted",
            align="left",
        )
        self.stream.flush()

    def message_heading(self, text: str, *, style: str = "assistant") -> None:
        """Separate transcript messages with a labelled, terminal-width rule."""

        self.notice("")
        self.rule(text, title_style=style)
        self.notice("")

    def heading(self, text: str, *, style: str = "assistant") -> None:
        self.notice(f"\n{safe_text(text)}", style=style)

    def body(self, text: str, *, markdown: bool = False) -> None:
        cleaned = safe_text(text)
        if self.plain:
            self.stream.write(cleaned)
            if not cleaned.endswith("\n"):
                self.stream.write("\n")
        elif markdown:
            document = _AnswerMarkdown(cleaned, hyperlinks=False)
            # Rich's Markdown tables use ellipsis for long cells. Preserve the
            # original Markdown whenever a table could hide response content.
            if any(token.type == "table_open" for token in document.parsed):
                self.code(cleaned, language="markdown")
            else:
                self.console.print(document, crop=False)
        else:
            self.console.print(Text(cleaned), overflow="fold", crop=False)
        self.stream.flush()

    def markdown_leading_blank(self, text: str) -> bool:
        """Whether ``body(text, markdown=True)`` starts with Rich's own blank line.

        Rich opens lists and block quotes with a margin row, so a streamed
        block can supply its own paragraph separation.
        """

        if self.plain:
            return False
        document = _AnswerMarkdown(safe_text(text), hyperlinks=False)
        if any(token.type == "table_open" for token in document.parsed):
            return False
        lines = self.console.render_lines(document, pad=False)
        return bool(lines) and not "".join(part.text for part in lines[0]).strip()

    def delta(self, text: str, *, style: str = "") -> None:
        """Write already-sanitized text verbatim, letting the terminal wrap it."""

        if self.plain:
            self.stream.write(text)
        else:
            self.console.print(Text(text, style=style), end="", soft_wrap=True)
        self.stream.flush()

    def arguments(self, arguments: object) -> None:
        if not isinstance(arguments, Mapping):
            self.body(arguments if isinstance(arguments, str) else _json(arguments))
            return
        for key, value in arguments.items():
            if isinstance(value, str) and "\n" in value:
                self.notice(f"  {key}:")
                path = str(arguments.get("path", ""))
                language = (
                    "diff" if "diff" in str(key) or "patch" in str(key) else "text"
                )
                if path and language == "text":
                    language = Syntax.guess_lexer(path, code=value)
                self.code(value, language=language)
            else:
                content = value if isinstance(value, str) else _json(value)
                self.notice(f"  {key}: {content}", style="")

    def code(self, text: str, *, language: str = "text") -> None:
        cleaned = safe_text(text)
        if self.plain:
            self.body(cleaned)
        else:
            self.console.print(
                Syntax(
                    cleaned,
                    language,
                    theme="monokai",
                    word_wrap=True,
                    padding=0,
                    background_color="default",
                ),
                crop=False,
            )
        self.stream.flush()


__all__ = ["TerminalUI", "TextSanitizer", "safe_text"]
