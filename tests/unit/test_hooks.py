"""User hooks: configuration, blocking and rewriting, and sandboxed runs."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from llm_cli.agent.hooks import HookRunner, hook_label
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.config.loader import load_settings
from llm_cli.config.models import HookConfig
from llm_cli.errors import LlmCoordError
from llm_cli.execution.commands import CommandRunner
from llm_cli.execution.sandbox import available_sandbox

KIND = available_sandbox()
needs_sandbox = pytest.mark.skipif(
    KIND is None, reason="no working operating-system sandbox on this machine"
)
GUARD = HookConfig("pre_tool", ("write_file", "apply_patch"), ("./guard.sh",), 30)
FORMAT = HookConfig("post_edit", ("*.md",), ("fmt", "--quiet", "{paths}"), 30)


def test_hooks_are_configured_strictly(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[[hooks.pre_tool]]
match = ["write_file", "mcp__*"]
command = ["sh", "-c", "./scripts/guard.sh"]
timeout = 10
name = "path guard"

[[hooks.post_edit]]
match = ["*.py"]
command = ["ruff", "format", "{paths}"]
""",
        encoding="utf-8",
    )
    assert load_settings(config).hooks == (
        HookConfig(
            "pre_tool",
            ("write_file", "mcp__*"),
            ("sh", "-c", "./scripts/guard.sh"),
            10,
            "path guard",
        ),
        HookConfig("post_edit", ("*.py",), ("ruff", "format", "{paths}"), 60),
    )
    for bad in (
        '[hooks.pre_tool]\nmatch = ["x"]\ncommand = ["y"]\n',
        '[[hooks.on_start]]\nmatch = ["x"]\ncommand = ["y"]\n',
        '[[hooks.pre_tool]]\nmatch = []\ncommand = ["y"]\n',
        '[[hooks.pre_tool]]\nmatch = ["x"]\ncommand = ["y", "{paths}"]\n',
        '[[hooks.post_edit]]\nmatch = ["x"]\ncommand = ["y"]\ntimeout = 0\n',
        '[[hooks.post_edit]]\nmatch = ["x"]\ncommand = ["y"]\nwhen = "now"\n',
    ):
        config.write_text(bad, encoding="utf-8")
        with pytest.raises(LlmCoordError):
            load_settings(config)


@dataclass
class Run:
    state: str = "completed"
    exit_code: int | None = 0
    output: str = ""
    files: dict[str, bytes | None] = field(default_factory=dict)


class FakeHooks:
    """Records hook runs and returns scripted results in order."""

    def __init__(self, *runs: Run) -> None:
        self.runs = list(runs)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], timeout: int, **kwargs: Any) -> Run:
        self.calls.append({"argv": argv, "timeout": timeout, **kwargs})
        return self.runs.pop(0)


def _runner(
    *runs: Run, hooks: Sequence[HookConfig] = (GUARD, FORMAT)
) -> tuple[HookRunner, FakeHooks, list[tuple[str, dict[str, object]]]]:
    fake = FakeHooks(*runs)
    events: list[tuple[str, dict[str, object]]] = []
    return HookRunner(hooks, fake, lambda k, p: events.append((k, p))), fake, events


def test_a_pre_tool_hook_sees_the_call_and_blocks_with_exit_2() -> None:
    runner, fake, events = _runner(
        Run(), Run(exit_code=2, output="docs/secret.md is protected\n")
    )

    assert runner.before_tool("read_file", {"path": "a.md"}) is None
    assert fake.calls == []  # Not a matching tool.
    assert runner.before_tool("write_file", {"path": "a.md"}) is None
    assert fake.calls[0]["hook_input"] == {
        "tool": "write_file",
        "arguments": {"path": "a.md"},
    }
    blocked = runner.before_tool("apply_patch", {"path": "docs/secret.md"})
    assert blocked == ("guard.sh", "docs/secret.md is protected")
    assert events == [("hook.blocked", {"hook": "guard.sh", "tool": "apply_patch"})]


