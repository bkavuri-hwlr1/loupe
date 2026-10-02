"""Answer formatting survives chunk boundaries, narrow terminals, and interruption."""

from __future__ import annotations

import io
import re

import pytest

from llm_cli.cli.streaming_markdown import MarkdownStream
from llm_cli.cli.terminal import TerminalUI, safe_text


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def ui(monkeypatch: pytest.MonkeyPatch) -> TerminalUI:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.delenv("NO_COLOR", raising=False)
    result = TerminalUI(Terminal())
    result.console.width = 34
    return result


def _output(ui: TerminalUI) -> str:
    assert isinstance(ui.stream, io.StringIO)
    return ui.stream.getvalue()


def _visible(ui: TerminalUI) -> str:
    return "\n".join(line.rstrip() for line in safe_text(_output(ui)).splitlines())


def test_partial_paragraph_waits_for_stable_boundary_and_finishes_once(
    ui: TerminalUI,
) -> None:
    stream = MarkdownStream(ui)
    stream.feed("A **bold")
    assert _output(ui) == ""
    stream.feed(" answer**.\n\nNext para")
    # The paragraph separator waits for the next block, which may supply its own.
    assert _visible(ui) == "A bold answer."
    stream.feed("graph.")
    stream.finish()
    assert _visible(ui) == "A bold answer.\n\nNext paragraph."
    before = _output(ui)
    stream.finish()
    assert _output(ui) == before


@pytest.mark.parametrize("chunk_size", [1, 7, 1000])
def test_streamed_lists_and_quotes_keep_single_blank_line_separation(
    ui: TerminalUI, chunk_size: int
) -> None:
    text = "## Findings\n\nIntro.\n\n- one\n- two\n\n> quoted\n\n1. First.\n\nEnd."
    stream = MarkdownStream(ui)
    for offset in range(0, len(text), chunk_size):
        stream.feed(text[offset : offset + chunk_size])
    stream.finish()
    visible = _visible(ui)
    assert "\n\n\n" not in visible
    assert visible.index("Intro.") < visible.index("• one") < visible.index("quoted")
    assert "Intro.\n\n • one" in visible


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 1000])
def test_fences_split_across_chunks_highlight_code_on_narrow_terminals(
    ui: TerminalUI, chunk_size: int
) -> None:
    text = 'Example:\n\n```python\ndef answer():\n    return "violet"\n```\n\nDone.'
    stream = MarkdownStream(ui)
    for offset in range(0, len(text), chunk_size):
        stream.feed(text[offset : offset + chunk_size])
    stream.finish()
    visible = _visible(ui)
    assert "```" not in visible
    assert visible.count("def answer():") == 1
    assert visible.count('return "violet"') == 1
    assert visible.count("Example:") == visible.count("Done.") == 1
    assert "\x1b[" in _output(ui)
    assert re.search(r"\x1b\[[0-9;]*38;2;", _output(ui))


def test_blank_lines_and_shorter_or_wrong_fences_do_not_close_code(
    ui: TerminalUI,
) -> None:
    stream = MarkdownStream(ui)
    stream.feed("````text\nfirst\n\n```\n~~~\nlast\n")
    assert _output(ui) == ""
    stream.feed("````\n")
    assert "first" in _visible(ui) and "last" in _visible(ui)
    assert "```" in _visible(ui) and "~~~" in _visible(ui)
    assert "````" not in _visible(ui)


@pytest.mark.parametrize("width", [1, 2, 34])
def test_markdown_code_preserves_content_even_in_tiny_panes(
    ui: TerminalUI, width: int
) -> None:
    ui.console.width = width
    ui.body("```python\nreturn 42\n```", markdown=True)
    assert "".join(_visible(ui).split()) == "return42"
    assert "```" not in _visible(ui)


@pytest.mark.parametrize("fence", ["```python", "~~~python"])
def test_interruption_flushes_unclosed_code_and_later_feed_can_resume(
    ui: TerminalUI, fence: str
) -> None:
    stream = MarkdownStream(ui)
    stream.feed(fence + "\ndef interrupted():\n    return 42")
    assert _output(ui) == ""
    stream.finish()
    assert "def interrupted():" in _visible(ui)
    assert "return 42" in _visible(ui)
    assert fence not in _visible(ui)
    first = _output(ui)
    stream.finish()
    assert _output(ui) == first
    stream.feed("A later answer.")
    stream.finish()
    assert _visible(ui).count("return 42") == 1
    assert "A later answer." in _visible(ui)


def test_plain_streams_are_immediate_literal_and_never_repeat_finish() -> None:
    output = io.StringIO()
    stream = MarkdownStream(TerminalUI(output, plain=True))
    stream.feed("**raw")
    assert output.getvalue() == "**raw"
    stream.feed("**\n```py\nx = 1")
    assert output.getvalue() == "**raw**\n```py\nx = 1"
    stream.finish()
    stream.finish()
    assert output.getvalue() == "**raw**\n```py\nx = 1\n"


def test_split_controls_are_removed_before_markdown_rendering(ui: TerminalUI) -> None:
    stream = MarkdownStream(ui)
    stream.feed("Safe\x1b]52;c;")
    stream.feed("private\x07 **answer**\x1b[")
    stream.feed("2J.\n\n")
    stream.finish()
    assert _visible(ui).strip() == "Safe answer."
    assert "private" not in _output(ui)
    assert "\x1b[2J" not in _output(ui)


def test_no_color_preserves_long_code_and_table_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("NO_COLOR", "1")
    ui = TerminalUI(Terminal())
    ui.console.width = 24
    long_value = "unique_value_" * 30
    stream = MarkdownStream(ui)
    stream.feed("```python\n" + long_value + "\n```\n\n")
    stream.feed("| Header | Value |\n| --- | --- |\n| cell | " + long_value + " |\n\n")
    stream.finish()
    compact = "".join(_visible(ui).split())
    assert compact.count(long_value) == 2
    assert "…" not in _visible(ui)
    assert not re.search(r"\x1b\[[0-9;]*(?:38|48);", _output(ui))


def test_activity_fallback_deduplicates_and_clears() -> None:
    output = io.StringIO()
    ui = TerminalUI(output, plain=True)
    ui.activity("Reading file")
    ui.activity("Reading file")
    ui.activity("Checking\nfiles\x1b[2J")
    ui.activity(None)
    ui.activity("Checking files")
    assert output.getvalue() == "Reading file\nChecking files\nChecking files\n"
