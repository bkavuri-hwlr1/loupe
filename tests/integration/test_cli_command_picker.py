"""The command picker keeps a small real terminal steady while editing."""

# ruff: noqa: RUF001 -- Exact terminal glyphs are part of the visible contract.

from __future__ import annotations

import contextlib
import re
from pathlib import Path

import pytest
from test_cli_mode_shortcut import _draft, _Screen, _Terminal
from test_cli_modes import _ModeCluster
from test_cli_modes import mode_cluster as mode_cluster
from test_cli_process_orchestration import _wait

_PROMPT = "  ❯ "


class _PickerView:
    def __init__(self, terminal: _Terminal) -> None:
        self.terminal = terminal
        _wait(
            lambda: (
                "Mode: normal" in terminal.footer and "Enter send" in terminal.footer
            )
        )
        with terminal.screen_lock:
            self.scrolls = terminal.screen.scrolls
        self.metadata = terminal.footer.splitlines()[0]
        self.prompt_row = 0
        self.picker_open = False
        self.captures: list[str] = []

    def capture(
        self,
        label: str,
        draft: str,
        *,
        picker: bool = True,
        text_grows: bool = False,
    ) -> list[str]:
        def completed_frame() -> tuple[list[str], int] | None:
            # PTY reads can split a single repaint after the menu but before
            # the footer. prompt_toolkit restores the cursor at frame end.
            with self.terminal.screen_lock:
                screen = self.terminal.screen
                lines = screen.lines()
                hint = "browse commands" if picker else "Enter send"
                if (
                    screen.cursor_visible
                    and self.metadata in lines
                    and any(hint in line for line in lines)
                ):
                    return lines, screen.scrolls
                return None

        lines, current_scrolls = _wait(completed_frame)
        visible = "\n".join(lines)
        if picker and not self.picker_open:
            # Opening allocates eight rows above input. Once it is open,
            # filtering and navigation must leave transcript scrollback alone.
            assert self.scrolls <= current_scrolls <= self.scrolls + 8, visible
        elif text_grows:
            assert self.scrolls <= current_scrolls <= self.scrolls + 1, visible
        else:
            assert current_scrolls == self.scrolls, visible
        self.scrolls = current_scrolls
        self.picker_open = picker
        self.prompt_row = max(
            index
            for index, line in enumerate(lines)
            if line.startswith(_PROMPT.rstrip())
        )
        status_row = max(
            index for index, line in enumerate(lines) if line == self.metadata
        )
        assert lines[status_row - 1] == "─" * 80, visible
        input_rows = lines[self.prompt_row : status_row - 1]
        assert all(input_rows), visible
        displayed_draft = [input_rows[0][len(_PROMPT) :]] + [
            line.removeprefix("  · ") for line in input_rows[1:]
        ]
        separator = "\n" if "\n" in draft else ""
        assert separator.join(displayed_draft) == draft, visible
        # The last editable row touches the footer, with no hidden empty region.
        assert not any(lines[status_row + 2 :]), visible
        if picker:
            assert lines[self.prompt_row - 9] == "─" * 80, visible
            assert lines[self.prompt_row - 8].startswith("  Commands"), visible
            assert "browse commands" in lines[status_row + 1], visible
            # At most six results plus heading fit above the persistent footer.
            assert len([line for line in lines if re.match(r"(?:› |  )/", line)]) <= 6
        else:
            assert lines[self.prompt_row - 1] == "─" * 80, visible
            assert not any(line.startswith("  Commands") for line in lines), visible
            assert "Enter send" in lines[status_row + 1], visible
        self.captures.append(f"{label}\n{visible}\n")
        return lines

    def save(self, path: Path) -> None:
        path.write_text("\n".join(self.captures), encoding="utf-8")


def test_screen_tracks_completion_across_split_repaint_bytes() -> None:
    screen = _Screen(rows=5, columns=80)
    screen.feed(b"\x1b[?25lCommands\r\n")
    assert not screen.cursor_visible
    screen.feed(b"Model: demo\r\nEnter send\x1b[?25")
    assert not screen.cursor_visible
    screen.feed(b"h")
    assert screen.cursor_visible
    assert screen.lines()[:3] == ["Commands", "Model: demo", "Enter send"]


def test_command_picker_aligns_results_and_uses_available_width(
    mode_cluster: _ModeCluster,
) -> None:
    with contextlib.closing(
        _Terminal(mode_cluster, "picker-width", rows=24, columns=65)
    ) as terminal:
        terminal.write("/mod")
        _wait(
            lambda: "› /mode" in terminal.visible
            and "browse commands" in terminal.footer
        )
        lines = terminal.visible.splitlines()
        heading_row = next(
            index for index, line in enumerate(lines) if line.startswith("  Commands")
        )
        heading, selected, model = lines[heading_row : heading_row + 3]
        assert selected.index("/mode") == model.index("/model") == heading.index(
            "Commands"
        )
        assert "Browse available models and their effort levels" in model
        assert terminal.footer.splitlines()[1].endswith("Esc close")
        terminal.write(b"\x03")
        terminal.write("/detach\r")
        assert terminal.process.wait(timeout=5) == 0, terminal.text


