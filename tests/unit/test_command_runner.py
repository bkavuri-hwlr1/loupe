"""Commands see pending edits in a disposable copy and never change the checkout."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

import pytest

from llm_cli.agent.tools import TaskCancelled
from llm_cli.coordination.scopes import ScopeValidationError
from llm_cli.execution import commands
from llm_cli.execution.commands import CommandRunner, cleanup_command_snapshots
from llm_cli.execution.sandbox import SandboxPolicy, available_sandbox
from llm_cli.workspace.batches import BatchFile
from llm_cli.workspace.identity import DIRECTORY_MODE, read_identified_path

KIND = available_sandbox()
needs_sandbox = pytest.mark.skipif(
    KIND is None, reason="no working operating-system sandbox on this machine"
)


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = (tmp_path / "repository").resolve()
    root.mkdir()
    (root / "app.py").write_text("VALUE = 1\n")
    (root / "src").mkdir()
    (root / "src" / "keep.txt").write_text("kept\n")
    (root / ".gitignore").write_text(".venv/\n.env\n")
    (root / ".env").write_text("TOKEN=hidden-value\n")
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "dep.txt").write_text("dependency\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-qm",
        "base",
    )
    return root


def _edit(repository: Path, path: str, content: bytes | None) -> BatchFile:
    base, _ = read_identified_path(repository / path)
    return BatchFile(relative_path=path, base=base, content=content, mode="100644")


def _runner(
    repository: Path,
    tmp_path: Path,
    *,
    files: Sequence[BatchFile] = (),
    cancelled: Callable[[], bool] = lambda: False,
    sandbox: str | None = None,
    events: list[tuple[str, dict[str, object]]] | None = None,
) -> CommandRunner:
    recorded = events if events is not None else []
    return CommandRunner(
        root=repository,
        snapshot_root=tmp_path / "snapshots",
        candidates=lambda: tuple(files),
        cancelled=cancelled,
        emit=lambda kind, payload: recorded.append((kind, payload)),
        lock=threading.RLock(),
        sandbox=sandbox or KIND or "none",
    )


@pytest.fixture
def unsandboxed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise snapshot handling on machines without a sandbox."""

    def passthrough(
        kind: str, policy: SandboxPolicy, argv: Sequence[str], *, cwd: Path
    ) -> list[str]:
        return list(argv)

    monkeypatch.setattr(commands, "wrap", passthrough)


@pytest.mark.usefixtures("unsandboxed")
def test_command_sees_pending_edits_and_its_changes_are_discarded(
    repository: Path, tmp_path: Path
) -> None:
    files = [
        _edit(repository, "app.py", b"VALUE = 2\n"),
        _edit(repository, "src/keep.txt", None),
        BatchFile(
            relative_path="new",
            base=read_identified_path(repository / "new")[0],
            content=None,
            mode=DIRECTORY_MODE,
        ),
    ]
    events: list[tuple[str, dict[str, object]]] = []
    runner = _runner(repository, tmp_path, files=files, events=events)

    result = runner.run(
        ["sh", "-c", "cat app.py; ls src; test -d new && echo dir; echo x > app.py"],
        ".",
        30,
    )

    assert not result.is_error
    assert result.content.startswith("exit 0 after ")
    assert "VALUE = 2" in result.content
    assert "keep.txt" not in result.content
    assert "dir" in result.content
    assert (repository / "app.py").read_text() == "VALUE = 1\n"
    assert not list((tmp_path / "snapshots").iterdir())
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "command.started" and kinds[-1] == "command.finished"
    started = events[0][1]
    assert started["argv"][0] == "sh" and started["cwd"] == "."
    finished = events[-1][1]
    assert finished["state"] == "completed" and finished["exit_code"] == 0


@pytest.mark.usefixtures("unsandboxed")
def test_ignored_files_are_not_copied_but_dependencies_are_linked(
    repository: Path, tmp_path: Path
) -> None:
    runner = _runner(repository, tmp_path)

    result = runner.run(
        ["sh", "-c", "test -e .env || echo no-env; cat .venv/lib/dep.txt"], ".", 30
    )

    assert "no-env" in result.content
    assert "dependency" in result.content


@pytest.mark.usefixtures("unsandboxed")
def test_nonzero_exit_is_information_not_a_tool_error(
    repository: Path, tmp_path: Path
) -> None:
    result = _runner(repository, tmp_path).run(
        ["sh", "-c", "echo fail; exit 3"], ".", 30
    )

    assert not result.is_error
    assert result.content.startswith("exit 3 after ")


@pytest.mark.usefixtures("unsandboxed")
def test_timeout_stops_the_command(repository: Path, tmp_path: Path) -> None:
    result = _runner(repository, tmp_path).run(["sleep", "30"], ".", 1)

    assert result.is_error
    assert result.content.startswith("timed out after ")


# Leaves the command's process group and keeps its output open. A unique token
# among its arguments finds it from outside any process namespace.
_DETACHED = "import os, time; os.setsid(); time.sleep(30)"


def _detached_command(token: str, then: str = "") -> list[str]:
    script = f'{sys.executable} -c "$0" {token} & echo started{then}'
    return ["sh", "-c", script, _DETACHED]


def _processes(token: str) -> list[int]:
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,command="], capture_output=True, text=True, check=True
    ).stdout
    return [int(line.split()[0]) for line in listing.splitlines() if token in line]


