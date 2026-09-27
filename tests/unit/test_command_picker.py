"""The slash picker stays compact, anchored, and independent of completion state."""

from __future__ import annotations

import asyncio

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import set_app
from prompt_toolkit.completion import Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input import DummyInput
from prompt_toolkit.layout import ConditionalContainer, FloatContainer, HSplit, Window
from prompt_toolkit.layout.menus import CompletionsMenu, MultiColumnCompletionsMenu
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth
from rich.text import Text

from llm_cli.cli.command_picker import (
    command_picker_fragments,
    install_command_picker,
)


def test_selected_command_is_visible_in_six_row_viewport() -> None:
    completions = [
        Completion(f"/command{i}", display_meta=f"Description {i}") for i in range(24)
    ]
    fragments = command_picker_fragments(completions, selected=20, width=60)
    lines = fragment_list_to_text(fragments).splitlines()
    assert lines[0] == "  Commands  19–24 of 24"  # noqa: RUF001
    assert len(lines) == 7
    assert lines[3].startswith("› /command20")  # noqa: RUF001
    assert "Description 20" in lines[3]
    assert sum(line.startswith("› ") for line in lines) == 1  # noqa: RUF001
    assert [
        text.strip()
        for style, text in fragments
        if style == "class:command-picker.current"
    ] == ["/command20"]


def test_narrow_and_unicode_rows_never_overflow_or_inject_terminal_controls() -> None:
    commands = [
        Completion("/日本語command", display_meta="说明\nnext\t\x1b[31mwords " * 10)
    ]
    for width in range(1, 79):
        fragments = command_picker_fragments(commands, selected=0, width=width)
        for line in fragment_list_to_text(fragments).splitlines():
            assert Text(line).cell_len <= width
            assert get_cwidth(line) <= width
            assert "\x1b" not in line
        assert len(fragment_list_to_text(fragments).splitlines()) == 2
    assert command_picker_fragments(commands, selected=0, width=0) == []


def test_names_align_and_empty_matches_remain_quiet() -> None:
    fragments = command_picker_fragments(
        [
            Completion("/cd", display_meta="Folder"),
            Completion("/provider", display_meta="Account"),
        ],
        selected=0,
        width=50,
    )
    lines = fragment_list_to_text(fragments).splitlines()
    assert lines[0].index("Commands") == lines[1].index("/cd") == lines[2].index(
        "/provider"
    ) == 2
    assert lines[1].index("Folder") == lines[2].index("Account") == 16
    empty = command_picker_fragments([], selected=0, width=50)
    assert fragment_list_to_text(empty) == "  Commands\n  No matching commands"
    assert all("current" not in style for style, _ in empty)


def test_install_replaces_completion_filters_and_leaves_right_prompt_untouched() -> (
    None
):
    session: PromptSession[str] = PromptSession(
        input=DummyInput(),
        output=DummyOutput(),
        rprompt="right",
        multiline=True,
        reserve_space_for_menu=0,
    )
    container = next(
        item for item in session.layout.walk() if isinstance(item, FloatContainer)
    )
    right_prompt = container.floats[-1]
    visible = True
    install_command_picker(
        session,
        lambda: command_picker_fragments([], 0, 60),
        Condition(lambda: visible),
    )
    assert container.floats[-1] is right_prompt
    assert container.floats == [right_prompt]
    assert not any(
        isinstance(item.content, CompletionsMenu | MultiColumnCompletionsMenu)
        for item in container.floats
    )
    assert isinstance(container.content, HSplit)
    picker = container.content.children[0]
    assert isinstance(picker, ConditionalContainer)
    assert isinstance(picker.content, Window)
    assert picker.content.style == "class:command-picker"
    assert picker.content.get_line_prefix is None
    with set_app(session.app):
        assert session.default_buffer.complete_state is None
        assert picker.filter()
        visible = False
        assert not picker.filter()

        # Closing the picker reserves only the actual input height, including
        # multiline drafts; it must not leave a permanent blank menu-sized box.
        async def dimensions() -> None:
            nonlocal visible
            try:
                height = container.content.preferred_height(80, 60)
                assert height.preferred == height.max == 1
                session.default_buffer.document = Document("first\nsecond\nthird")
                height = container.content.preferred_height(80, 60)
                assert height.preferred == height.max == 3
                visible = True
                height = container.content.preferred_height(80, 60)
                assert height.preferred == height.max == 11
                assert container.content.preferred_height(80, 6).min <= 3
                visible = False
                height = container.content.preferred_height(80, 60)
                assert height.preferred == height.max == 3
            finally:
                await session.app.cancel_and_wait_for_background_tasks()

        asyncio.run(dimensions())


def test_rendered_picker_never_covers_wrapped_input_or_leaves_closed_gap() -> None:
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.layout.controls import BufferControl
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen, WritePosition

    class Output(DummyOutput):
        def __init__(self, rows: int, columns: int) -> None:
            self.rows = rows
            self.columns = columns

        def get_size(self) -> Size:
            return Size(rows=self.rows, columns=self.columns)

    async def exercise(rows: int, columns: int) -> None:
        session: PromptSession[str] = PromptSession(
            "> ",
            input=DummyInput(),
            output=Output(rows, columns),
            multiline=True,
            prompt_continuation=". ",
            bottom_toolbar="divider\nmetadata\nhints",
            reserve_space_for_menu=0,
        )
        visible = True
        install_command_picker(
            session,
            lambda: command_picker_fragments([], 0, columns - 4),
            Condition(lambda: visible),
        )

        def render() -> tuple[Screen, WritePosition, WritePosition]:
            screen = Screen()
            session.layout.container.write_to_screen(
                screen,
                MouseHandlers(),
                WritePosition(0, 0, columns, rows),
                parent_style="",
                erase_bg=True,
                z_index=None,
            )
            screen.draw_all_floats()
            positions = screen.visible_windows_to_write_positions
            input_position = next(
                position
                for window, position in positions.items()
                if isinstance(window.content, BufferControl)
                and window.content.buffer is session.default_buffer
            )
            footer_position = next(
                position
                for window, position in positions.items()
                if window.style == "class:bottom-toolbar"
            )
            assert footer_position.ypos == input_position.ypos + input_position.height
            return screen, input_position, footer_position

        with set_app(session.app):
            try:
                session.default_buffer.document = Document("/" + "x" * 100)
                screen, input_position, _ = render()
                positions = screen.visible_windows_to_write_positions
                picker_position = next(
                    position
                    for window, position in positions.items()
                    if window.style == "class:command-picker"
                )
                assert (
                    picker_position.ypos + picker_position.height <= input_position.ypos
                )
                for row in range(
                    input_position.ypos, input_position.ypos + input_position.height
                ):
                    line = "".join(
                        screen.data_buffer[row][column].char
                        for column in range(columns)
                    )
                    assert line.startswith(("> ", ". "))
                    assert "x" in line

                visible = False
                session.default_buffer.document = Document("first\nsecond\nthird")
                _, input_position, footer_position = render()
                assert input_position.ypos == 0
                assert input_position.height == footer_position.ypos == 3
            finally:
                await session.app.cancel_and_wait_for_background_tasks()

    for rows in (8, 24):
        for columns in (30, 80):
            asyncio.run(exercise(rows, columns))
