"""Slash search and real prompt-toolkit key handling without a terminal or daemon."""

from __future__ import annotations

import io
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.input import PipeInput, create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.base import Size

from llm_cli.cli import composer
from llm_cli.cli.interrupts import ExitRequested, InputInterrupted


def completions(text: str, cursor: int | None = None) -> list[Completion]:
    return list(
        composer.CommandCompleter().get_completions(
            Document(text, cursor_position=cursor), CompleteEvent()
        )
    )


def test_slash_shows_every_command_with_description() -> None:
    results = completions("/")
    assert [item.text for item in results] == list(composer._COMMANDS)
    assert all(item.start_position == -1 for item in results)
    assert all(item.display_meta_text for item in results)


def test_search_prioritizes_command_names_over_description_matches() -> None:
    # /logout appears earlier in the registry but only its description matches.
    assert [item.text for item in completions("/account")] == [
        "/accounts",
        "/logout",
        "/models",
    ]
    assert [item.text for item in completions("/CODex")] == ["/login"]
    assert [item.text for item in completions("/task")] == [
        "/tasks",
        "/attach",
        "/diff",
        "/checks",
        "/apply",
        "/undo",
        "/stop",
    ]
    assert completions("/CODex")[0].start_position == -6
    # A model query should not include logout just because "remove" contains mo.
    assert [item.text for item in completions("/mo")] == [
        "/mode",
        "/model",
        "/models",
        "/effort",
    ]


def test_exact_command_stays_in_search_results() -> None:
    assert completions("/model")[0].text == "/model"


@pytest.mark.parametrize(
    ("text", "cursor"),
    [
        ("", None),
        ("/does-not-exist", None),
        ("/cd /Users/example/project", None),
        ("/scope src/", None),
        ("/login codex", None),
        ("Explain /login", None),
        ("/lo\nnew line", None),
        ("Read this\n/lo", None),
        ("/login", 3),
    ],
)
def test_completion_does_not_rewrite_arguments_or_prompt_text(
    text: str, cursor: int | None
) -> None:
    assert completions(text, cursor) == []


def wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("prompt-toolkit did not reach the expected input state")
        time.sleep(0.005)


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@dataclass
class Editor:
    composer: composer.Composer
    session: PromptSession[str]
    pipe: PipeInput
    result: Future[str]

    def send(self, text: str) -> None:
        self.pipe.send_text(text)

    def expect_draft(self, text: str, *, menu: bool) -> None:
        wait_until(
            lambda: (
                self.session.default_buffer.text == text
                and self.composer._picker_visible() is menu
            )
        )
        assert not self.result.done(), "selection prematurely submitted the prompt"

    def submitted(self, text: str) -> None:
        assert self.result.result(timeout=3) == text


@contextmanager
def editing(
    monkeypatch: pytest.MonkeyPatch,
    *,
    answer: bool = False,
    on_mode_change: Callable[[composer.Composer], None] | None = None,
) -> Iterator[Editor]:
    monkeypatch.setenv("TERM", "xterm-256color")
    with create_pipe_input() as pipe:

        def session_factory(*args: Any, **kwargs: Any) -> PromptSession[str]:
            return PromptSession(*args, input=pipe, output=DummyOutput(), **kwargs)

        monkeypatch.setattr(composer, "PromptSession", session_factory)
        instance = composer.Composer(Tty(), Tty())
        if on_mode_change is not None:
            instance.on_mode_cycle = lambda: on_mode_change(instance)
        session = instance._session
        assert session is not None
        # Keep the real Escape/Alt key disambiguation with a bounded test delay.
        session.app.timeoutlen = 0.05
        session.app.ttimeoutlen = 0.05

        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(instance.read, answer=answer)
            try:
                wait_until(lambda: session.app.is_running)
                yield Editor(instance, session, pipe, result)
            finally:
                if not result.done():
                    pipe.send_text("\x03\x03")
                with suppress(
                    EOFError,
                    KeyboardInterrupt,
                    ExitRequested,
                ):
                    result.result(timeout=3)


def test_slash_opens_menu_and_arrows_enter_choose_before_submitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch) as editor:
        editor.send("/")
        editor.expect_draft("/", menu=True)
        assert len(editor.composer._command_matches()) == len(composer._COMMANDS)
        editor.send("\x1b[B")
        wait_until(lambda: editor.composer._picker_index == 1)
        editor.expect_draft("/", menu=True)
        editor.send("\x1b[B")
        wait_until(lambda: editor.composer._picker_index == 2)
        editor.expect_draft("/", menu=True)
        editor.send("\x1b[A")
        wait_until(lambda: editor.composer._picker_index == 1)
        editor.expect_draft("/", menu=True)
        editor.send("\r")
        editor.expect_draft("/provider", menu=False)
        editor.send("\r")
        editor.submitted("/provider")


