"""Searchable, account-aware model and effort selection inside the terminal."""

from __future__ import annotations

import os
from contextlib import closing
from dataclasses import dataclass
from typing import TextIO

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import create_input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.output import create_output
from prompt_toolkit.styles import Style
from rich.table import Table
from rich.text import Text

from llm_cli.cli.terminal import TerminalUI, safe_text
from llm_cli.paths import AppPaths
from llm_cli.providers import catalog
from llm_cli.providers.catalog import ModelOption

_PROVIDERS = {
    "codex": "Codex · ChatGPT",
    "openai": "OpenAI API",
    "anthropic": "Anthropic",
}
_EFFORT_DESCRIPTIONS = {
    "none": "Skip reasoning for the quickest responses.",
    "minimal": "A small reasoning budget for straightforward work.",
    "low": "Less reasoning time for faster responses.",
    "medium": "A balance of reasoning depth and response time.",
    "high": "More reasoning for difficult tasks; responses may take longer.",
    "xhigh": "Extra reasoning for demanding tasks; expect longer responses.",
    "max": "An intensive reasoning setting; may take longer and cost more.",
    "ultra": "The highest available reasoning setting; may take longer and cost more.",
}
_CANCEL = frozenset({"", "0", "cancel", "back", "later", "quit"})


def _line(value: str) -> str:
    return " ".join(safe_text(value).split())


@dataclass(frozen=True)
class _Choice:
    key: str
    name: str
    description: str


def _matches(choices: tuple[_Choice, ...], query: str) -> list[int]:
    words = query.casefold().split()
    return [
        index
        for index, choice in enumerate(choices)
        if all(
            word in f"{choice.key} {choice.name} {choice.description}".casefold()
            for word in words
        )
    ]


def _exact(choices: tuple[_Choice, ...], query: str) -> int | None:
    normalized = query.strip().casefold()
    for index, choice in enumerate(choices):
        if normalized in {
            str(index + 1),
            choice.key.casefold(),
            choice.name.casefold(),
        }:
            return index
    return None


