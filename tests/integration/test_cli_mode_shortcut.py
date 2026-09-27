"""Shift+Tab traverses prompt_toolkit through a real controlling terminal."""

from __future__ import annotations

import codecs
import contextlib
import fcntl
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import threading

from test_cli_modes import _ModeCluster
from test_cli_modes import mode_cluster as mode_cluster
from test_cli_process_orchestration import _wait
from wcwidth import wcwidth

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")


class _Screen:
    """Track the VT100 screen and answer the editor's cursor-position requests."""

    def __init__(self, rows: int = 36, columns: int = 160) -> None:
        self.rows, self.columns = rows, columns
        self.cells = [[" "] * columns for _ in range(rows)]
        self.row = self.column = 0
        self.saved = (0, 0)
        self.top, self.bottom = 0, rows - 1
        self.scrolls = 0
        self.wrap = True
        self.cursor_visible = True
        self.pending = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def _scroll(self, count: int = 1) -> None:
        for _ in range(count):
            self.cells.pop(self.top)
            self.cells.insert(self.bottom, [" "] * self.columns)
            self.scrolls += 1

    def _newline(self) -> None:
        if self.row == self.bottom:
            self._scroll()
        else:
            self.row = min(self.rows - 1, self.row + 1)

    def _csi(self, command: str, raw: str) -> bytes | None:
        values = [int(value or "0") for value in raw.lstrip("?").split(";")]
        first = values[0]
        count = first or 1
        if command == "n" and first == 6:
            column = min(self.column, self.columns - 1) + 1
            return f"\x1b[{self.row + 1};{column}R".encode()
        if command in {"h", "l"}:
            if raw.startswith("?") and 7 in values:
                self.wrap = command == "h"
            if raw.startswith("?") and 25 in values:
                self.cursor_visible = command == "h"
        elif command in {"H", "f"}:
            self.row = min(self.rows - 1, count - 1)
            self.column = (
                min(self.columns - 1, (values[1] or 1) - 1) if len(values) > 1 else 0
            )
        elif command == "A":
            self.row = max(0, self.row - count)
        elif command in {"B", "E"}:
            self.row = min(self.rows - 1, self.row + count)
            if command == "E":
                self.column = 0
        elif command == "F":
            self.row, self.column = max(0, self.row - count), 0
        elif command == "C":
            self.column = min(self.columns - 1, self.column + count)
        elif command == "D":
            self.column = max(0, self.column - count)
        elif command == "G":
            self.column = min(self.columns - 1, count - 1)
        elif command == "d":
            self.row = min(self.rows - 1, count - 1)
        elif command in {"J", "K"}:
            for row in range(self.rows) if command == "J" else [self.row]:
                for col in range(self.columns):
                    if (
                        first == 2
                        or (first == 0 and (row, col) >= (self.row, self.column))
                        or (first == 1 and (row, col) <= (self.row, self.column))
                    ):
                        self.cells[row][col] = " "
        elif command == "S":
            self._scroll(count)
        elif command == "r":
            self.top = max(0, count - 1)
            self.bottom = (
                min(self.rows, values[1] or self.rows) - 1
                if len(values) > 1
                else self.rows - 1
            )
            self.row = self.column = 0
        elif command == "s":
            self.saved = self.row, self.column
        elif command == "u":
            self.row, self.column = self.saved
        return None

    def feed(self, chunk: bytes) -> list[bytes]:
        self.pending += self.decoder.decode(chunk)
        replies: list[bytes] = []
        while self.pending:
            text = self.pending
            char = text[0]
            consumed = 1
            if char == "\x1b":
                if len(text) < 2:
                    break
                if text[1] == "[":
                    end = next(
                        (i for i in range(2, len(text)) if "@" <= text[i] <= "~"), None
                    )
                    if end is None:
                        break
                    raw = text[2:end]
                    if re.fullmatch(r"\??[0-9;]*", raw):
                        reply = self._csi(text[end], raw)
                        if reply:
                            replies.append(reply)
                    consumed = end + 1
                elif text[1] == "]":
                    end = re.search(r"\x07|\x1b\\", text)
                    if end is None:
                        break
                    consumed = end.end()
                else:
                    consumed = 2
                    if text[1] == "7":
                        self.saved = self.row, self.column
                    elif text[1] == "8":
                        self.row, self.column = self.saved
            elif char == "\r":
                self.column = 0
            elif char == "\n":
                self._newline()
            elif char == "\b":
                self.column = max(0, self.column - 1)
            elif char == "\t":
                self.column = min(self.columns - 1, (self.column // 8 + 1) * 8)
            elif (width := wcwidth(char)) > 0:
                if self.column + width > self.columns:
                    if self.wrap:
                        self.column = 0
                        self._newline()
                    else:
                        self.column = self.columns - width
                self.cells[self.row][self.column] = char
                for extra in range(1, width):
                    self.cells[self.row][self.column + extra] = ""
                self.column += width
            self.pending = text[consumed:]
        return replies

    def lines(self) -> list[str]:
        return ["".join(row).rstrip() for row in self.cells]


class _Terminal:
    def __init__(
        self,
        cluster: _ModeCluster,
        name: str,
        *,
        mode: str | None = None,
        resume: str | None = None,
        rows: int = 36,
        columns: int = 160,
    ) -> None:
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        self.output = bytearray()
        self.screen = _Screen(rows=rows, columns=columns)
        self.screen_lock = threading.Lock()
        self.stopping = threading.Event()
        self.log_path = cluster.root / f"{name}.terminal.log"
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from test_cli_interrupts import _terminal_entry; "
                    "_terminal_entry()",
                    "--profile",
                    "test",
                    "chat",
                    "--repo",
                    str(cluster.repository),
                    "--scope",
                    "docs/",
                    *(
                        ["--resume", resume]
                        if resume
                        else [
                            "--provider",
                            "process-test",
                            "--model",
                            name,
                        ]
                    ),
                    *(["--mode", mode] if mode else []),
                ],
                env={
                    key: value
                    for key, value in {**cluster.env, "TERM": "xterm-256color"}.items()
                    if key != "PROMPT_TOOLKIT_NO_CPR"
                },
                cwd=cluster.repository,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                text=True,
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        cluster.processes.append(self.process)
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()
        try:
            _wait(lambda: "❯" in self.text)  # noqa: RUF001
        except BaseException:
            self.close()
            raise

    def _pump(self) -> None:
        with self.log_path.open("wb", buffering=0) as log:
            while not self.stopping.is_set():
                ready, _, _ = select.select([self.master], [], [], 0.05)
                if not ready:
                    continue
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                self.output.extend(chunk)
                log.write(chunk)
                with self.screen_lock:
                    replies = self.screen.feed(chunk)
                for reply in replies:
                    os.write(self.master, reply)

    @property
    def text(self) -> str:
        return _ANSI.sub("", bytes(self.output).decode("utf-8", errors="replace"))

    def write(self, value: str | bytes) -> None:
        assert self.process.poll() is None, self.text
        os.write(self.master, value.encode() if isinstance(value, str) else value)

    @property
    def footer(self) -> str:
        with self.screen_lock:
            lines = self.screen.lines()
        rows = [
            index
            for index, line in enumerate(lines)
            if line.startswith(" Model:") and "Effort:" in line and "Mode:" in line
        ]
        return "\n".join(lines[rows[-1] : rows[-1] + 2]) if rows else ""

    def assert_compact_editor(self) -> tuple[int, int]:
        with self.screen_lock:
            lines = self.screen.lines()
        prompt_row = max(
            index
            for index, line in enumerate(lines)
            if line.startswith("  ❯")  # noqa: RUF001
        )
        status_row = max(
            index for index, line in enumerate(lines) if line.startswith(" Model:")
        )
        visible = "\n".join(lines)
        assert lines[prompt_row - 1] == "─" * self.screen.columns, visible
        assert lines[status_row - 1] == "─" * self.screen.columns, visible
        assert all(lines[prompt_row : status_row - 1]), visible
        assert (
            "Enter send" in lines[status_row + 1]
            or "still running" in lines[status_row + 1]
        ), visible
        return prompt_row, status_row

    @property
    def visible(self) -> str:
        with self.screen_lock:
            return "\n".join(self.screen.lines())

    def cycle(self, expected: str, *, error: str | None = None) -> None:
        _wait(lambda: "Model:" in self.footer and "Effort:" in self.footer)
        before_geometry = self.assert_compact_editor()
        with self.screen_lock:
            scrolls = self.screen.scrolls
            before = "\n".join(self.screen.lines())
        prior = len(self.text)
        self.write(b"\x1b[Z")
        _wait(
            lambda: (
                f"Mode: {expected}" in self.footer
                and (error is None or error in self.footer)
            )
        )
        with self.screen_lock:
            after = "\n".join(self.screen.lines())
            assert self.screen.scrolls == scrolls, after
        assert after.count("❯") == before.count("❯")  # noqa: RUF001
        assert after.count("─") == before.count("─")
        assert f"Mode: {expected}." not in self.text[prior:]
        assert "Model:" in self.footer and "Effort:" in self.footer
        assert self.assert_compact_editor() == before_geometry

    def prompt_after_task(self, after: int) -> None:
        def ready() -> bool:
            recent = self.text[after:]
            return "❯" in recent and (  # noqa: RUF001
                "Enter send" in self.footer or "Draft cleared." in self.footer
            )

        _wait(ready)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.stopping.set()
        self.reader.join(timeout=1)
        os.close(self.master)


def _draft(label: str, **kwargs: object) -> str:
    return "ORCHESTRATION " + json.dumps({"label": label, **kwargs})


def test_shift_tab_cycles_lobby_and_preserves_draft_and_resumed_mode(
    mode_cluster: _ModeCluster,
) -> None:
    cluster = mode_cluster
    with contextlib.closing(_Terminal(cluster, "lobby")) as terminal:
        _wait(lambda: "Mode: normal" in terminal.footer)
        planned = _draft("shortcut-plan", action="plan")
        terminal.write(planned)
        _wait(lambda: planned in terminal.visible)
        terminal.cycle("auto")
        terminal.cycle("plan")
        plan_output = len(terminal.text)
        terminal.write(b"\r")
        plan_task = cluster.settled("shortcut-plan")
        assert plan_task["state"] == "completed", terminal.text
        assert plan_task["title"] == planned
        session_id = plan_task["session_id"]
        assert cluster.mode(session_id) == "plan"
        assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
        terminal.prompt_after_task(plan_output)
        normal = _draft("shortcut-normal", content="review this draft\n")
        terminal.write(normal)
        _wait(lambda: normal in terminal.visible)
        terminal.cycle("normal")
        normal_output = len(terminal.text)
        terminal.write(b"\r")
        normal_task = cluster.settled("shortcut-normal")
        assert normal_task["state"] == "awaiting_review", terminal.text
        assert normal_task["title"] == normal
        assert cluster.mode(session_id) == "normal"
        terminal.prompt_after_task(normal_output)
        terminal.write("/detach\r")
        assert terminal.process.wait(timeout=5) == 0, terminal.text

    with contextlib.closing(
        _Terminal(cluster, "resumed", resume=session_id)
    ) as resumed:
        _wait(lambda: "Mode: normal" in resumed.footer)
        assert cluster.mode(session_id) == "normal"
        draft = _draft("shortcut-resumed", content="resumed automatic edit\n")
        resumed.write(draft)
        _wait(lambda: draft in resumed.visible)
        resumed.cycle("auto")
        _wait(lambda: cluster.mode(session_id) == "auto")
        resumed_output = len(resumed.text)
        resumed.write(b"\r")
        task = cluster.settled("shortcut-resumed")
        assert task["state"] == "completed", resumed.text
        assert task["title"] == draft
        assert (cluster.repository / "docs/a.md").read_text() == (
            "resumed automatic edit\n"
        )
        resumed.prompt_after_task(resumed_output)
        resumed.write("/detach\r")
        assert resumed.process.wait(timeout=5) == 0, resumed.text


def test_shift_tab_refusal_preserves_draft_and_active_task_mode(
    mode_cluster: _ModeCluster,
) -> None:
    cluster = mode_cluster
    # A tall viewport exposes empty space inserted by physical-bottom anchoring.
    with contextlib.closing(
        _Terminal(cluster, "busy", mode="auto", rows=60)
    ) as terminal:
        terminal.write(_draft("shortcut-busy", content="active edit\n", pause=True))
        terminal.write(b"\r")
        cluster.ready("shortcut-busy")
        task = cluster.task("shortcut-busy")
        _wait(
            lambda: (
                "Reviewing project files" in terminal.text
                and "Thinking…" in terminal.visible
                and "Mode: auto" in terminal.footer
            )
        )
        assert "base a" not in terminal.text
        assert "docs/a.md" not in terminal.text
        assert "Reviewing project files" in terminal.text
        assert "Model:" in terminal.footer, terminal.visible
        assert "Effort:" in terminal.footer and "Mode: auto" in terminal.footer
        with terminal.screen_lock:
            lines = terminal.screen.lines()
        status_row = max(
            index for index, line in enumerate(lines) if line.startswith(" Model:")
        )
        assert lines[status_row - 1] == "─" * terminal.screen.columns, terminal.visible
        assert "Thinking…" in lines[status_row - 2], terminal.visible
        last_output = max(
            index for index, line in enumerate(lines[: status_row - 1]) if line
        )
        assert status_row - last_output <= 3, terminal.visible
        assert status_row < 45, terminal.visible
        assert not any(lines[status_row + 1 :]), terminal.visible
        detached_output = len(terminal.text)
        terminal.write(b"\x03")
        _wait(lambda: "Detached;" in terminal.text)
        terminal.prompt_after_task(detached_output)
        draft = _draft("shortcut-after-busy", content="preserved after refusal\n")
        terminal.write(draft)
        _wait(lambda: draft in terminal.visible)
        terminal.cycle("auto", error="still running")
        assert cluster.mode(task["session_id"]) == "auto"
        assert len(cluster.rpc("task.list")) == 1
        cluster.release("shortcut-busy")
        assert cluster.settled("shortcut-busy")["state"] == "completed"
        followup_output = len(terminal.text)
        terminal.write(b"\r")
        followup = cluster.settled("shortcut-after-busy")
        assert followup["state"] == "completed", terminal.text
        assert followup["title"] == draft
        assert (cluster.repository / "docs/a.md").read_text() == (
            "preserved after refusal\n"
        )
        terminal.prompt_after_task(followup_output)
        terminal.write("/detach\r")
        assert terminal.process.wait(timeout=5) == 0, terminal.text
