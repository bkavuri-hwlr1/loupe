"""Private dependency adapters and actual-value output screening."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from llm_cli.agent.source_policy import register_runtime_paths
from llm_cli.errors import LlmCoordError
from llm_cli.execution.checks import runtime_identity, supervised_run
from llm_cli.execution.commands import CommandRunner
from llm_cli.workspace.runtime import copy_dependency_tree, private_output_values


def test_venv_relative_pytest_runs_after_two_private_relocations(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    private = tmp_path / "private"
    command = tmp_path / "command"
    for root in (primary, private, command):
        root.mkdir()
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(primary / ".venv")],
        check=True,
    )
    # Install the local pytest toolchain without network or an installer. The
    # entry point is exactly the absolute-shebang layout an installed venv uses.
    import importlib.metadata

    site = (
        primary
        / ".venv/lib"
        / ("python" + str(sys.version_info.major) + "." + str(sys.version_info.minor))
        / "site-packages"
    )
    for name in ("pytest", "iniconfig", "packaging", "pluggy", "pygments"):
        distribution = importlib.metadata.distribution(name)
        for file in distribution.files or ():
            if ".." in file.parts or file.suffix == ".pyc":
                continue
            source = Path(distribution.locate_file(file))
            if source.is_file():
                target = site / file
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    launcher = primary / ".venv/bin/pytest"
    launcher.write_text(
        "#!"
        + str(primary / ".venv/bin/python")
        + "\nfrom pytest import console_main\nraise SystemExit(console_main())\n"
    )
    launcher.chmod(0o755)
    assert copy_dependency_tree(primary, private, ".venv") == ()
    assert copy_dependency_tree(private, command, ".venv") == ()
    (command / "test_local.py").write_text(
        "def test_private():\n    assert 1 + 1 == 2\n"
    )
    completed = subprocess.run(
        [".venv/bin/pytest", "-q", "test_local.py"],
        cwd=command,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout
    assert runtime_identity(private, [".venv"]) == runtime_identity(command, [".venv"])
    assert (command / ".venv/bin/python").resolve() == Path(
        sys._base_executable
    ).resolve()
    (command / ".venv/bin/pytest").write_text("private change")
    assert launcher.read_text().startswith("#!")
    assert (private / ".venv/bin/pytest").read_text().startswith("#!")


def test_node_relative_bin_runs_without_primary_aliases(tmp_path: Path) -> None:
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm is unavailable")
    primary, private = tmp_path / "primary", tmp_path / "private"
    (primary / "node_modules/.bin").mkdir(parents=True)
    (primary / "node_modules/local").mkdir()
    private.mkdir()
    script = primary / "node_modules/local/check.js"
    script.write_text(
        "#!/usr/bin/env node\nconsole.log('local dependency check passed');\n"
    )
    script.chmod(0o755)
    (primary / "node_modules/.bin/local-check").symlink_to("../local/check.js")
    (primary / "node_modules/local/.env").write_text("TOKEN=never-copy-this")
    notices = copy_dependency_tree(primary, private, "node_modules")
    assert "node_modules/local/.env" in repr(notices)
    assert not (private / "node_modules/local/.env").exists()
    (private / "package.json").write_text('{"scripts":{"test":"local-check"}}')
    completed = subprocess.run(
        [npm, "test", "--silent"], cwd=private, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "local dependency check passed" in completed.stdout
    (private / "node_modules/local/check.js").write_text("private change")
    assert "local dependency" in script.read_text()


def test_dependency_escape_and_special_nodes_are_refused(tmp_path: Path) -> None:
    primary, private = tmp_path / "primary", tmp_path / "private"
    (primary / "node_modules").mkdir(parents=True)
    private.mkdir()
    (primary / "node_modules/escape").symlink_to("../../outside")
    with pytest.raises(LlmCoordError, match="escapes"):
        copy_dependency_tree(primary, private, "node_modules")
    shutil.rmtree(private / "node_modules")
    (primary / "node_modules/escape").unlink()
    os.mkfifo(primary / "node_modules/fifo")
    with pytest.raises(LlmCoordError, match="special"):
        copy_dependency_tree(primary, private, "node_modules")


def test_actual_values_are_redacted_before_streaming_with_useful_output(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text('TOKEN="short-value"\nPASSWORD=xy\n')
    (tmp_path / "cache.pyc").write_bytes(b"\0unrelated ignored bytecode")
    values = private_output_values(tmp_path, (".env", "cache.pyc"))
    events: list[str] = []
    code, output, clipped, stopped = supervised_run(
        [
            sys.executable,
            "-c",
            "import sys,time; print('test assertion failed at line 12'); "
            "sys.stdout.write('short-'); sys.stdout.flush(); time.sleep(.05); "
            "print('value'); print('xy'); print('diagnostic after secret')",
        ],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        deadline=time.monotonic() + 20,
        remaining=4096,
        cancelled=lambda: False,
        on_output=events.append,
        sensitive_values=values,
    )
    assert code == 0 and stopped is None and not clipped
    assert "short-value" not in output and "xy" not in output
    assert "short-value" not in "".join(events)
    assert "test assertion failed at line 12" in output
    assert "diagnostic after secret" in output
    assert "[private value]" in output


def test_command_ignored_cache_keeps_output_and_secret_never_reaches_events(
    tmp_path: Path,
    repository_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(
        tmp_path, {"a.py": "pass\n", ".gitignore": ".env\ncache.pyc\n"}
    )
    (root / ".env").write_text("TOKEN=local-runtime-test-value\n")
    (root / "cache.pyc").write_bytes(b"\0ordinary bytecode")
    register_runtime_paths(root, (".env", "cache.pyc"))
    from llm_cli.execution import commands

    monkeypatch.setattr(commands, "wrap", lambda kind, policy, argv, *, cwd: list(argv))
    events: list[tuple[str, dict[str, object]]] = []
    runner = CommandRunner(
        root=root,
        snapshot_root=tmp_path / "snapshots",
        candidates=lambda: (),
        cancelled=lambda: False,
        emit=lambda kind, payload: events.append((kind, payload)),
        lock=threading.RLock(),
        sandbox="none",
        runtime_files=(".env", "cache.pyc"),
    )
    result = runner.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; print('useful assertion failed'); "
            "print(Path('.env').read_text()); print('done')",
        ],
        ".",
        20,
    )
    assert not result.is_error
    assert "useful assertion failed" in result.content and "done" in result.content
    assert "local-runtime-test-value" not in result.content
    assert "local-runtime-test-value" not in repr(events)


def test_credential_dependency_consent_remains_pinned_in_command_copies(
    tmp_path: Path,
) -> None:
    primary, private, command = (
        tmp_path / name for name in ("primary", "private", "command")
    )
    (primary / "node_modules/pkg").mkdir(parents=True)
    private.mkdir()
    command.mkdir()
    credential = "node_modules/pkg/.env"
    (primary / credential).write_text("TOKEN=dependency-private-value\n")
    assert (
        copy_dependency_tree(
            primary, private, "node_modules", approved_secret_paths=(credential,)
        )
        == ()
    )
    assert (
        copy_dependency_tree(private, command, "node_modules", pinned_private=True)
        == ()
    )
    assert (command / credential).read_text() == (private / credential).read_text()
    assert "dependency-private-value" in private_output_values(
        command, ("node_modules",)
    )


def test_cow_probe_never_operates_on_user_dependency_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.workspace import isolated

    primary, private = tmp_path / "primary", tmp_path / "private"
    (primary / "node_modules/pkg").mkdir(parents=True)
    private.mkdir()
    original = primary / "node_modules/pkg/module.js"
    original.write_bytes(b"original user dependency")

    def broken(source: int, parent: int, name: str) -> bool:
        assert isolated._read(source, 1024) == b"private clone probe"
        # Simulate a COW implementation that aliases writable source blocks.
        os.link("source", name, src_dir_fd=parent, dst_dir_fd=parent)
        return True

    monkeypatch.setattr(isolated, "_clone", broken)
    with pytest.raises(LlmCoordError, match="isolate dependency"):
        copy_dependency_tree(primary, private, "node_modules")
    assert original.read_bytes() == b"original user dependency"


def test_sensitive_config_values_cover_netrc_urls_and_multiline_fields(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text('TOKEN="inline-private-value" # a comment\n')
    (tmp_path / ".netrc").write_text(
        "machine example.test login operator password narrow-secret"
    )
    (tmp_path / ".git-credentials").write_text(
        "https://user:url%2Dsecret@example.test/repo"
    )
    (tmp_path / "secrets.yaml").write_text("password: |\n  multiline-secret\n")
    values = private_output_values(
        tmp_path, (".env", ".netrc", ".git-credentials", "secrets.yaml")
    )
    assert {
        "inline-private-value",
        "narrow-secret",
        "url-secret",
        "multiline-secret",
    } <= set(values)


@pytest.mark.parametrize("leak", [False, True])
def test_hook_returns_source_only_after_private_value_screening(
    tmp_path: Path,
    repository_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    leak: bool,
) -> None:
    root = repository_factory(tmp_path, {"source.py": "pass\n", ".gitignore": ".env\n"})
    (root / ".env").write_text("TOKEN=hook-private-value\n")
    register_runtime_paths(root, (".env",))
    from llm_cli.execution import commands

    monkeypatch.setattr(commands, "wrap", lambda kind, policy, argv, *, cwd: list(argv))
    runner = CommandRunner(
        root=root,
        snapshot_root=tmp_path / "snapshots",
        candidates=lambda: (),
        cancelled=lambda: False,
        emit=lambda kind, payload: None,
        lock=threading.RLock(),
        sandbox="none",
        runtime_files=(".env",),
    )
    expression = "Path('.env').read_text()" if leak else "'safe generated source'"
    result = runner.run_hook(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('generated.txt').write_text("
            + expression
            + ")",
        ],
        20,
        read_back=("generated.txt",),
    )
    assert "hook-private-value" not in result.output
    if leak:
        assert result.state == "error" and result.files == {}
    else:
        assert result.state == "completed"
        assert result.files == {"generated.txt": b"safe generated source"}