def test_tab_accepts_a_description_search_and_leaves_room_for_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch) as editor:
        editor.send("/codex")
        editor.expect_draft("/codex", menu=True)
        editor.send("\t")
        editor.expect_draft("/login", menu=False)
        editor.send(" codex")
        editor.expect_draft("/login codex", menu=False)
        editor.send("\r")
        editor.submitted("/login codex")


def test_escape_restores_search_text_and_dismisses_the_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch) as editor:
        editor.send("/")
        editor.expect_draft("/", menu=True)
        editor.send("\x1b[B")
        wait_until(lambda: editor.composer._picker_index == 1)
        editor.expect_draft("/", menu=True)
        editor.send("\x1b")
        editor.expect_draft("/", menu=False)
        editor.send("status\r")
        editor.submitted("/status")


def test_filtering_after_browsing_uses_typed_query_and_resets_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch) as editor:
        editor.send("/mo\x1b[Bd")
        editor.expect_draft("/mod", menu=True)
        assert editor.composer._picker_index == 0
        editor.send("\x7f\x7f")
        editor.expect_draft("/m", menu=True)
        editor.send("\x7f")
        editor.expect_draft("/", menu=True)
        assert len(editor.composer._command_matches()) == len(composer._COMMANDS)
        editor.send("status\r")
        editor.submitted("/status")


def test_no_results_stays_open_and_escape_preserves_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch) as editor:
        editor.send("/zzz")
        editor.expect_draft("/zzz", menu=True)
        assert editor.composer._command_matches() == []
        editor.send("\x1b")
        editor.expect_draft("/zzz", menu=False)
        editor.send("\x7f")
        editor.expect_draft("/zz", menu=True)


def test_page_navigation_selects_offscreen_commands_without_changing_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch) as editor:
        editor.send("/\x1b[6~\x1b[6~")
        wait_until(lambda: editor.composer._picker_index == 12)
        editor.expect_draft("/", menu=True)
        editor.send("\t")
        editor.expect_draft("/history", menu=False)
        editor.send("\r")
        editor.submitted("/history")


def test_alt_enter_preserves_multiline_editing_with_a_menu_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch) as editor:
        editor.send("/")
        editor.expect_draft("/", menu=True)
        editor.send("\x1b\r")
        editor.expect_draft("/\n", menu=False)
        editor.send("literal /cd /tmp\r")
        editor.submitted("/\nliteral /cd /tmp")


def test_fast_search_and_enter_selects_without_submitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch) as editor:
        editor.send("/codex\r")
        editor.expect_draft("/login", menu=False)
        editor.send("\r")
        editor.submitted("/login")


def test_fully_typed_command_keeps_picker_open_and_runs_with_one_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch) as editor:
        editor.send("/status")
        editor.expect_draft("/status", menu=True)
        assert editor.composer._command_matches()[0].text == "/status"
        assert "Enter run" in editor.composer._toolbar()
        editor.send("\r")
        editor.submitted("/status")


def test_exact_command_can_still_choose_another_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch) as editor:
        editor.send("/model")
        editor.expect_draft("/model", menu=True)
        editor.send("\x1b[B")
        wait_until(lambda: editor.composer._picker_index == 1)
        editor.send("\r")
        editor.expect_draft("/models", menu=False)
        editor.send("\r")
        editor.submitted("/models")


def test_question_answers_bypass_slash_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch, answer=True) as editor:
        editor.send("/codex")
        editor.expect_draft("/codex", menu=False)
        editor.send("\r")
        editor.submitted("/codex")
        assert editor.composer.prompts == []


@pytest.mark.parametrize("text", ["/status", "/cd /tmp", "Describe /login"])
def test_complete_commands_arguments_and_prose_submit_normally(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    with editing(monkeypatch) as editor:
        editor.send(text + "\r")
        editor.submitted(text)


def test_first_ctrl_c_clears_draft_and_second_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch) as editor:
        editor.send("unfinished draft")
        editor.expect_draft("unfinished draft", menu=False)
        editor.send("\x03")
        editor.expect_draft("", menu=False)
        assert "Ctrl+C again" in editor.composer._toolbar()
        editor.send("\x03")
        with pytest.raises(ExitRequested):
            editor.result.result(timeout=3)


@pytest.mark.parametrize("answer", [False, True], ids=["prompt", "question"])
def test_ctrl_c_twice_in_one_keyboard_batch_exits(
    monkeypatch: pytest.MonkeyPatch,
    answer: bool,
) -> None:
    with editing(monkeypatch, answer=answer) as editor:
        editor.send("\x03\x03")
        with pytest.raises(ExitRequested):
            editor.result.result(timeout=3)