@pytest.mark.parametrize("rows", [24, 60])
def test_command_picker_keeps_query_geometry_and_footer_stable(
    mode_cluster: _ModeCluster,
    rows: int,
) -> None:
    cluster = mode_cluster
    with contextlib.closing(
        _Terminal(cluster, "picker", rows=rows, columns=80)
    ) as terminal:
        view = _PickerView(terminal)
        view.capture("Before opening", "", picker=False)
        output_start = len(terminal.output)

        terminal.write("/")
        _wait(lambda: "› /login" in terminal.visible)
        lines = view.capture("All commands", "/")
        assert "1–6 of" in lines[view.prompt_row - 8]
        assert next(line for line in lines if "Connect Codex" in line).index(
            "Connect Codex"
        ) == next(line for line in lines if "Choose your AI" in line).index(
            "Choose your AI"
        )

        terminal.write(b"\x1b[B")
        _wait(lambda: "› /provider" in terminal.visible)
        view.capture("Down preserves the query", "/")
        terminal.write(b"\x1b[A")
        _wait(lambda: "› /login" in terminal.visible)
        view.capture("Up preserves the query", "/")

        terminal.write("mo")
        _wait(lambda: "› /mode" in terminal.visible)
        view.capture("Filtered", "/mo")
        terminal.write(b"\x7f")
        _wait(lambda: _PROMPT + "/m\n" in terminal.visible)
        view.capture("Backspace refreshes matches", "/m")
        terminal.write(b"\x7f")
        _wait(lambda: "› /login" in terminal.visible)
        view.capture("Backspace restores all commands", "/")

        terminal.write("zzzz")
        _wait(lambda: "No matching commands" in terminal.visible)
        view.capture("No results keeps the picker in place", "/zzzz")
        terminal.write("x" * 100)
        _wait(lambda: "x" * 40 in terminal.visible)
        view.capture(
            "Wrapped slash query remains below the picker",
            "/zzzz" + "x" * 100,
            text_grows=True,
        )
        terminal.write(b"\x7f" * 100)
        _wait(lambda: _PROMPT + "/zzzz\n" in terminal.visible)
        view.capture("Short query collapses its wrapped row", "/zzzz")
        terminal.write(b"\x1b")
        _wait(lambda: "No matching commands" not in terminal.visible)
        view.capture("Escape preserves the query", "/zzzz", picker=False)
        terminal.write(b"\x7f" * 4)
        _wait(lambda: "› /login" in terminal.visible)
        view.capture("Editing after Escape reopens", "/")

        terminal.write("codex")
        _wait(lambda: "› /login" in terminal.visible and "/codex" in terminal.visible)
        view.capture("Long description query stays left aligned", "/codex")
        terminal.write(b"\t")
        _wait(lambda: _PROMPT + "/login\n" in terminal.visible)
        view.capture("Tab chooses without executing", "/login", picker=False)

        # Selected rows must not inherit reverse video or a background block.
        codes = re.findall(rb"\x1b\[([0-9;]*)m", bytes(terminal.output[output_start:]))
        for code in codes:
            parts = code.split(b";")
            assert b"7" not in parts, code
            assert b"48" not in parts, code
        assert cluster.rpc("task.list") == []

        terminal.write(b"\x03")
        terminal.write("First line\x1b\rSecond line")
        _wait(lambda: "  · Second line\n" in terminal.visible)
        view.capture(
            "Multiline input stays between transcript and footer",
            "First line\nSecond line",
            picker=False,
            text_grows=True,
        )

        # Returning from real model output must not recreate the empty editor
        # area that used to separate the draft from its footer.
        terminal.write(b"\x03")
        task_output = len(terminal.text)
        terminal.write(_draft("picker-chat", action="plan") + "\r")
        assert cluster.settled("picker-chat")["state"] == "completed"
        terminal.prompt_after_task(task_output)
        after_task = _PickerView(terminal)
        after_task.capture("After a completed chat task", "", picker=False)
        terminal.write("/")
        _wait(lambda: "› /login" in terminal.visible)
        after_task.capture("Picker after a chat task", "/")
        terminal.write(b"\x1b")
        _wait(lambda: "browse commands" not in terminal.footer)
        after_task.capture("Closed picker after a chat task", "/", picker=False)
        view.captures.extend(after_task.captures)
        view.save(cluster.root / "command-picker-screen.txt")
        terminal.write(b"\x03")
        terminal.write("/detach\r")
        assert terminal.process.wait(timeout=5) == 0, terminal.text


def test_command_picker_enter_executes_selection_once(
    mode_cluster: _ModeCluster,
) -> None:
    cluster = mode_cluster
    with contextlib.closing(
        _Terminal(cluster, "picker-enter", rows=24, columns=80)
    ) as terminal:
        view = _PickerView(terminal)
        terminal.write("/stat")
        _wait(lambda: "› /status" in terminal.visible)
        view.capture("Status search", "/stat")
        output_start = len(terminal.text)
        terminal.write(b"\r")
        _wait(lambda: "Starts with your first message" in terminal.text[output_start:])
        _wait(
            lambda: (
                _PROMPT.rstrip() in terminal.visible and "Enter send" in terminal.footer
            )
        )
        assert terminal.text[output_start:].count("Starts with your first message") == 1
        assert cluster.rpc("task.list") == []
        terminal.write("/detach\r")
        assert terminal.process.wait(timeout=5) == 0, terminal.text