class ModelMenu:
    """Select settings locally; cancellation never writes preferences or sessions."""

    def __init__(
        self,
        paths: AppPaths,
        ui: TerminalUI,
        stdin: TextIO,
        stream: TextIO,
        plain: bool = False,
    ) -> None:
        self.paths = paths
        self.ui = ui
        self.stdin = stdin
        self.stream = stream
        self.plain = plain or ui.plain

    def choose_model(
        self,
        provider: str,
        current_model: str | None = None,
        *,
        refresh: bool = False,
    ) -> ModelOption | None:
        self.ui.rule(f"Models · {_PROVIDERS.get(provider, _line(provider))}")
        listing = catalog.list_models(self.paths, provider, refresh=refresh)
        if listing.notice:
            self.ui.notice(listing.notice)
        self.ui.notice(
            f"Model list: {listing.source} · /model --refresh reloads the list"
        )
        if not listing.models:
            self.ui.notice("No models are available. Connect an account with /login.")
            return None
        self.ui.notice("Recommended / newest models first.")
        choices = tuple(
            _Choice(
                option.id,
                _line(option.name),
                " · ".join(
                    part
                    for part in (
                        _line(option.description),
                        "Effort: " + ", ".join(option.efforts)
                        if option.efforts
                        else "Effort controls not listed",
                    )
                    if part
                ),
            )
            for option in listing.models
        )
        current = current_model or catalog.default_model(provider)
        selected = self._pick(
            choices, current=current, label="Model", select_current=False
        )
        return listing.models[selected] if selected is not None else None

    def choose_effort(
        self, option: ModelOption, current_effort: str | None = None
    ) -> str | None:
        if not option.efforts:
            unchanged = (
                f"Your current effort setting ({_line(current_effort)}) is unchanged."
                if current_effort and current_effort != "default"
                else "Loupe will use the provider's default."
            )
            self.ui.notice(
                f"{_line(option.name)} has no listed effort settings. {unchanged}"
            )
            return "default"
        self.ui.rule(f"Reasoning effort · {_line(option.name)}")
        self.ui.notice(
            "Higher effort gives the model more room to reason and can take longer."
        )
        default_description = "Let the provider choose its standard reasoning setting."
        if option.default_effort:
            default_description += f" Currently {_line(option.default_effort)}."
        choices = (
            _Choice("default", "Default", default_description),
            *(
                _Choice(
                    effort,
                    effort.capitalize(),
                    _EFFORT_DESCRIPTIONS.get(
                        effort, "Reasoning effort supported by this model."
                    ),
                )
                for effort in option.efforts
                if effort != "default"
            ),
        )
        selected = self._pick(
            choices, current=current_effort or "default", label="Effort"
        )
        return choices[selected].key if selected is not None else None

    def _pick(
        self,
        choices: tuple[_Choice, ...],
        *,
        current: str | None,
        label: str,
        select_current: bool = True,
    ) -> int | None:
        if current:
            self.ui.notice(f"Current: {_line(current)}")
        try:
            if (
                not self.plain
                and self.stdin.isatty()
                and self.stream.isatty()
                and os.getenv("TERM") != "dumb"
            ):
                return self._interactive(
                    choices,
                    current=current,
                    label=label,
                    select_current=select_current,
                )
            return self._plain(choices, current=current, label=label)
        except (KeyboardInterrupt, EOFError):
            self.ui.notice("")
            return None

    def _render(
        self, choices: tuple[_Choice, ...], matches: list[int], current: str | None
    ) -> None:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="brand", no_wrap=True)
        table.add_column(overflow="fold")
        for index in matches:
            choice = choices[index]
            label = choice.name
            if choice.key != choice.name:
                label += f" · {_line(choice.key)}"
            if choice.key == current:
                label += " · current"
            table.add_row(str(index + 1), Text(label, style="bold"))
            table.add_row("", Text(choice.description, style="muted"))
        self.ui.console.print(table)

    def _plain(
        self, choices: tuple[_Choice, ...], *, current: str | None, label: str
    ) -> int | None:
        self._render(choices, list(range(len(choices))), current)
        self.ui.notice(
            "Choose a number or name, or type to filter. * shows all. Enter cancels."
        )
        while True:
            self.ui.delta(f"  {label} > ")
            answer = self.stdin.readline()
            self.ui.notice("")
            if not answer or answer.strip().casefold() in _CANCEL:
                return None
            value = answer.strip()
            exact = _exact(choices, value)
            if exact is not None:
                return exact
            matches = _matches(choices, "" if value == "*" else value)
            if matches:
                self._render(choices, matches, current)
            else:
                self.ui.notice("No matches. Try another search, or * to show all.")

    def _interactive(
        self,
        choices: tuple[_Choice, ...],
        *,
        current: str | None,
        label: str,
        select_current: bool = True,
    ) -> int | None:
        """Use an inline menu so ordinary terminal scrollback remains available."""
        matches = list(range(len(choices)))
        selected = next(
            (
                index
                for index, choice in enumerate(choices)
                if select_current and choice.key == current
            ),
            0,
        )

        def changed(buffer: Buffer) -> None:
            nonlocal matches, selected
            matches = _matches(choices, buffer.text)
            exact = _exact(choices, buffer.text)
            if exact is not None and exact not in matches:
                matches = [exact]
            selected = matches.index(exact) if exact in matches else 0

        search = Buffer(on_text_changed=changed)
        control = BufferControl(buffer=search)
        bindings = KeyBindings()

        @bindings.add("up")
        @bindings.add("s-tab")
        def previous(event: KeyPressEvent) -> None:
            nonlocal selected
            if matches:
                selected = (selected - 1) % len(matches)

        @bindings.add("down")
        @bindings.add("tab")
        def following(event: KeyPressEvent) -> None:
            nonlocal selected
            if matches:
                selected = (selected + 1) % len(matches)

        @bindings.add("enter")
        def choose(event: KeyPressEvent) -> None:
            # Resolving an Escape-prefixed key batch can invoke another handler
            # after cancellation has already completed the application.
            if event.app.is_done:
                return
            if search.text.strip().casefold() in _CANCEL - {""}:
                event.app.exit(result=None)
            elif matches:
                event.app.exit(result=matches[selected])

        @bindings.add("escape")
        @bindings.add("c-c")
        @bindings.add("c-d")
        def cancel(event: KeyPressEvent) -> None:
            if not event.app.is_done:
                event.app.exit(result=None)

        def rows() -> StyleAndTextTuples:
            if not matches:
                return [("class:muted", "  No matches. Try another search.")]
            start = max(0, min(selected - 3, len(matches) - 8))
            result: StyleAndTextTuples = []
            for position in range(start, min(start + 8, len(matches))):
                index = matches[position]
                choice = choices[index]
                style = "class:selected" if position == selected else ""
                marker = ">" if position == selected else " "
                text = f" {marker} {index + 1:>2}  {choice.name}"
                if choice.key.casefold() != choice.name.casefold():
                    text += f" · {_line(choice.key)}"
                if choice.key == current:
                    text += " · current"
                result.append((style, text + "\n"))
            return result

        def detail() -> str:
            if not matches:
                return ""
            return "  " + choices[matches[selected]].description

        body = HSplit(
            [
                VSplit(
                    [
                        Window(
                            FormattedTextControl(f"  {label} search > "),
                            width=len(label) + 12,
                        ),
                        Window(control),
                    ],
                    height=1,
                ),
                Window(FormattedTextControl(rows), height=min(8, len(choices))),
                Window(
                    FormattedTextControl(detail),
                    height=3,
                    wrap_lines=True,
                    style="class:muted",
                ),
                Window(
                    FormattedTextControl(
                        "  Type to filter · ↑↓ browse · Enter choose · Esc cancel"
                    ),
                    height=1,
                    style="class:muted",
                ),
            ]
        )
        style = Style.from_dict(
            {"selected": "bold", "muted": ""}
            if "NO_COLOR" in os.environ
            else {"selected": "bg:#34304b #ffffff bold", "muted": "#9da8bb"}
        )
        with closing(create_input(stdin=self.stdin)) as prompt_input:
            application: Application[int | None] = Application(
                layout=Layout(body, focused_element=control),
                key_bindings=bindings,
                input=prompt_input,
                output=create_output(stdout=self.stream),
                style=style,
                full_screen=False,
                erase_when_done=True,
            )
            return application.run(in_thread=True)


__all__ = ["ModelMenu"]
