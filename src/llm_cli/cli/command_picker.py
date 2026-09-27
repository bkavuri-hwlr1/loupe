"""A compact, cursor-independent command list for the prompt editor."""

from __future__ import annotations

from collections.abc import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completion
from prompt_toolkit.filters import FilterOrBool, is_done, to_filter
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout import (
    ConditionalContainer,
    FloatContainer,
    HSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu, MultiColumnCompletionsMenu
from rich.text import Text

from llm_cli.cli.terminal import safe_text


def install_command_picker(
    session: PromptSession[str],
    get_fragments: Callable[[], StyleAndTextTuples],
    visible: FilterOrBool,
) -> None:
    """Keep input next to its footer, with a picker above it only while open."""

    shown = to_filter(visible) & ~is_done
    for container in list(session.layout.walk()):
        if (
            isinstance(container, Window)
            and isinstance(container.content, BufferControl)
            and container.content.buffer is session.default_buffer
        ):
            container.dont_extend_height = to_filter(True)
        if not isinstance(container, FloatContainer):
            continue
        if not any(
            isinstance(item.content, CompletionsMenu) for item in container.floats
        ):
            continue
        # Cap the whole editor at its content height. Bottom alignment adds a
        # flexible spacer that separates the prompt from the transcript on tall
        # terminals, even when the input window itself refuses to grow.
        # Render the picker in its own allocated rows so wrapped drafts and
        # short terminals cannot make it overlap the input.
        container.content = HSplit(
            [
                ConditionalContainer(
                    Window(
                        FormattedTextControl(get_fragments),
                        height=Dimension(min=0, preferred=8, max=8),
                        style="class:command-picker",
                    ),
                    filter=shown,
                ),
                container.content,
            ],
        )
        container.floats = [
            floating
            for floating in container.floats
            if not isinstance(
                floating.content, CompletionsMenu | MultiColumnCompletionsMenu
            )
        ]


def _clip(value: str, width: int) -> str:
    if width < 1:
        return ""
    text = Text(safe_text(value).replace("\n", " ").replace("\t", " "))
    text.truncate(max(0, width), overflow="ellipsis")
    return text.plain


def command_picker_fragments(
    completions: list[Completion], selected: int, width: int
) -> StyleAndTextTuples:
    """Render a header and at most six command rows, with no selection fill."""

    if width < 1:
        return []
    if not completions:
        return [
            ("class:command-picker.heading", _clip("  Commands", width) + "\n"),
            (
                "class:command-picker.description",
                _clip("  No matching commands", width),
            ),
        ]
    selected = max(0, min(selected, len(completions) - 1))
    first = min(selected // 6 * 6, max(0, len(completions) - 6))
    last = min(first + 6, len(completions))
    header = f"Commands  {first + 1}–{last} of {len(completions)}"  # noqa: RUF001
    fragments: StyleAndTextTuples = [
        ("class:command-picker.heading", _clip("  " + header, width))
    ]
    name_width = min(12, max(0, width - 2))
    description_width = max(0, width - 2 - name_width - 2)
    for index in range(first, last):
        completion = completions[index]
        current = index == selected
        fragments.append(("", "\n"))
        fragments.append(
            ("class:command-picker.marker", ("› " if current else "  ")[:width])  # noqa: RUF001
        )
        name = _clip(completion.display_text, name_width)
        name += " " * (name_width - Text(name).cell_len)
        fragments.append(
            (
                "class:command-picker.current"
                if current
                else "class:command-picker.command",
                name,
            )
        )
        if description_width:
            fragments.append(
                (
                    "class:command-picker.description",
                    "  " + _clip(completion.display_meta_text, description_width),
                )
            )
    return fragments


__all__ = ["command_picker_fragments", "install_command_picker"]
