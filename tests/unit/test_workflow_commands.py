"""Public workflow CLI and live stop input contracts."""

from __future__ import annotations

import contextlib
import io
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys

from llm_cli.cli import active_controls
from llm_cli.cli.app import build_parser, dispatch
from llm_cli.cli.output import emit
from llm_cli.execution.checks import validate_config


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.called = threading.Event()

    def call(self, method: str, params: dict[str, Any]) -> dict[str, str]:
        self.calls.append((method, params))
        self.called.set()
        return {"state": "stopping"}


@pytest.mark.parametrize("action", ["diff", "checks", "undo", "cancel", "apply"])
def test_cli_dispatches_task_operations(action: str) -> None:
    client = Client()
    args = build_parser().parse_args(["task", action, "task-example"])
    dispatch(args, client)
    assert client.calls[0][0] == "task." + action
    assert client.calls[0][1]["task_id"] == "task-example"
    if action == "apply":
        assert not client.calls[0][1]["allow_unverified"]


def test_explicit_apply_override_and_publication_mode() -> None:
    client = Client()
    args = build_parser().parse_args(
        ["task", "apply", "task-example", "--allow-unverified"]
    )
    dispatch(args, client)
    assert client.calls[0][1]["allow_unverified"] is True
    assert (
        build_parser().parse_args(["chat", "--publish", "review"]).publish == "review"
    )
    assert build_parser().parse_args(["chat"]).publish is None


def test_configuration_import_and_json_output(tmp_path: Path) -> None:
    config = tmp_path / "checks.toml"
    config.write_text('[checks.test]\nargv = ["python3", "-m", "pytest"]\n')
    client = Client()
    dispatch(
        build_parser().parse_args(["checks", "configure", "--file", str(config)]),
        client,
    )
    assert client.calls[0][0] == "checks.configure"
    normalized = validate_config(client.calls[0][1]["config"])
    assert normalized["checks"]["test"]["timeout"] == 600
    assert normalized["checks"]["test"]["required"] is True
    output = io.StringIO()
    emit(
        {"diff": "-old\n+new\n", "status": "awaiting_review"},
        as_json=True,
        stream=output,
    )
    import json

    assert json.loads(output.getvalue())["diff"] == "-old\n+new\n"


@pytest.mark.parametrize(
    "config",
    [
        {"checks": {"bad": {"argv": "sh -c unsafe"}}},
        {"checks": {"bad": {"argv": ["test"], "cwd": "../outside"}}},
        {"checks": {"bad": {"argv": ["test"], "timeout": 0}}},
        {"checks": {"bad": {"argv": ["test"], "required": "yes"}}},
        {"runtime_paths": [".git/config"]},
    ],
)
def test_configuration_rejects_invalid_commands(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_config(config)


def test_live_stop_input_has_bounded_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()

    class Keyboard:
        def fileno(self) -> int:
            return read_fd

        def raw_mode(self) -> Any:
            return contextlib.nullcontext()

        def read_keys(self) -> list[KeyPress]:
            data = os.read(read_fd, 100).decode()
            return [KeyPress(Keys.ControlM if c == "\n" else c, c) for c in data]

        def close(self) -> None:
            os.close(read_fd)

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(active_controls, "create_input", lambda **_: Keyboard())
    client = Client()
    output = io.StringIO()
    try:
        with active_controls.active_controls(
            client, "running-task", Terminal(), output
        ):
            os.write(write_fd, b"/stop\n")
            assert client.called.wait(2)
        assert client.calls == [("task.cancel", {"task_id": "running-task"})]
        assert not any(
            t.name == "loupe-active-controls" for t in threading.enumerate()
        )
    finally:
        os.close(write_fd)


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("/effort high", "so /effort was not run"),
        ("/model", "so /model was not run"),
        ("what changed?", "your message was not sent"),
    ],
)
def test_live_input_other_than_stop_is_explained_not_dropped(
    monkeypatch: pytest.MonkeyPatch, typed: str, expected: str
) -> None:
    read_fd, write_fd = os.pipe()

    class Keyboard:
        def fileno(self) -> int:
            return read_fd

        def raw_mode(self) -> Any:
            return contextlib.nullcontext()

        def read_keys(self) -> list[KeyPress]:
            data = os.read(read_fd, 100).decode()
            return [KeyPress(Keys.ControlM if c == "\n" else c, c) for c in data]

        def close(self) -> None:
            os.close(read_fd)

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(active_controls, "create_input", lambda **_: Keyboard())
    client = Client()
    notices: list[str] = []
    noticed = threading.Event()

    def notice(text: str) -> None:
        notices.append(text)
        noticed.set()

    try:
        with active_controls.active_controls(
            client, "running-task", Terminal(), io.StringIO(), notice=notice
        ):
            os.write(write_fd, typed.encode() + b"\n")
            assert noticed.wait(2)
        assert client.calls == []
        assert len(notices) == 1
        assert expected in notices[0]
        assert "/stop" in notices[0]
    finally:
        os.close(write_fd)
