"""Model selection works interactively and in plain terminals without side effects."""

from __future__ import annotations

import io
from asyncio import AbstractEventLoop
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from llm_cli.cli import models
from llm_cli.cli.terminal import TerminalUI
from llm_cli.paths import AppPaths
from llm_cli.providers import accounts, catalog
from llm_cli.providers.catalog import ModelCatalog, ModelOption


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    return AppPaths.resolve("test", environ={}, home=tmp_path)


@pytest.fixture
def options(monkeypatch: pytest.MonkeyPatch) -> tuple[ModelOption, ...]:
    available = (
        ModelOption(
            "reasoning-one",
            "Reasoning One",
            "Difficult coding tasks",
            ("low", "medium", "high"),
            "medium",
        ),
        ModelOption("quick-two", "Quick Two", "Fast routine changes"),
    )
    monkeypatch.setattr(
        catalog,
        "list_models",
        lambda *_args, **_kwargs: ModelCatalog(
            "openai", available, "account", "Available to this connected account."
        ),
    )
    monkeypatch.setattr(catalog, "default_model", lambda _: "reasoning-one")
    return available


def _menu(paths: AppPaths, text: str) -> tuple[models.ModelMenu, io.StringIO]:
    stream = io.StringIO()
    return (
        models.ModelMenu(
            paths, TerminalUI(stream, plain=True), io.StringIO(text), stream, plain=True
        ),
        stream,
    )


@pytest.mark.parametrize("choice", ["2\n", "quick-two\n", "Quick Two\n"])
def test_choose_by_number_id_or_display_name(
    paths: AppPaths, options: tuple[ModelOption, ...], choice: str
) -> None:
    picker, stream = _menu(paths, choice)
    assert picker.choose_model("openai", "reasoning-one") == options[1]
    text = stream.getvalue()
    assert "Reasoning One" in text and "Quick Two" in text
    assert "Effort: low, medium, high" in text
    assert "Effort controls not listed" in text
    assert "Current: reasoning-one" in text
    assert "Model list: account" in text
    assert "\x1b" not in text
    assert accounts.load_preference(paths) == {}


def test_plain_search_matches_description_and_keeps_stable_numbers(
    paths: AppPaths, options: tuple[ModelOption, ...]
) -> None:
    picker, stream = _menu(paths, "routine\n2\n")
    assert picker.choose_model("openai") == options[1]
    assert stream.getvalue().count("Fast routine changes") == 2
    assert stream.getvalue().count("Difficult coding tasks") == 1


def test_unknown_search_can_recover_and_show_all(
    paths: AppPaths, options: tuple[ModelOption, ...]
) -> None:
    picker, stream = _menu(paths, "missing-model\n*\n1\n")
    assert picker.choose_model("openai") == options[0]
    assert "No matches" in stream.getvalue()
    assert stream.getvalue().count("Reasoning One") == 2


@pytest.mark.parametrize("choice", ["", "\n", "0\n", "cancel\n"])
def test_model_cancellation_preserves_saved_selection(
    paths: AppPaths, options: tuple[ModelOption, ...], choice: str
) -> None:
    accounts.save_preference(paths, "openai", "reasoning-one", "high")
    before = accounts.load_preference(paths)
    picker, _ = _menu(paths, choice)
    assert picker.choose_model("openai") is None
    assert accounts.load_preference(paths) == before


def test_refresh_is_explicit_and_does_not_create_a_session(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[AppPaths, str, bool]] = []

    def listing(
        paths: AppPaths, provider: str, *, refresh: bool = False
    ) -> ModelCatalog:
        calls.append((paths, provider, refresh))
        return ModelCatalog(
            provider, (), "unavailable", "Account could not be reached."
        )

    monkeypatch.setattr(catalog, "list_models", listing)
    picker, stream = _menu(paths, "")
    assert picker.choose_model("anthropic", refresh=True) is None
    assert calls == [(paths, "anthropic", True)]
    assert "Account could not be reached" in stream.getvalue()
    assert "No models are available" in stream.getvalue()
    assert not paths.state_dir.exists()


def test_effort_menu_only_offers_levels_supported_by_selected_model(
    paths: AppPaths, options: tuple[ModelOption, ...]
) -> None:
    picker, stream = _menu(paths, "ultra\nhigh\n")
    assert picker.choose_effort(options[0], "low") == "high"
    text = stream.getvalue()
    assert "Current: low" in text
    assert "Currently medium" in text
    assert "No matches" in text
    assert "Ultra" not in text


def test_default_effort_is_distinct_from_cancel(
    paths: AppPaths, options: tuple[ModelOption, ...]
) -> None:
    picker, _ = _menu(paths, "default\n")
    assert picker.choose_effort(options[0], "high") == "default"
    picker, _ = _menu(paths, "\n")
    assert picker.choose_effort(options[0], "high") is None


def test_model_without_effort_does_not_consume_the_next_prompt(
    paths: AppPaths, options: tuple[ModelOption, ...]
) -> None:
    picker, stream = _menu(paths, "/status\n")
    assert picker.choose_effort(options[1]) == "default"
    assert picker.stdin.readline() == "/status\n"
    assert "has no listed effort settings" in stream.getvalue()


def test_unknown_effort_capabilities_describe_the_preserved_explicit_setting(
    paths: AppPaths, options: tuple[ModelOption, ...]
) -> None:
    picker, stream = _menu(paths, "")
    picker.choose_effort(options[1], "high")
    assert "current effort setting (high) is unchanged" in stream.getvalue()
    assert "will use the provider's default" not in stream.getvalue()