def test_other_pre_tool_failures_are_reported_but_do_not_block() -> None:
    runner, _, events = _runner(Run(exit_code=1), Run(state="timed_out"))

    assert runner.before_tool("write_file", {}) is None
    assert runner.before_tool("write_file", {}) is None
    assert [payload["reason"] for _, payload in events] == ["exit 1", "timed out"]


def _edit(
    runner: HookRunner,
    paths: Sequence[str],
    pending: dict[str, bytes],
    refuse: Callable[[str], str | None] = lambda path: None,
) -> list[str]:
    def stage(path: str, content: bytes) -> str | None:
        refusal = refuse(path)
        if refusal is None:
            pending[path] = content
        return refusal

    return runner.after_edit(paths, current=pending.get, stage=stage)


def test_a_post_edit_hook_rewrites_matching_files_as_staged_edits() -> None:
    runner, fake, events = _runner(Run(files={"docs/a.md": b"Formatted\n"}))
    pending = {"docs/a.md": b"draft\n", "src/b.py": b"x = 1\n"}

    notes = _edit(runner, ["docs/a.md"], pending)

    assert fake.calls[0]["argv"] == ["fmt", "--quiet", "docs/a.md"]
    assert fake.calls[0]["read_back"] == ["docs/a.md"]
    assert pending["docs/a.md"] == b"Formatted\n"
    assert notes == [
        "[post_edit hook 'fmt --quiet' changed docs/a.md; read it again before "
        "patching it]"
    ]
    assert events == [("hook.changed", {"hook": "fmt --quiet", "paths": ["docs/a.md"]})]
    # Paths that match no hook run nothing.
    assert _edit(runner, ["src/b.py"], pending) == []
    assert len(fake.calls) == 1


def test_unchanged_removed_refused_and_failed_rewrites_leave_the_edit() -> None:
    runner, _, events = _runner(
        Run(files={"a.md": b"same\n"}),
        Run(files={"a.md": None}),
        Run(files={"a.md": b"new\n"}),
        Run(exit_code=1, output="a.md:1: unexpected heading"),
    )
    pending = {"a.md": b"same\n"}

    assert _edit(runner, ["a.md"], pending) == []
    assert _edit(runner, ["a.md"], pending) == [
        "[post_edit hook 'fmt --quiet' removed a.md; ignored]"
    ]
    refused = _edit(runner, ["a.md"], pending, refuse=lambda path: "too large")
    assert refused == [
        "[post_edit hook 'fmt --quiet' changed a.md, but the change was not "
        "staged: too large]"
    ]
    failed = _edit(runner, ["a.md"], pending)
    assert failed == [
        "[post_edit hook 'fmt --quiet' failed (exit 1); the edit is unchanged. "
        "Its output:\na.md:1: unexpected heading]"
    ]
    assert pending == {"a.md": b"same\n"}
    assert [kind for kind, _ in events] == ["hook.failed"]


def test_hook_labels_are_short_and_skip_placeholders() -> None:
    assert hook_label(FORMAT) == "fmt --quiet"
    assert (
        hook_label(HookConfig("post_edit", ("*",), ("/usr/bin/black", "{paths}")))
        == "black"
    )
    named = HookConfig("pre_tool", ("*",), ("sh", "-c", "exit 0"), name="path guard")
    assert hook_label(named) == "path guard"


@pytest.fixture
def checkout(tmp_path: Path, git_run: Callable[..., str]) -> Path:
    root = (tmp_path / "checkout").resolve()
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text("original\n")
    git_run(root, "init", "-q")
    git_run(root, "add", "-A")
    return root


def _broker(checkout: Path, hooks: HookRunner, **kwargs: Any) -> SharedToolBroker:
    broker = SharedToolBroker(worktree=checkout, scopes=("docs/",), **kwargs)
    broker.hooks = hooks
    return broker


