"""The active footer preserves transcript text through terminal redraws."""

from __future__ import annotations

import io
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from prompt_toolkit.input import create_pipe_input

from llm_cli.cli import active_controls, session
from llm_cli.cli.composer import Composer
from llm_cli.cli.render import EventRenderer
from llm_cli.cli.status import TaskStatusUI
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient

_STATUS = "Model: demo · Effort: high · Mode: normal"
_CSI = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])")


class Terminal(io.StringIO):
    """Interpret the cursor/erase operations Rich emits, including scrollback.

    Stripping ANSI alone hides duplicate or overwritten text, so assertions use
    the resulting screen and saved rows rather than the raw escape stream.
    """

    def __init__(self, width: int = 60, height: int = 6) -> None:
        super().__init__()
        self.width = width
        self.height = height
        self.rows = [""] * height
        self.x = 0
        self.y = 0
        self.top = 0
        self._escape = ""

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> int:
        super().write(text)
        remaining = self._escape + text
        self._escape = ""
        while remaining:
            character = remaining[0]
            if character == "\x1b":
                match = _CSI.match(remaining)
                if match is None:
                    self._escape = remaining
                    break
                argument, _, command = match.groups()
                if command == "A":
                    self.y = max(self.top, self.y - int(argument or 1))
                elif command == "H":
                    row, column = argument.split(";")
                    self.y = self.top + int(row) - 1
                    self.x = int(column) - 1
                elif command == "K":
                    assert argument == "2"
                    self.rows[self.y] = ""
                else:
                    assert command in {"m", "h", "l"}, match.group()
                remaining = remaining[match.end() :]
                continue
            remaining = remaining[1:]
            if character == "\r":
                self.x = 0
            elif character == "\n":
                self._newline()
            else:
                if self.x == self.width:
                    self._newline()
                row = self.rows[self.y].ljust(self.x)
                self.rows[self.y] = row[: self.x] + character + row[self.x + 1 :]
                self.x += 1
        return len(text)

    def _newline(self) -> None:
        self.x = 0
        self.y += 1
        if self.y == self.top + self.height:
            self.rows.append("")
            self.top += 1

    @property
    def transcript(self) -> str:
        return "\n".join(row.rstrip() for row in self.rows).strip("\n")


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> Terminal:
    monkeypatch.setenv("TERM", "xterm-256color")
    return Terminal()


def _ui(output: Terminal) -> TaskStatusUI:
    result = TaskStatusUI(output, status=lambda: _STATUS)
    result.console.width = output.width
    result.console.height = output.height
    return result


def test_streamed_chunks_remain_contiguous_above_footer(terminal: Terminal) -> None:
    footer = "─" * terminal.width + "\n" + _STATUS
    with _ui(terminal) as ui:
        assert terminal.rows[:2] == ["─" * terminal.width, _STATUS]
        ui.delta("Hello ")
        assert terminal.transcript == "Hello\n" + footer
        ui.delta("world")
        assert terminal.transcript == "Hello world\n" + footer
        ui.delta("\nSecond\nThird")
        assert terminal.transcript == "Hello world\nSecond\nThird\n" + footer
        assert terminal.rows[terminal.y] == _STATUS
    assert terminal.transcript == "Hello world\nSecond\nThird"
    assert "\x1b[?25h" in terminal.getvalue()


def test_activity_replaces_one_row_without_adding_to_transcript(
    terminal: Terminal,
) -> None:
    footer = "─" * terminal.width + "\n" + _STATUS
    with _ui(terminal) as ui:
        ui.delta("Existing response\n")
        ui.activity("Inspecting the project")
        assert (
            terminal.transcript
            == "Existing response\nInspecting the project\n" + footer
        )
        before = terminal.getvalue()
        ui.activity("Inspecting the project")
        assert terminal.getvalue() == before
        ui.activity("Preparing changes")
        assert terminal.transcript == "Existing response\nPreparing changes\n" + footer
        ui.activity(None)
        assert terminal.transcript == "Existing response\n" + footer
    assert terminal.transcript == "Existing response"