def test_typing_a_new_draft_disarms_double_ctrl_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch) as editor:
        editor.send("\x03new draft")
        editor.expect_draft("new draft", menu=False)
        editor.send("\x03")
        editor.expect_draft("", menu=False)
        editor.send("keep working\r")
        editor.submitted("keep working")


def test_ctrl_c_leaves_answer_pending_and_arms_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch, answer=True) as editor:
        editor.send("\x03")
        with pytest.raises(InputInterrupted):
            editor.result.result(timeout=3)
        assert editor.composer.interrupts.armed


def test_shift_tab_preserves_multiline_draft_cursor_and_queued_typing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed: list[str] = []

    def change(instance: composer.Composer) -> None:
        assert instance.prompts == []
        assert instance._session is not None
        assert instance._session.history.get_strings() == []
        instance.mode = "auto"
        changed.append(instance.mode)

    with editing(monkeypatch, on_mode_change=change) as editor:
        editor.send("first\x1b\rsecond\x1b[D\x1b[Z!\r")
        editor.submitted("first\nsecon!d")
        assert changed == ["auto"]
        assert editor.composer.prompts == ["first\nsecon!d"]
        assert editor.session.history.get_strings() == ["first\nsecon!d"]


def test_repeated_shift_tab_keeps_all_keys_in_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.agent.modes import AGENT_MODES

    changed: list[str] = []

    def change(instance: composer.Composer) -> None:
        instance.mode = AGENT_MODES[(AGENT_MODES.index(instance.mode) + 1) % 3]
        changed.append(instance.mode)

    with editing(monkeypatch, on_mode_change=change) as editor:
        editor.send("draft\x1b[Z\x1b[Z\x1b[Z finished\r")
        editor.submitted("draft finished")
        assert changed == ["auto", "plan", "normal"]
        assert editor.composer.prompts == ["draft finished"]
        assert editor.session.history.get_strings() == ["draft finished"]


def test_shift_tab_preserves_query_with_unaccepted_command_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed: list[str] = []
    with editing(
        monkeypatch, on_mode_change=lambda instance: changed.append(instance.mode)
    ) as editor:
        editor.send("/")
        editor.expect_draft("/", menu=True)
        editor.send("\x1b[B")
        wait_until(lambda: editor.composer._picker_index == 1)
        editor.expect_draft("/", menu=True)
        editor.send("\x1b[Zstatus\r")
        editor.submitted("/status")
        assert changed == ["normal"]
        assert editor.composer.prompts == []


def test_shift_tab_in_question_does_not_change_mode_or_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch, answer=True) as editor:
        editor.send("my answe\x1b[Zr")
        editor.expect_draft("my answer", menu=False)
        editor.send("\r")
        editor.submitted("my answer")
        assert editor.composer.mode == "normal"
        assert editor.composer.prompts == []


def test_ctrl_c_clears_draft_and_cycle_disarms_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed: list[str] = []
    with editing(
        monkeypatch, on_mode_change=lambda instance: changed.append(instance.mode)
    ) as editor:
        # First clear the draft, then cycle while exit is armed. The
        # next Ctrl+C must clear the empty buffer, not exit or revive the draft.
        editor.send("discard me\x1b[Z")
        wait_until(lambda: len(changed) == 1 and not editor.composer._changing_mode)
        editor.send("\x03\x1b[Z")
        wait_until(lambda: len(changed) == 2 and not editor.composer._changing_mode)
        editor.send("\x03")
        wait_until(lambda: editor.composer.interrupts.armed)
        editor.expect_draft("", menu=False)
        editor.send("replacement\r")
        editor.submitted("replacement")
        assert editor.composer.prompts == ["replacement"]


def test_mode_change_updates_status_without_writing_to_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def change(instance: composer.Composer) -> None:
        instance.mode = "auto"

    with editing(monkeypatch, on_mode_change=change) as editor:
        editor.composer.model = "example-model"
        editor.composer.effort = "high"
        editor.send("keep this draft")
        editor.expect_draft("keep this draft", menu=False)
        transcript = editor.composer.stream.getvalue()  # type: ignore[attr-defined]
        editor.send("\x1b[Z")
        wait_until(
            lambda: (
                not editor.composer._changing_mode and editor.composer.mode == "auto"
            )
        )
        editor.expect_draft("keep this draft", menu=False)
        assert editor.composer.stream.getvalue() == transcript  # type: ignore[attr-defined]
        assert (
            "Model: example-model · Effort: high · Mode: auto"
            in editor.composer._toolbar()
        )
        editor.send("\r")
        editor.submitted("keep this draft")