@pytest.mark.parametrize(
    ("keys", "current", "selected"),
    [
        ("\x1b[B\r", None, 1),
        ("\x1b[A\r", "quick-two", 0),
        ("routine\r", None, 1),
        ("2\r", None, 1),
        ("\r", "quick-two", 1),
        ("\x1b", None, None),
        ("\x03", None, None),
        ("missing\r\x15quick\r", None, 1),
    ],
)
def test_interactive_arrows_search_enter_and_cancel(
    paths: AppPaths,
    monkeypatch: pytest.MonkeyPatch,
    keys: str,
    current: str | None,
    selected: int | None,
) -> None:
    picker, _ = _menu(paths, "")
    choices = (
        models._Choice("reasoning-one", "Reasoning One", "Difficult coding tasks"),
        models._Choice("quick-two", "Quick Two", "Fast routine changes"),
    )
    with create_pipe_input() as pipe:
        monkeypatch.setattr(models, "create_input", lambda **_: pipe)
        monkeypatch.setattr(models, "create_output", lambda **_: DummyOutput())
        pipe.send_text(keys)
        assert picker._interactive(choices, current=current, label="Model") == selected


@pytest.mark.parametrize("keys", ["\r", "\x1b"])
def test_model_picker_opens_at_newest_models_without_changing_saved_choice(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch, keys: str
) -> None:
    class TtyStream(io.StringIO):
        def isatty(self) -> bool:
            return True

    class RecordingOutput(DummyOutput):
        def __init__(self) -> None:
            self.writes: list[str] = []

        def write(self, data: str) -> None:
            self.writes.append(data)

    available = tuple(
        ModelOption(f"model-{version}", f"Model {version} release")
        for version in range(12, 0, -1)
    )
    monkeypatch.setattr(
        catalog,
        "list_models",
        lambda *_args, **_kwargs: ModelCatalog("openai", available, "account"),
    )
    monkeypatch.setenv("TERM", "xterm")
    accounts.save_preference(paths, "openai", "model-1", "high")
    before = accounts.load_preference(paths)
    stream = TtyStream()
    picker = models.ModelMenu(paths, TerminalUI(stream), TtyStream(), stream)
    terminal = RecordingOutput()
    with create_pipe_input() as pipe:
        monkeypatch.setattr(models, "create_input", lambda **_: pipe)
        monkeypatch.setattr(models, "create_output", lambda **_: terminal)
        pipe.send_text(keys)
        assert picker.choose_model("openai", "model-1", refresh=True) == (
            available[0] if keys == "\r" else None
        )

    rendered = "".join(terminal.writes)
    assert ">  1  Model 12 release" in rendered
    assert "Model 5 release" in rendered
    assert "Model 1 release" not in rendered
    assert "Current: model-1" in stream.getvalue()
    assert accounts.load_preference(paths) == before


def test_effort_picker_still_initially_selects_current_effort(
    paths: AppPaths,
    options: tuple[ModelOption, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picker, _ = _menu(paths, "")
    picker.plain = False
    monkeypatch.setattr(picker.stdin, "isatty", lambda: True)
    monkeypatch.setattr(picker.stream, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm")
    with create_pipe_input() as pipe:
        monkeypatch.setattr(models, "create_input", lambda **_: pipe)
        monkeypatch.setattr(models, "create_output", lambda **_: DummyOutput())
        pipe.send_text("\r")
        assert picker.choose_effort(options[0], "high") == "high"


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("\r", 1),
        ("\r\r", 1),
        ("\x1b\r", None),
        ("\x1b\x03", None),
        ("\x1b\x04", None),
        ("\x1b\x1b", None),
    ],
)
def test_picker_completes_once_when_exit_keys_arrive_together(
    paths: AppPaths,
    monkeypatch: pytest.MonkeyPatch,
    keys: str,
    expected: int | None,
) -> None:
    picker, _ = _menu(paths, "")
    choices = (
        models._Choice("other", "Other", "Another model"),
        models._Choice("current", "Current", "Currently selected model"),
    )
    errors: list[dict[str, Any]] = []

    def capture_exception(
        self: models.Application[int | None],
        loop: AbstractEventLoop,
        context: dict[str, Any],
    ) -> None:
        errors.append(context)

    # Capture the real event loop's failures instead of opening prompt-toolkit's
    # exception prompt, which would otherwise hide the error behind a result.
    monkeypatch.setattr(models.Application, "_handle_exception", capture_exception)
    with create_pipe_input() as pipe:
        monkeypatch.setattr(models, "create_input", lambda **_: pipe)
        monkeypatch.setattr(models, "create_output", lambda **_: DummyOutput())
        pipe.send_text(keys)
        assert (
            picker._interactive(choices, current="current", label="Model") == expected
        )
    assert errors == []


def test_provider_model_text_is_never_terminal_markup(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    option = ModelOption("safe-id", "[red]Name\x1b[2J", "Description\x1b[31m")
    monkeypatch.setattr(
        catalog,
        "list_models",
        lambda *_args, **_kwargs: ModelCatalog("openai", (option,), "account", ""),
    )
    picker, stream = _menu(paths, "1\n")
    assert picker.choose_model("openai") == option
    assert "[red]Name" in stream.getvalue()
    assert "\x1b" not in stream.getvalue()