def test_activity_updates_preserve_partial_reply_and_stop_notice(
    terminal: Terminal,
) -> None:
    with _ui(terminal) as ui:
        ui.delta("Partial ")
        ui.activity("Inspecting the project")
        ui.notice("Stop requested")
        ui.activity("Stopping")
        ui.delta("sentence.")
        assert terminal.transcript.splitlines() == [
            "Stop requested",
            "Partial sentence.",
            "Stopping",
            "─" * terminal.width,
            _STATUS,
        ]
    assert terminal.transcript == "Stop requested\nPartial sentence."


def test_activity_is_safe_single_line_and_clipped_to_terminal_width(
    terminal: Terminal,
) -> None:
    with _ui(terminal) as ui:
        ui.activity("Reading\n\t\x1b[31mproject\x1b[0m " + "details " * 30)
        rows = terminal.transcript.splitlines()
        assert len(rows) == 3
        assert rows[0].startswith("Reading project details")
        assert rows[0].endswith("…")
        assert len(rows[0]) == terminal.width
        assert rows[1:] == ["─" * terminal.width, _STATUS]
        ui.activity(" \n\t ")
        assert terminal.transcript.splitlines() == ["─" * terminal.width, _STATUS]
    assert terminal.transcript == ""


def test_many_activity_updates_never_replace_or_repeat_reply_lines(
    terminal: Terminal,
) -> None:
    expected = [f"Reply line {index}" for index in range(100)]
    with _ui(terminal) as ui:
        for index, line in enumerate(expected):
            ui.activity(f"Working on step {index}")
            ui.delta(line + "\n")
        assert terminal.transcript.splitlines() == [
            *expected,
            "Working on step 99",
            "─" * terminal.width,
            _STATUS,
        ]
    assert terminal.transcript.splitlines() == expected


def test_stop_flushes_reply_and_restart_does_not_restore_stale_activity(
    terminal: Terminal,
) -> None:
    ui = _ui(terminal)
    ui.start()
    ui.activity("Preparing changes")
    ui.delta("Saved partial reply")
    ui.stop()
    assert terminal.transcript == "Saved partial reply"
    ui.start()
    assert terminal.transcript.splitlines() == [
        "Saved partial reply",
        "─" * terminal.width,
        _STATUS,
    ]
    ui.stop()
    assert terminal.transcript == "Saved partial reply"


def test_activity_yields_its_row_to_partial_reply_in_three_row_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    terminal = Terminal(height=3)
    with _ui(terminal) as ui:
        ui.activity("Preparing changes")
        ui.delta("Partial ")
        ui.activity("Checking changes")
        ui.delta("reply")
        assert terminal.transcript.splitlines() == [
            "Partial reply",
            "─" * terminal.width,
            _STATUS,
        ]
    assert terminal.transcript == "Partial reply"


def test_answer_preview_shows_its_tail_and_never_reaches_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    terminal = Terminal(height=10)
    footer = ["─" * terminal.width, _STATUS]
    with _ui(terminal) as ui:
        ui.delta("Earlier reply\n")
        ui.activity("Thinking…")
        draft = "First line.\nSecond line.\nThird line.\nFourth line."
        ui.preview(draft)
        assert terminal.transcript.splitlines() == [
            "Earlier reply",
            f"Writing the answer… ({len(draft)} characters)",
            "  Second line.",
            "  Third line.",
            "  Fourth line.",
            *footer,
        ]
        ui.preview(None)
        assert terminal.transcript.splitlines() == [
            "Earlier reply",
            "Thinking…",
            *footer,
        ]
        ui.preview("Unaccepted draft")
    assert terminal.transcript == "Earlier reply"


def test_answer_preview_shrinks_to_fit_a_small_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    terminal = Terminal(height=4)
    with _ui(terminal) as ui:
        ui.preview("one\ntwo\nthree")
        assert terminal.transcript.splitlines() == [
            "Writing the answer… (13 characters)",
            "  three",
            "─" * terminal.width,
            _STATUS,
        ]
    assert terminal.transcript == ""


