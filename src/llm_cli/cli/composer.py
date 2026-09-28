"""An editable, scrollback-friendly prompt with a deterministic pipe fallback."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable, Iterable
from contextlib import suppress
from typing import TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    ConditionalCompleter,
)
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from prompt_toolkit.styles import Style
from rich.text import Text

from llm_cli.cli.command_picker import (
    command_picker_fragments,
    install_command_picker,
)
from llm_cli.cli.interrupts import (
    EXIT_HINT,
    ExitRequested,
    InputInterrupted,
    InterruptState,
)
from llm_cli.cli.terminal import TerminalUI, safe_text
from llm_cli.errors import LlmCoordError

_COMMANDS = {
    "/login": "Connect Codex, Anthropic, or OpenAI",
    "/provider": "Choose your AI provider",
    "/logout": "Remove a saved AI account",
    "/accounts": "Show connected AI accounts",
    "/cd": "Choose a project folder",
    "/help": "Show commands and keyboard shortcuts",
    "/status": "Show this session and its workspace",
    "/mode": "Choose plan, normal review, or auto mode",
    "/scope": "View or change editable paths",
    "/changes": "Show new changes from other sessions",
    "/tasks": "List tasks in this conversation",
    "/attach": "Follow the last task, or a task ID",
    "/history": "Show prompts from this visit",
    "/model": "Browse available models and their effort levels",
    "/models": "Browse this account's available models",
    "/effort": "Choose thinking effort for the current model",
    "/clear": "Clear the screen; keep the conversation",
    "/detach": "Leave this conversation available to resume",
    "/exit": "Close this conversation",
    "/diff": "Inspect a task's proposed or published changes",
    "/checks": "Show a task's verification results",
    "/apply": "Publish a retained task proposal",
    "/undo": "Undo a published task if its files are unchanged",
    "/stop": "Stop a task and retain its pending edits",
}


def _command_query(document: Document) -> bool:
    return (
        document.text.startswith("/")
        and document.cursor_position == len(document.text)
        and not any(char.isspace() for char in document.text)
    )


class CommandCompleter(Completer):
    """Search command names and descriptions while editing the command itself."""

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        if not _command_query(document):
            return
        query = document.text[1:].casefold()
        matches = [
            (command, description)
            for command, description in _COMMANDS.items()
            if query in command.casefold()
            or any(word.startswith(query) for word in description.casefold().split())
        ]
        matches.sort(
            key=lambda item: (
                not item[0][1:].startswith(query),
                query not in item[0][1:],
            )
        )
        for command, description in matches:
            yield Completion(
                command,
                start_position=-len(document.text),
                display_meta=description,
            )


class Composer:
    def __init__(self, stdin: TextIO, stream: TextIO, *, plain: bool = False) -> None:
        self.stdin = stdin
        self.stream = stream
        self._ui = TerminalUI(stream, plain=plain)
        self.model = ""
        self.effort = "default"
        self.scope = ""
        self.mode = "normal"
        self.prompts: list[str] = []
        self.interrupts = InterruptState()
        self._session: PromptSession[str] | None = None
        self._answer_mode = False
        self.on_mode_cycle: Callable[[], None] | None = None
        self._changing_mode = False
        self._mode_error = ""
        self._deferred_keys: list[KeyPress] = []
        self._commands = CommandCompleter()
        self._picker_index = 0
        self._picker_dismissed: str | None = None
        # Custom streams (tests, redirects, pipes) must never open /dev/tty.
        if (
            not plain
            and stdin.isatty()
            and stream.isatty()
            and os.getenv("TERM") != "dumb"
        ):
            bindings = KeyBindings()
            picking = Condition(self._picker_visible)

            @bindings.add("s-tab")
            def cycle_mode(event: KeyPressEvent) -> None:
                if self._answer_mode or self.on_mode_cycle is None:
                    return
                # A highlighted completion is only a preview, not the draft.
                event.current_buffer.cancel_completion()
                self.interrupts.reset()
                self._mode_error = ""
                self._changing_mode = True
                event.app.create_background_task(self._cycle_mode(event))

            @bindings.add("c-c")
            def interrupt(event: KeyPressEvent) -> None:
                self._deferred_keys.clear()
                self._mode_error = ""
                result = self.interrupts.press()
                if isinstance(result, ExitRequested):
                    event.app.exit(exception=result)
                elif self._answer_mode:
                    # Finish this input batch before leaving the question, so
                    # a second queued Ctrl+C can still request an exit.
                    def leave_question() -> None:
                        if not event.app.is_done:
                            event.app.exit(exception=result)

                    assert event.app.loop is not None
                    event.app.loop.call_soon(leave_question)
                else:
                    # Keep the editor alive so two keys in the same input batch
                    # are both handled, even when Ctrl+C is pressed very fast.
                    event.current_buffer.reset()

            @bindings.add("enter")
            def submit(event: KeyPressEvent) -> None:
                buffer = event.current_buffer
                if not self._answer_mode:
                    # Handle fast typing followed by Enter before the automatic
                    # completion task has had a chance to display its results.
                    matches = self._command_matches()
                    selected = (
                        matches[self._picker_index % len(matches)] if matches else None
                    )
                    if selected is not None and selected.text != buffer.text:
                        buffer.apply_completion(selected)
                        self._picker_dismissed = buffer.text
                        return
                buffer.validate_and_handle()

            @bindings.add("tab", filter=picking)
            def choose(event: KeyPressEvent) -> None:
                matches = self._command_matches()
                if matches:
                    event.current_buffer.apply_completion(
                        matches[self._picker_index % len(matches)]
                    )
                    self._picker_dismissed = event.current_buffer.text

            @bindings.add("down", filter=picking)
            @bindings.add("c-n", filter=picking)
            def next_command(event: KeyPressEvent) -> None:
                self._move_picker(1)

            @bindings.add("up", filter=picking)
            @bindings.add("c-p", filter=picking)
            def previous_command(event: KeyPressEvent) -> None:
                self._move_picker(-1)

            @bindings.add("pagedown", filter=picking)
            def next_page(event: KeyPressEvent) -> None:
                self._move_picker(6)

            @bindings.add("pageup", filter=picking)
            def previous_page(event: KeyPressEvent) -> None:
                self._move_picker(-6)

            @bindings.add("escape", filter=picking)
            def dismiss(event: KeyPressEvent) -> None:
                event.current_buffer.cancel_completion()
                self._picker_dismissed = event.current_buffer.text

            @bindings.add("escape", "enter")
            def newline(event: KeyPressEvent) -> None:
                event.current_buffer.insert_text("\n")

            def defer(event: KeyPressEvent) -> None:
                key = event.key_sequence[-1]
                if (
                    key.key in {Keys.BackTab, Keys.BracketedPaste, Keys.ControlJ}
                    or (len(key.key) == 1 and key.key.isprintable())
                    or (
                        key.key == Keys.ControlM
                        and self._deferred_keys
                        and self._deferred_keys[-1].key == Keys.Escape
                    )
                ):
                    self.interrupts.reset()
                self._deferred_keys.extend(event.key_sequence)

            # Gate input synchronously, including keys in the Shift+Tab batch.
            # Replay it after acknowledgement so Enter cannot use the old mode.
            # Ctrl+C stays responsive; CPR responses must still reach the renderer.
            changing = Condition(lambda: self._changing_mode)
            for key in Keys:
                if key not in {Keys.ControlC, Keys.CPRResponse}:
                    bindings.add(key, filter=changing, eager=key != Keys.Any)(defer)

            # Prompt-toolkit's default toolbar uses reverse video. Explicitly
            # clear that style so only the separator carries the accent color.
            style = {
                "bottom-toolbar": "noreverse bg:default",
                "bottom-toolbar.text": "noreverse bg:default",
                "footer-divider": "noreverse bg:default",
                "command-picker": "noreverse bg:default",
                "command-picker.heading": "nobold",
                "command-picker.current": "bold",
                "command-picker.marker": "bold",
            }
            if "NO_COLOR" not in os.environ:
                style.update(
                    {
                        "prompt": "#bda4ff bold",
                        "bottom-toolbar": "#9da8bb noreverse bg:default",
                        "footer-divider": "#a78bfa noreverse bg:default",
                        "command-picker": "#bac0cd noreverse bg:default",
                        "command-picker.heading": "#8d93a6 nobold",
                        "command-picker.description": "#8d93a6",
                        "command-picker.current": "#bda4ff bold",
                        "command-picker.marker": "#a78bfa bold",
                    }
                )
            searching = Condition(self._searching_commands)
            self._session = PromptSession(
                history=InMemoryHistory(),
                completer=ConditionalCompleter(self._commands, searching),
                complete_while_typing=searching,
                multiline=True,
                key_bindings=bindings,
                style=Style.from_dict(style),
                bottom_toolbar=self._toolbar_fragments,
                prompt_continuation="  · ",
                reserve_space_for_menu=0,
                enable_open_in_editor=False,
            )
            install_command_picker(self._session, self._picker_fragments, picking)
            self._session.default_buffer.on_text_changed += self._draft_changed

    async def _request_mode_cycle(self) -> None:
        """Await the blocking RPC without making editor shutdown wait for it."""
        assert self.on_mode_cycle is not None
        callback = self.on_mode_cycle
        loop = asyncio.get_running_loop()
        completed: asyncio.Future[None] = loop.create_future()

        def resolve(error: BaseException | None) -> None:
            if completed.done():
                return
            if error is None:
                completed.set_result(None)
            else:
                completed.set_exception(error)

        def request() -> None:
            error: BaseException | None = None
            try:
                callback()
            except BaseException as exc:
                error = exc
            # Double Ctrl+C may already have closed the editor's loop.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(resolve, error)

        # DaemonClient owns a synchronous event loop. A daemon worker keeps the
        # editor responsive and never delays exit for an unresponsive daemon.
        # An already dispatched request may still settle on the server.
        threading.Thread(target=request, name="loupe-mode", daemon=True).start()
        await completed

    async def _cycle_mode(self, event: KeyPressEvent) -> None:
        try:
            await self._request_mode_cycle()
        except (LlmCoordError, ValueError, OSError) as exc:
            self._mode_error = (
                exc.message if isinstance(exc, LlmCoordError) else str(exc)
            )
        finally:
            self._changing_mode = False
        queued, self._deferred_keys = self._deferred_keys, []
        if event.app.is_done:
            return
        if self._mode_error:
            # Keep typed text, but require a fresh Enter after a refused change.
            # Alt+Enter remains a newline rather than leaving an unmatched Escape.
            queued = [
                key
                for index, key in enumerate(queued)
                if key.key != Keys.ControlM
                or (index > 0 and queued[index - 1].key == Keys.Escape)
            ]
        event.app.key_processor.feed_multiple(queued, first=True)
        event.app.key_processor.process_keys()
        event.app.invalidate()

    def _draft_changed(self, buffer: object) -> None:
        self._picker_index = 0
        self._picker_dismissed = None
        if self._session is not None and self._session.default_buffer.text:
            self.interrupts.reset()

    def _command_matches(self) -> list[Completion]:
        if self._session is None or self._answer_mode:
            return []
        return list(
            self._commands.get_completions(
                self._session.default_buffer.document,
                CompleteEvent(completion_requested=True),
            )
        )

    def _picker_visible(self) -> bool:
        if self._session is None or self._answer_mode:
            return False
        document = self._session.default_buffer.document
        return (
            _command_query(document)
            and document.text != self._picker_dismissed
        )

    def _move_picker(self, step: int) -> None:
        matches = self._command_matches()
        if matches:
            self._picker_index = (self._picker_index + step) % len(matches)

    def _picker_fragments(self) -> StyleAndTextTuples:
        width = self._session.output.get_size().columns if self._session else 80
        return command_picker_fragments(
            self._command_matches(), self._picker_index, max(1, width - 1)
        )

    def _searching_commands(self) -> bool:
        return (
            not self._answer_mode
            and self._session is not None
            and _command_query(self._session.default_buffer.document)
        )

    def status_text(self) -> str:
        """Persistent metadata shared by the editor and task-output footer."""
        model = (
            safe_text(self.model or "Not connected")
            .replace("\n", " ")
            .replace("\t", " ")
        )
        effort = (
            safe_text(self.effort or "default").replace("\n", " ").replace("\t", " ")
        )
        prefix = " Model: "
        suffix = f" · Effort: {effort} · Mode: {self.mode}"
        if self._session is not None:
            width = self._session.output.get_size().columns
            if width < 55:
                prefix = " "
                suffix = f" · {effort} effort · {self.mode} mode"
            label = Text(model)
            label.truncate(
                max(1, width - Text(suffix).cell_len - len(prefix) - 1),
                overflow="ellipsis",
            )
            model = label.plain
        return f"{prefix}{model}{suffix}"

    def _toolbar_fragments(self) -> StyleAndTextTuples:
        width = self._session.output.get_size().columns if self._session else 80
        return [
            ("class:footer-divider", "─" * width + "\n"),
            ("", self._toolbar()),
        ]

    def _toolbar(self) -> str:
        status = self.status_text() + "\n "
        if self._changing_mode:
            return status + "Changing mode… · Ctrl+C clear · Ctrl+C twice exit"
        if self._mode_error:
            return status + safe_text(self._mode_error).replace("\n", " ")
        if self.interrupts.armed:
            return status + "Draft cleared. " + EXIT_HINT
        if self._answer_mode:
            return (
                status
                + "Enter answer · Alt+Enter newline · Ctrl+C detach · /stop cancel"
            )
        if self._picker_visible():
            assert self._session is not None
            matches = self._command_matches()
            selected = (
                matches[self._picker_index % len(matches)] if matches else None
            )
            run = (
                selected is not None
                and selected.text == self._session.default_buffer.text
            )
            action = "Tab choose · Enter run" if run else "Tab/Enter choose"
            hint = f"↑↓ browse commands · {action} · Esc close"
            width = self._session.output.get_size().columns
            if Text(" " + hint + " · Shift+Tab mode").cell_len <= width:
                hint += " · Shift+Tab mode"
            elif Text(" " + hint).cell_len > width:
                hint = "↑↓ browse · Enter run" if run else "↑↓ browse · Enter choose"
            label = Text(hint)
            label.truncate(max(0, width - 1), overflow="ellipsis")
            return status + label.plain
        return status + "Shift+Tab mode · Enter send · Alt+Enter newline · / commands"

    def read(self, *, answer: bool = False) -> str:
        self._answer_mode = answer
        self._mode_error = ""
        self._deferred_keys.clear()
        self._ui.rule()
        try:
            if self._session is not None:
                value = self._session.prompt(
                    [("class:prompt", "  ? " if answer else "  ❯ ")],  # noqa: RUF001
                    in_thread=True,
                )
            else:
                self.stream.write("  ? " if answer else "  > ")
                self.stream.flush()
                value = self.stdin.readline()
                if not value:
                    raise EOFError
                value = value.rstrip("\r\n")
        except KeyboardInterrupt as exc:
            if isinstance(exc, InputInterrupted):
                raise
            raise self.interrupts.press() from None
        finally:
            if self._session is None:
                # Pipes do not echo input, and EOF/cancellation may leave the
                # fallback prompt open on its current line.
                self._ui.notice("")
            self._ui.rule()
        self.interrupts.reset()
        if value.strip() and not value.startswith("/") and not answer:
            self.prompts.append(value)
        return value


__all__ = ["Composer"]