def test_a_blocked_call_is_refused_counted_and_never_staged(checkout: Path) -> None:
    runner, _, _ = _runner(Run(exit_code=2, output="guide is frozen"))
    broker = _broker(checkout, runner)
    assert not broker.invoke("read_file", {"path": "docs/guide.md"}).is_error

    refused = broker.invoke("write_file", {"path": "docs/guide.md", "content": "x\n"})

    assert refused.is_error
    assert refused.content == (
        "a pre_tool hook (guard.sh) blocked this call: guide is frozen"
    )
    assert broker.usage.calls == 2 and broker.usage.denied == 1
    assert broker.candidates() == ()


def test_a_rewritten_edit_is_what_gets_staged(checkout: Path) -> None:
    runner, _, _ = _runner(Run(), Run(files={"docs/guide.md": b"# Formatted\n"}))
    broker = _broker(checkout, runner)
    broker.invoke("read_file", {"path": "docs/guide.md"})

    written = broker.invoke(
        "write_file", {"path": "docs/guide.md", "content": "draft\n"}
    )

    assert not written.is_error
    assert written.content.endswith(
        "[post_edit hook 'fmt --quiet' changed docs/guide.md; read it again "
        "before patching it]"
    )
    (staged,) = broker.candidates()
    assert staged.content == b"# Formatted\n"


def test_hooks_do_not_run_for_tools_that_are_not_offered(checkout: Path) -> None:
    runner, fake, _ = _runner()
    broker = _broker(checkout, runner, agent_mode="plan")

    refused = broker.invoke("write_file", {"path": "docs/guide.md", "content": "x"})

    assert refused.is_error and "Plan mode is read-only" in refused.content
    assert fake.calls == []


def _command_runner(checkout: Path, tmp_path: Path) -> CommandRunner:
    return CommandRunner(
        root=checkout,
        snapshot_root=tmp_path / "snapshots",
        candidates=lambda: (),
        cancelled=lambda: False,
        emit=lambda kind, payload: None,
        lock=threading.RLock(),
        sandbox=KIND or "none",
    )


@needs_sandbox
def test_a_sandboxed_hook_reads_its_input_and_hands_back_files(
    checkout: Path, tmp_path: Path
) -> None:
    runner = _command_runner(checkout, tmp_path)
    guard = (
        'grep -q \'"path": "docs/guide.md"\' "$LOUPE_HOOK_INPUT" '
        "&& { echo protected; exit 2; }; exit 0"
    )

    blocked = runner.run_hook(
        ["sh", "-c", guard],
        30,
        hook_input={"tool": "w", "arguments": {"path": "docs/guide.md"}},
    )
    allowed = runner.run_hook(
        ["sh", "-c", guard], 30, hook_input={"tool": "w", "arguments": {"path": "x"}}
    )
    rewrite = runner.run_hook(
        [
            "sh",
            "-c",
            'for f; do tr a-z A-Z < "$f" > "$f.t" && mv "$f.t" "$f"; done',
            "hook",
            "docs/guide.md",
        ],
        30,
        read_back=["docs/guide.md"],
    )

    assert (blocked.exit_code, blocked.output.strip()) == (2, "protected")
    assert allowed.exit_code == 0
    assert rewrite.files == {"docs/guide.md": b"ORIGINAL\n"}
    # The checkout itself is untouched; the snapshot is discarded.
    assert (checkout / "docs" / "guide.md").read_text() == "original\n"
    assert not any((tmp_path / "snapshots").iterdir())


@needs_sandbox
def test_a_hook_cannot_reach_the_real_checkout(checkout: Path, tmp_path: Path) -> None:
    runner = _command_runner(checkout, tmp_path)
    attempt = runner.run_hook(
        ["sh", "-c", f"echo changed > {checkout}/docs/guide.md"], 30
    )
    assert attempt.exit_code != 0
    assert (checkout / "docs" / "guide.md").read_text() == "original\n"