def test_plan_step_sits_above_activity_and_yields_in_a_small_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    terminal = Terminal(height=6)
    footer = ["─" * terminal.width, _STATUS]
    with _ui(terminal) as ui:
        ui.delta("Earlier reply\n")
        ui.progress("Step 2 of 3 · Add tests")
        ui.activity("Searching the codebase…")
        assert terminal.transcript.splitlines() == [
            "Earlier reply",
            "Step 2 of 3 · Add tests",
            "Searching the codebase…",
            *footer,
        ]
        ui.progress(None)
        assert terminal.transcript.splitlines() == [
            "Earlier reply",
            "Searching the codebase…",
            *footer,
        ]
    assert terminal.transcript == "Earlier reply"

    small = Terminal(height=3)
    with _ui(small) as ui:
        ui.progress("Step 1 of 2 · Read")
        ui.activity("Thinking…")
        assert small.transcript.splitlines() == [
            "Thinking…",
            "─" * small.width,
            _STATUS,
        ]
    assert small.transcript == ""


def test_answer_preview_without_a_live_region_only_announces_progress() -> None:
    output = io.StringIO()
    ui = TaskStatusUI(output, status=lambda: _STATUS, plain=True)
    with ui:
        ui.preview("A provisional draft")
        ui.preview("A provisional draft, longer")
        ui.preview(None)
    assert output.getvalue() == "Writing the answer…\n"


