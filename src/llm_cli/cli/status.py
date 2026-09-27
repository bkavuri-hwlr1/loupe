"""A transient task footer that preserves streaming text and terminal scrollback."""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import TracebackType
from typing import TextIO

from rich.cells import chop_cells
from rich.console import Group
from rich.live import Live
from rich.rule import Rule
from rich.style import Style
from rich.text import Text

from llm_cli.cli.terminal import TerminalUI, safe_text


class TaskStatusUI(TerminalUI):
    """Keep metadata below active output without owning terminal input.

    Complete lines are ordinary scrollback. Only the unfinished visual row,
    current activity, divider, and metadata belong to Live, so an arbitrarily
    long response cannot overflow or be cropped by its transient region. Other
    UI primitives share Live's Console.
    """

    def __init__(
        self,
        stream: TextIO,
        *,
        status: Callable[[], str] | None = None,
        plain: bool = False,
    ) -> None:
        super().__init__(stream, plain=plain)
        self._status = status
        self._live: Live | None = None
        self._pending = Text()
        self._activity_text: str | None = None
        self._lock = threading.RLock()

    def __enter__(self) -> TaskStatusUI:
        self.start()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    def start(self) -> None:
        with self._lock:
            if (
                self._live is not None
                or self.plain
                or self._status is None
                or self.console.width < 2
                or self.console.height < 3
            ):
                return
            self._live = Live(
                self._renderable(),
                console=self.console,
                auto_refresh=False,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
                vertical_overflow="visible",
            )
            self._live.start(refresh=True)

    def stop(self) -> None:
        with self._lock:
            self._activity_text = None
            if self._live is None:
                super().activity(None)
                return
            self._update()
            if self._pending:
                self.delta("\n")
            self._live.stop()
            self._live = None
            super().activity(None)

    def activity(self, text: str | None) -> None:
        """Replace the current progress line without committing it to history."""

        cleaned = " ".join(safe_text(text).split()) if text is not None else ""
        current = cleaned or None
        with self._lock:
            if current == self._activity_text:
                return
            self._activity_text = current
            if self._live is None:
                super().activity(current)
                return
            self._update()

    def delta(self, text: str, *, style: str = "") -> None:
        with self._lock:
            if self._live is None:
                super().delta(text, style=style)
                return
            # A provider chunk may contain multiple lines or no newline at all.
            # Keep its final row live while immediately committing every other
            # row, without waiting for an eventual completion event.
            parts = text.split("\n")
            for index, part in enumerate(parts):
                self._pending.append(part, style=style)
                self._pending.expand_tabs()
                self._fold_pending()
                if index < len(parts) - 1:
                    complete = self._pending
                    self._pending = Text()
                    self._update()
                    self.console.print(complete, soft_wrap=True)
            self._update()

    def notice(self, text: str, *, style: str = "muted") -> None:
        # /stop can arrive on the existing input thread between text chunks.
        # Print its notice above the unfinished row without splicing the notice
        # into the provider's sentence or racing a live-region update.
        with self._lock:
            if self._live is not None:
                self._fold_pending()
                self._update()
            super().notice(text, style=style)

    def _fold_pending(self) -> None:
        rows = chop_cells(self._pending.plain, max(2, self.console.width))
        if len(rows) < 2:
            return
        offset = 0
        complete: list[Text] = []
        for row in rows[:-1]:
            complete.append(self._pending[offset : offset + len(row)])
            offset += len(row)
        self._pending = self._pending[offset:]
        self._update()
        self.console.print(Text("\n").join(complete), soft_wrap=True)

    def _renderable(self) -> Group:
        status = safe_text(self._status() if self._status else "").replace("\n", " ")
        divider = Rule(
            characters="─",
            style=Style(
                color=None if self.console.no_color else "#a78bfa",
                bgcolor="default",
                bold=False,
                reverse=False,
            ),
        )
        footer = Text(
            status,
            style=Style(dim=True, bold=False, reverse=False, bgcolor="default"),
            no_wrap=True,
            overflow="ellipsis",
        )
        rows: list[Text | Rule] = []
        if self._pending:
            rows.append(self._pending)
        # A live region taller than the screen leaks its first row into
        # scrollback on redraw. Reply text takes priority in a tiny viewport.
        if self._activity_text and self.console.height >= 3 + bool(self._pending):
            rows.append(
                Text(
                    self._activity_text,
                    style=Style(dim=True, bold=False, reverse=False, bgcolor="default"),
                    no_wrap=True,
                    overflow="ellipsis",
                )
            )
        return Group(*rows, divider, footer)

    def _update(self) -> None:
        if self._live is not None:
            # Live.update without refresh leaves the render hook pointing at
            # the old row; the next ordinary print would repeat that row.
            self._live.update(self._renderable(), refresh=True)


__all__ = ["TaskStatusUI"]
