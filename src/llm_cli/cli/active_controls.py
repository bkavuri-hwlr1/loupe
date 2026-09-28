"""Small live input surface while a task stream owns terminal output."""

from __future__ import annotations

import contextlib
import os
import selectors
import signal
import threading
from collections.abc import Callable, Iterator
from typing import TextIO

from prompt_toolkit.input import create_input
from prompt_toolkit.keys import Keys

from llm_cli.cli.interrupts import ExitRequested, InputInterrupted, InterruptState
from llm_cli.protocol.client import DaemonClient


@contextlib.contextmanager
def active_controls(
    client: DaemonClient,
    task_id: str,
    stdin: TextIO,
    output: TextIO,
    *,
    interrupts: InterruptState | None = None,
    notice: Callable[[str], None] | None = None,
) -> Iterator[None]:
    if not stdin.isatty():
        yield
        return
    done = threading.Event()
    interruption: InputInterrupted | ExitRequested | None = None
    with contextlib.closing(create_input(stdin=stdin)) as keyboard, keyboard.raw_mode():
        selector = selectors.DefaultSelector()
        selector.register(keyboard.fileno(), selectors.EVENT_READ)

        def listen() -> None:
            nonlocal interruption
            line = ""
            while not done.is_set():
                if not selector.select(0.1):
                    continue
                for key in keyboard.read_keys():
                    if key.key in {Keys.ControlC, Keys.ControlD}:
                        if key.key == Keys.ControlC and interrupts is not None:
                            interruption = interrupts.press()
                        elif interruption is None:
                            interruption = InputInterrupted()
                        continue
                    if key.key == Keys.ControlM:
                        if line.strip() == "/stop":
                            try:
                                client.call("task.cancel", {"task_id": task_id})
                                message = "Stop requested; retaining pending edits."
                            except Exception:
                                message = (
                                    "Could not confirm stop; use task cancel "
                                    "with this task ID."
                                )
                            if notice is not None:
                                notice("\n" + message)
                            else:
                                output.write("\n" + message + "\n")
                                output.flush()
                        line = ""
                    elif key.key in {Keys.ControlH, Keys.Backspace}:
                        line = line[:-1]
                    elif len(key.data) == 1 and key.data.isprintable():
                        line = (line + key.data)[-100:]
                if interruption is not None:
                    done.set()
                    # A real signal wakes an idle RPC selector immediately.
                    os.kill(os.getpid(), signal.SIGINT)
                    return

        thread = threading.Thread(
            target=listen, name="loupe-active-controls", daemon=True
        )
        try:
            thread.start()
            yield
        except KeyboardInterrupt:
            if interruption is not None:
                raise interruption from None
            raise
        finally:
            done.set()
            thread.join(timeout=1)
            selector.close()
