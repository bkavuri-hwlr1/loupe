"""Render stable Markdown blocks without exposing partial markup while streaming."""

from __future__ import annotations

import re

from llm_cli.cli.terminal import TerminalUI, TextSanitizer

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_PARAGRAPH_END = re.compile(r"\n[ \t]*\n$")


class MarkdownStream:
    """Commit complete paragraphs and fenced blocks once, then flush on finish.

    Plain streams remain immediate and literal. On terminals, an unfinished
    paragraph or code block is held until a stable boundary or finish(), so
    chunked formatting tokens never briefly appear as ordinary answer text.
    """

    def __init__(self, ui: TerminalUI) -> None:
        self.ui = ui
        self._sanitizer = TextSanitizer()
        self._partial = ""
        self._block: list[str] = []
        self._fence: str | None = None
        self._plain_line_open = False

    def feed(self, cleaned_text: str) -> None:
        cleaned = self._sanitizer.feed(cleaned_text)
        if not cleaned:
            return
        if self.ui.plain:
            self.ui.delta(cleaned)
            self._plain_line_open = not cleaned.endswith("\n")
            return
        lines = (self._partial + cleaned).split("\n")
        self._partial = lines.pop()
        for line in lines:
            self._accept_line(line + "\n")

    def finish(self) -> None:
        """Flush interrupted or completed text; repeated calls add no output."""

        if self.ui.plain:
            if self._plain_line_open:
                self.ui.delta("\n")
                self._plain_line_open = False
            return
        if self._partial:
            partial, self._partial = self._partial, ""
            self._accept_line(partial)
        self._emit()
        self._fence = None

    def _accept_line(self, line: str) -> None:
        match = _FENCE.fullmatch(line.removesuffix("\n"))
        if self._fence is not None:
            self._block.append(line)
            if (
                match is not None
                and match[1][0] == self._fence[0]
                and len(match[1]) >= len(self._fence)
                and not match[2].strip()
            ):
                self._emit()
                self._fence = None
            return
        if match is not None and (match[1][0] != "`" or "`" not in match[2]):
            self._emit()
            self._fence = match[1]
        self._block.append(line)
        if not line.strip():
            self._emit()

    def _emit(self) -> None:
        if not self._block:
            return
        text = "".join(self._block)
        self._block.clear()
        if text.strip():
            self.ui.body(text, markdown=True)
            # Rich discards trailing paragraph separators when rendering one
            # block at a time. Keep the separation between committed paragraphs.
            if _PARAGRAPH_END.search(text):
                self.ui.notice("")
        else:
            self.ui.notice("")


__all__ = ["MarkdownStream"]