def _gone(token: str) -> bool:
    deadline = time.monotonic() + 5
    while _processes(token):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _kill(token: str) -> None:
    for pid in _processes(token):
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.usefixtures("unsandboxed")
def test_a_detached_process_cannot_hold_a_command_open(
    repository: Path, tmp_path: Path
) -> None:
    token = f"loupe-detached-{uuid.uuid4().hex}"
    started = time.monotonic()
    try:
        result = _runner(repository, tmp_path).run(_detached_command(token), ".", 60)

        assert time.monotonic() - started < 10
        # Finishing is not a timeout, even if reading output ran past one.
        assert result.content.startswith("exit 0 after "), result.content
        assert "started" in result.content
        if sys.platform.startswith("linux"):
            # The supervisor adopts and stops it; macOS has no equivalent
            # outside the sandbox.
            assert _gone(token)
    finally:
        _kill(token)


@needs_sandbox
@pytest.mark.parametrize("finishes", [True, False])
def test_sandboxed_command_leaves_no_detached_process(
    repository: Path, tmp_path: Path, finishes: bool
) -> None:
    token = f"loupe-detached-{uuid.uuid4().hex}"
    command = _detached_command(token, "" if finishes else "; sleep 30")
    started = time.monotonic()
    try:
        result = _runner(repository, tmp_path).run(command, ".", 30 if finishes else 1)

        assert time.monotonic() - started < 10
        assert result.content.startswith("exit 0" if finishes else "timed out")
        assert _gone(token)
    finally:
        _kill(token)


@pytest.mark.usefixtures("unsandboxed")
def test_cancellation_stops_the_command_and_the_task(
    repository: Path, tmp_path: Path
) -> None:
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    runner = _runner(repository, tmp_path, cancelled=stop.is_set)

    with pytest.raises(TaskCancelled):
        runner.run(["sleep", "30"], ".", 60)

    assert not list((tmp_path / "snapshots").iterdir())


@pytest.mark.usefixtures("unsandboxed")
def test_working_directory_must_exist_inside_the_snapshot(
    repository: Path, tmp_path: Path
) -> None:
    runner = _runner(repository, tmp_path)

    assert runner.run(["pwd"], "src", 30).content.splitlines()[1].endswith("/src")
    missing = runner.run(["pwd"], "missing", 30)
    assert missing.is_error and "not a directory" in missing.content
    with pytest.raises(ScopeValidationError, match="dot path components"):
        runner.run(["pwd"], "../elsewhere", 30)


@pytest.mark.usefixtures("unsandboxed")
def test_secret_material_is_refused_in_arguments_and_withheld_in_output(
    repository: Path, tmp_path: Path
) -> None:
    runner = _runner(repository, tmp_path)

    refused = runner.run(["echo", "AKIAABCDEFGHIJKLMNOP"], ".", 30)
    assert refused.is_error and "secret material" in refused.content

    printed = runner.run(
        [
            "sh",
            "-c",
            "echo BEFORE-MARK; printf 'AKIA%s\\n' ABCDEFGHIJKLMNOP; echo AFTER-MARK",
        ],
        ".",
        30,
    )
    assert printed.content.startswith("exit 0 after ")
    assert "BEFORE-MARK" in printed.content
    assert "ABCDEFGHIJKLMNOP" not in printed.content
    assert "AFTER-MARK" not in printed.content
    assert "withheld" in printed.content


@pytest.mark.usefixtures("unsandboxed")
def test_long_output_keeps_the_start_and_the_end(
    repository: Path, tmp_path: Path
) -> None:
    script = "for i in $(seq 1 20000); do echo line-$i; done"

    result = _runner(repository, tmp_path).run(["sh", "-c", script], ".", 60)

    assert "line-1\n" in result.content
    assert "line-20000" in result.content
    assert "bytes of output omitted" in result.content
    assert len(result.content.encode()) < 40 * 1024


@pytest.mark.usefixtures("unsandboxed")
def test_environment_carries_no_daemon_secrets(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-leak")

    result = _runner(repository, tmp_path).run(["env"], ".", 30)

    assert "should-not-leak" not in result.content
    assert "PATH=" in result.content
    assert "GIT_TERMINAL_PROMPT=0" in result.content


def test_leftover_snapshots_are_removed(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    (root / "command_old" / "source").mkdir(parents=True)
    (root / "unrelated").mkdir()

    cleanup_command_snapshots(root)

    assert sorted(path.name for path in root.iterdir()) == ["unrelated"]
    cleanup_command_snapshots(tmp_path / "missing")


@needs_sandbox
def test_sandboxed_command_cannot_read_the_real_checkout(
    repository: Path, tmp_path: Path
) -> None:
    runner = _runner(repository, tmp_path)

    copy = runner.run(["cat", "app.py"], ".", 30)
    real = runner.run(["cat", str(repository / ".env")], ".", 30)
    write = runner.run(["sh", "-c", "echo x > .venv/lib/new.txt"], ".", 30)

    assert "VALUE = 1" in copy.content
    assert "hidden-value" not in real.content
    assert real.content.startswith("exit 1")
    assert not (repository / ".venv" / "lib" / "new.txt").exists()
    assert not write.content.startswith("exit 0")