def test_streamed_markdown_and_progress_share_live_footer_without_replays(
    terminal: Terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.delenv("NO_COLOR", raising=False)
    answer = (
        "Here is the answer.\n\nUse the function below.\n\n"
        '```python\ndef answer():\n    return "violet"\n```\n\n'
        "Ready to use."
    )
    renderer = EventRenderer(terminal)
    with _ui(terminal) as ui:
        renderer.ui = ui

        def event(kind: str, **payload: object) -> None:
            renderer.render(
                {"event_type": kind, "payload": {"turn_id": "one", **payload}}
            )
            assert terminal.transcript.splitlines()[-2:] == [
                "─" * terminal.width,
                _STATUS,
            ]

        event("model.turn.started")
        event("model.reasoning.delta", text="Private reasoning must stay hidden")
        event(
            "model.tool_call",
            tool="read_file",
            arguments={"path": "private_config.py"},
        )
        event("model.tool_result", tool="read_file", content="RAW_FILE_PAYLOAD")
        assert terminal.transcript.splitlines() == [
            "Reviewing project files…",
            "─" * terminal.width,
            _STATUS,
        ]
        event("model.text.delta", text="Here is the answer.\n\n")
        event("model.text.delta", text="Use the function below.\n\n")
        code_start = len(terminal.getvalue())
        for chunk in [
            "`",
            "``py",
            "thon\n",
            "def ans",
            "wer():\n",
            '    return "violet"\n',
            "`",
            "``\n\n",
        ]:
            event("model.text.delta", text=chunk)
        code_output = terminal.getvalue()[code_start:]
        assert re.search(r"\x1b\[[0-9;]*38;2;[0-9;]*mdef", code_output)
        event("model.text.delta", text="Ready to use.")
        event("model.said", text=answer)
        event("model.finished", summary=answer)
        renderer.finish()
        assert terminal.transcript.splitlines()[-2:] == [
            "─" * terminal.width,
            _STATUS,
        ]
    transcript = terminal.transcript
    for text in [
        "Here is the answer.",
        "Use the function below.",
        "def answer():",
        'return "violet"',
        "Ready to use.",
    ]:
        assert transcript.count(text) == 1, transcript
    for hidden in [
        "Private reasoning",
        "private_config.py",
        "RAW_FILE_PAYLOAD",
        "Reviewing project files",
        "```",
        _STATUS,
    ]:
        assert hidden not in transcript
    assert "Private reasoning" not in terminal.getvalue()
    assert "RAW_FILE_PAYLOAD" not in terminal.getvalue()


def test_footer_stays_next_to_partial_text_when_terminal_grows(
    terminal: Terminal,
) -> None:
    with _ui(terminal) as ui:
        ui.delta("Hello ")
        terminal.rows.extend([""] * 8)
        terminal.height += 8
        ui.console.height = terminal.height
        ui.delta("world")
        assert terminal.rows[:3] == ["Hello world", "─" * terminal.width, _STATUS]
        assert terminal.y == 2
    assert terminal.transcript == "Hello world"


def test_footer_follows_existing_output_without_blank_rows_or_screen_repositioning(
    terminal: Terminal,
) -> None:
    terminal.write("Previous response\n")
    with _ui(terminal) as ui:
        assert terminal.rows[:3] == [
            "Previous response",
            "─" * terminal.width,
            _STATUS,
        ]
        ui.delta("New response\n")
        assert terminal.rows[:4] == [
            "Previous response",
            "New response",
            "─" * terminal.width,
            _STATUS,
        ]
    assert terminal.transcript == "Previous response\nNew response"
    assert "\x1b[?1049" not in terminal.getvalue()
    assert not re.search(r"\x1b\[[0-9;]*r", terminal.getvalue())
    assert not re.search(r"\x1b\[[0-9;]*H", terminal.getvalue())


def test_stop_notice_uses_live_output_without_splitting_streamed_sentence(
    terminal: Terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    notified = threading.Event()
    calls: list[tuple[str, object]] = []

    class Client:
        def call(self, method: str, params: object) -> object:
            calls.append((method, params))
            return {}

    with create_pipe_input() as keyboard, _ui(terminal) as ui:
        monkeypatch.setattr(active_controls, "create_input", lambda **_: keyboard)

        def notice(message: str) -> None:
            ui.notice(message)
            notified.set()

        with active_controls.active_controls(
            cast(DaemonClient, Client()),
            "task",
            terminal,
            terminal,
            notice=notice,
        ):
            ui.delta("Partial ")
            keyboard.send_text("/stop\r")
            assert notified.wait(timeout=2)
            ui.delta("sentence.\n")
    assert calls == [("task.cancel", {"task_id": "task"})]
    assert terminal.transcript.count("Partial sentence.") == 1
    assert "Stop requested; retaining pending edits." in terminal.transcript
    assert _STATUS not in terminal.transcript


def test_unbroken_response_larger_than_screen_is_never_truncated(
    terminal: Terminal,
) -> None:
    text = "".join(f"word{index:04d}_" for index in range(1000))
    with _ui(terminal) as ui:
        for offset in range(0, len(text), 73):
            ui.delta(text[offset : offset + 73])
        assert terminal.transcript.splitlines()[-1] == _STATUS
        assert terminal.transcript.splitlines()[-2] == "─" * terminal.width
        assert "".join(terminal.transcript.splitlines()[:-2]) == text
    assert "".join(terminal.transcript.splitlines()) == text
    assert "…" not in terminal.transcript


def test_multiline_output_and_notices_do_not_overwrite_partial_text(
    terminal: Terminal,
) -> None:
    lines = [f"line {index:03d}" for index in range(100)]
    with _ui(terminal) as ui:
        ui.delta("\n".join(lines) + "\nPartial ")
        ui.notice("Stop requested", style="warning")
        ui.delta("sentence.\n")
        ui.code("one\ntwo\nthree")
    assert terminal.transcript.splitlines() == [
        *lines,
        "Stop requested",
        "Partial sentence.",
        "one",
        "two",
        "three",
    ]


def test_footer_removes_itself_on_exception_without_losing_last_chunk(
    terminal: Terminal,
) -> None:
    with pytest.raises(RuntimeError), _ui(terminal) as ui:
        ui.activity("Preparing changes")
        ui.delta("saved partial")
        raise RuntimeError("lost transport")
    assert terminal.transcript == "saved partial"


@pytest.mark.parametrize("no_color", [False, True])
def test_footer_divider_is_violet_without_highlighting_and_respects_no_color(
    terminal: Terminal, monkeypatch: pytest.MonkeyPatch, no_color: bool
) -> None:
    monkeypatch.setenv("COLORTERM", "truecolor")
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
    with _ui(terminal):
        assert terminal.rows[:2] == ["─" * terminal.width, _STATUS]
        styles = [
            match.group(1).split(";")
            for match in _CSI.finditer(terminal.getvalue())
            if match.group(3) == "m"
        ]
        assert all("1" not in style and "7" not in style for style in styles)
        assert all("48" not in style for style in styles)
        # Rich can reuse the 256-color style cached by an earlier Console.
        # Both encodings represent violet; the contract is color, not depth.
        violet = ("38;2;167;139;250", "38;5;141")
        assert any(code in terminal.getvalue() for code in violet) is not no_color
    assert terminal.transcript == ""


@pytest.mark.parametrize(
    "plain,tty,term",
    [(True, True, "xterm"), (False, False, "xterm"), (False, True, "dumb")],
)
def test_plain_and_redirected_output_remain_byte_identical(
    monkeypatch: pytest.MonkeyPatch, plain: bool, tty: bool, term: str
) -> None:
    monkeypatch.setenv("TERM", term)
    output = Terminal() if tty else io.StringIO()
    with TaskStatusUI(output, status=lambda: _STATUS, plain=plain) as ui:
        ui.delta("part")
        ui.delta("ial\nsecond")
        ui.notice(" notice")
    assert output.getvalue() == "partial\nsecond notice\n"


@pytest.mark.parametrize(
    "plain,tty,term",
    [(True, True, "xterm"), (False, False, "xterm"), (False, True, "dumb")],
)
def test_activity_fallback_deduplicates_without_terminal_redraws(
    monkeypatch: pytest.MonkeyPatch, plain: bool, tty: bool, term: str
) -> None:
    monkeypatch.setenv("TERM", term)
    output = Terminal() if tty else io.StringIO()
    with TaskStatusUI(output, status=lambda: _STATUS, plain=plain) as ui:
        ui.activity("Inspecting the project")
        ui.activity("Inspecting the project")
        ui.activity("Preparing changes")
        ui.activity(None)
        ui.activity("Preparing changes")
    assert output.getvalue().splitlines() == [
        "Inspecting the project",
        "Preparing changes",
        "Preparing changes",
    ]


def test_follow_releases_footer_for_questions_and_keeps_it_across_idle(
    terminal: Terminal, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lifecycle: list[str] = []

    class TrackingUI(TaskStatusUI):
        def start(self) -> None:
            lifecycle.append("start")
            self.console.width = terminal.width
            self.console.height = terminal.height
            super().start()

        def stop(self) -> None:
            lifecycle.append("stop")
            super().stop()

    @contextmanager
    def controls(*args: object, **kwargs: object) -> Iterator[None]:
        assert callable(kwargs["notice"])
        lifecycle.append("controls start")
        yield
        lifecycle.append("controls stop")

    class Client:
        paths = AppPaths("test", tmp_path, tmp_path, tmp_path, tmp_path)
        attaches = 0

        def stream(self, method: str, params: object) -> Iterator[dict[str, Any]]:
            self.attaches += 1
            assert _STATUS in terminal.transcript
            if self.attaches == 1:
                yield {
                    "sequence": 1,
                    "event_type": "model.text.delta",
                    "payload": {"text": "Before question"},
                }
            elif self.attaches == 2:
                yield {
                    "sequence": 2,
                    "event_type": "question.asked",
                    "payload": {"question": "Choose?", "question_id": "q"},
                }
            else:
                yield {
                    "sequence": 3,
                    "event_type": "model.said",
                    "payload": {"text": "After answer"},
                }

        def call(self, method: str, params: object) -> object:
            if method == "task.question":
                return {"pending": True, "question_id": "q"}
            assert method == "task.show"
            return {"state": "running" if self.attaches == 1 else "completed"}

    def answer(*args: object, **kwargs: object) -> bool:
        lifecycle.append("answer")
        assert _STATUS not in terminal.transcript
        assert "Before question" in terminal.transcript
        terminal.write("Question and answer\n")
        return True

    monkeypatch.setattr(session, "TaskStatusUI", TrackingUI)
    monkeypatch.setattr(session, "active_controls", controls)
    monkeypatch.setattr(session, "_answer", answer)
    editor = Composer(io.StringIO(), io.StringIO(), plain=True)
    monkeypatch.setattr(editor, "status_text", lambda: _STATUS)
    session._follow(
        cast(DaemonClient, Client()),
        task_id="task",
        stream=terminal,
        input_stream=io.StringIO(),
        composer=editor,
    )
    assert lifecycle == [
        "start",
        "controls start",
        "controls stop",
        "controls start",
        "controls stop",
        "stop",
        "answer",
        "start",
        "controls start",
        "controls stop",
        "stop",
    ]
    assert _STATUS not in terminal.transcript
    assert terminal.transcript.count("Before question") == 1
    assert terminal.transcript.count("After answer") == 1
    assert "Question and answer" in terminal.transcript