@pytest.mark.parametrize("refused", [False, True])
def test_enter_waits_for_mode_acknowledgement_and_refusal_keeps_draft(
    monkeypatch: pytest.MonkeyPatch, refused: bool
) -> None:
    entered, release = threading.Event(), threading.Event()

    def change(instance: composer.Composer) -> None:
        entered.set()
        assert release.wait(timeout=3)
        if refused:
            raise OSError("Mode change refused")
        instance.mode = "auto"

    with editing(monkeypatch, on_mode_change=change) as editor:
        try:
            editor.send("draft\x1b[D\x1b[Z!\r")
            assert entered.wait(timeout=3)
            assert not editor.result.done()
            assert editor.composer.mode == "normal"
            assert "Changing mode" in editor.composer._toolbar()
        finally:
            release.set()
        if refused:
            editor.expect_draft("draf!t", menu=False)
            assert "Mode change refused" in editor.composer._toolbar()
            assert editor.composer.mode == "normal"
            editor.send("\r")
        editor.submitted("draf!t")
        assert editor.composer.mode == ("normal" if refused else "auto")


def test_ctrl_c_remains_responsive_during_mode_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()

    def change(instance: composer.Composer) -> None:
        entered.set()
        assert release.wait(timeout=3)
        instance.mode = "auto"

    with editing(monkeypatch, on_mode_change=change) as editor:
        try:
            editor.send("discard\x1b[Zqueued\r")
            assert entered.wait(timeout=3)
            editor.send("\x03")
            editor.expect_draft("", menu=False)
            assert editor.composer._changing_mode
        finally:
            release.set()
        wait_until(lambda: not editor.composer._changing_mode)
        editor.expect_draft("", menu=False)
        editor.send("replacement\r")
        editor.submitted("replacement")


def test_double_ctrl_c_does_not_wait_for_pending_mode_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def change(instance: composer.Composer) -> None:
        entered.set()
        try:
            assert release.wait(timeout=3)
            instance.mode = "auto"
        finally:
            finished.set()

    with editing(monkeypatch, on_mode_change=change) as editor:
        try:
            editor.send("draft\x1b[Z")
            assert entered.wait(timeout=3)
            editor.send("\x03\x03")
            with pytest.raises(ExitRequested):
                editor.result.result(timeout=1)
            assert not release.is_set()
        finally:
            release.set()
            assert finished.wait(timeout=3)


@pytest.mark.parametrize("text", ["replacement", "\x1b[200~pasted draft\x1b[201~"])
def test_typing_while_mode_request_pending_disarms_exit(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    entered, release = threading.Event(), threading.Event()

    def change(instance: composer.Composer) -> None:
        entered.set()
        assert release.wait(timeout=3)
        instance.mode = "auto"

    with editing(monkeypatch, on_mode_change=change) as editor:
        try:
            editor.send("draft\x1b[Z")
            assert entered.wait(timeout=3)
            editor.send("\x03" + text + "\x03")
            editor.expect_draft("", menu=False)
            wait_until(lambda: editor.composer.interrupts.armed)
            assert not editor.result.done()
        finally:
            release.set()
        wait_until(lambda: not editor.composer._changing_mode)
        editor.send("keep working\r")
        editor.submitted("keep working")


@pytest.mark.parametrize("answer", [False, True])
def test_footer_metadata_remains_visible_in_editor_states(
    monkeypatch: pytest.MonkeyPatch, answer: bool
) -> None:
    with editing(monkeypatch, answer=answer) as editor:
        editor.composer.model = "example-model"
        editor.composer.effort = "high"
        expected = "Model: example-model · Effort: high · Mode: normal"
        assert expected in editor.composer._toolbar()
        editor.send("/")
        editor.expect_draft("/", menu=not answer)
        assert expected in editor.composer._toolbar()
        if not answer:
            editor.send("\x03")
            editor.expect_draft("", menu=False)
            assert expected in editor.composer._toolbar()


def test_long_model_name_leaves_room_for_effort_and_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch) as editor:
        editor.composer.model = "long-model-" * 30
        editor.composer.effort = "high"
        assert "Effort: high · Mode: normal" in editor.composer.status_text()
        assert (
            len(editor.composer.status_text())
            <= editor.session.output.get_size().columns
        )


def test_narrow_footer_keeps_effort_and_mode_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with editing(monkeypatch) as editor:
        monkeypatch.setattr(
            editor.session.output, "get_size", lambda: Size(rows=24, columns=40)
        )
        editor.composer.model = "long-model-" * 30
        editor.composer.effort = "high"
        status = editor.composer.status_text()
        assert "high effort · normal mode" in status
        assert len(status) <= 40
