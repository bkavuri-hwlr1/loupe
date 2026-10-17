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


def test_plain_runtime_path_values_are_exact_not_venv_relocated(tmp_path: Path) -> None:
    from llm_cli.workspace.isolated import copy_runtime_files

    private, snapshot = tmp_path / "private", tmp_path / "snapshot"
    private.mkdir()
    snapshot.mkdir()
    (private / ".env").write_text("LOCAL_PATH=" + str(private) + "\n")
    register_runtime_paths(private, (".env",))
    copy_runtime_files(private, snapshot, (".env",))
    assert (snapshot / ".env").read_bytes() == (private / ".env").read_bytes()
    assert runtime_identity(private, [".env"]) == runtime_identity(snapshot, [".env"])


def test_venv_fixture_path_values_remain_exact_while_launchers_relocate(
    tmp_path: Path,
) -> None:
    primary, private = tmp_path / "primary", tmp_path / "private"
    (primary / ".venv/data").mkdir(parents=True)
    private.mkdir()
    value = "fixture root=" + str(primary)
    (primary / ".venv/data/fixture.txt").write_text(value)
    copy_dependency_tree(primary, private, ".venv")
    assert (private / ".venv/data/fixture.txt").read_text() == value
    assert runtime_identity(primary, [".venv"]) == runtime_identity(private, [".venv"])


def test_node_workspace_link_points_at_copied_private_source(
    tmp_path: Path,
    repository_factory: Any,
) -> None:
    primary = repository_factory(
        tmp_path,
        {
            ".gitignore": "node_modules/\n",
            "packages/local/package.json": '{"main":"index.js"}',
            "packages/local/index.js": "module.exports = 42;\n",
        },
    )
    (primary / "node_modules").mkdir()
    (primary / "node_modules/local").symlink_to("../packages/local")
    private, snapshot = tmp_path / "private", tmp_path / "snapshot"
    for destination in (private, snapshot):
        destination.mkdir()
        shutil.copytree(primary / "packages", destination / "packages")
        shutil.copyfile(primary / ".gitignore", destination / ".gitignore")
        subprocess.run(["git", "init", "--quiet"], cwd=destination, check=True)
    register_runtime_paths(private, ("node_modules",))
    register_runtime_paths(snapshot, ("node_modules",))
    assert copy_dependency_tree(primary, private, "node_modules") == ()
    assert (
        copy_dependency_tree(private, snapshot, "node_modules", pinned_private=True)
        == ()
    )
    assert runtime_identity(private, ["node_modules"]) == runtime_identity(
        snapshot, ["node_modules"]
    )
    link = snapshot / "node_modules/local"
    assert link.resolve() == snapshot / "packages/local"
    (link / "index.js").write_text("private only\n")
    assert (primary / "packages/local/index.js").read_text() == "module.exports = 42;\n"
    assert (private / "packages/local/index.js").read_text() == "module.exports = 42;\n"


@pytest.mark.parametrize(
    "target", ["../.env", "../.git", "../ignored", "../../outside"]
)
def test_node_workspace_link_refuses_private_admin_ignored_and_escape(
    tmp_path: Path,
    repository_factory: Any,
    target: str,
) -> None:
    primary = repository_factory(
        tmp_path,
        {
            ".gitignore": "node_modules/\n.env\nignored/\n",
            "source.py": "pass\n",
        },
    )
    (primary / ".env").write_text("TOKEN=private")
    (primary / "ignored").mkdir()
    (primary / "node_modules").mkdir()
    (primary / "node_modules/local").symlink_to(target)
    private = tmp_path / "private"
    private.mkdir()
    with pytest.raises(
        (LlmCoordError, ValueError), match=r"protected|excluded|escapes"
    ):
        copy_dependency_tree(primary, private, "node_modules")


def test_alternate_installed_python_venv_runs_after_private_relocation(
    tmp_path: Path,
) -> None:
    candidates = [
        shutil.which("python3.14"),
        shutil.which("python3.13"),
        "/opt/homebrew/bin/python3.14",
    ]
    base = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    alternate = next(
        (
            Path(path)
            for path in candidates
            if path and Path(path).is_file() and Path(path).resolve() != base
        ),
        None,
    )
    if alternate is None:
        pytest.skip("an alternate installed Python is unavailable")
    primary, private, snapshot = (
        tmp_path / name for name in ("primary", "private", "snapshot")
    )
    for path in (primary, private, snapshot):
        path.mkdir()
    subprocess.run(
        [str(alternate), "-m", "venv", "--without-pip", str(primary / ".venv")],
        check=True,
    )
    copy_dependency_tree(primary, private, ".venv")
    copy_dependency_tree(private, snapshot, ".venv", pinned_private=True)
    completed = subprocess.run(
        [
            ".venv/bin/python",
            "-c",
            "import sys; print(sys.version_info[:2]); import encodings",
        ],
        cwd=snapshot,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert runtime_identity(private, [".venv"]) == runtime_identity(snapshot, [".venv"])
    assert (snapshot / ".venv/bin/python").resolve() != base


def test_venv_external_python_must_match_cfg_and_be_native(tmp_path: Path) -> None:
    primary, private, external = (
        tmp_path / name for name in ("primary", "private", "external")
    )
    (primary / ".venv/bin").mkdir(parents=True)
    private.mkdir()
    external.mkdir()
    fake = external / "python3.99"
    fake.write_text("#!/bin/sh\nprintf unsafe\n")
    fake.chmod(0o755)
    (primary / ".venv/pyvenv.cfg").write_text("home = " + str(external) + "\n")
    (primary / ".venv/bin/python").symlink_to(fake)
    with pytest.raises(LlmCoordError, match="native"):
        copy_dependency_tree(primary, private, ".venv")
    shutil.rmtree(private / ".venv")
    (primary / ".venv/pyvenv.cfg").write_text("home = /nonexistent-python-base\n")
    with pytest.raises(LlmCoordError, match="pinned configuration"):
        copy_dependency_tree(primary, private, ".venv")


def test_nested_json_credentials_are_redacted_without_hiding_siblings(
    tmp_path: Path,
) -> None:
    (tmp_path / "settings.local").write_text(
        '{"database":{"password":"nested-private-value","host":"ordinary-host"},'
        '"services":[{"credentials":{"token":"nested-list-private-value"}}],'
        '"diagnostic":"assertion at line 42"}'
    )
    values = private_output_values(tmp_path, ("settings.local",))
    assert {"nested-private-value", "nested-list-private-value"} <= set(values)
    assert "ordinary-host" not in values
    assert "assertion at line 42" not in values
    events: list[str] = []
    code, output, clipped, stopped = supervised_run(
        [
            sys.executable,
            "-c",
            "import json; from pathlib import Path; "
            "d=json.loads(Path('settings.local').read_text()); "
            "print(d['database']['password']); "
            "print(d['services'][0]['credentials']['token']); "
            "print(d['database']['host']); print(d['diagnostic'])",
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
    assert "nested-private-value" not in output + repr(events)
    assert "nested-list-private-value" not in output + repr(events)
    assert "ordinary-host" in output and "assertion at line 42" in output


def test_numeric_and_toml_sensitive_fields_are_actual_private_values(
    tmp_path: Path,
) -> None:
    (tmp_path / "settings.local").write_text(
        '{"database":{"password":418529},"ordinary":123}'
    )
    (tmp_path / "auth.toml").write_text(
        '[database]\npassword = "toml-private-value"\nport = 9012\n'
    )
    values = private_output_values(tmp_path, ("settings.local", "auth.toml"))
    assert "418529" in values and "toml-private-value" in values
    assert "123" not in values and "9012" not in values


def test_exact_credential_descendant_consent_copies_only_approved_file(
    tmp_path: Path,
) -> None:
    primary, private = tmp_path / "primary", tmp_path / "private"
    (primary / "node_modules/pkg/.aws").mkdir(parents=True)
    private.mkdir()
    approved = "node_modules/pkg/.aws/credentials"
    (primary / approved).write_text("aws_access_key_id=approved-private-value\n")
    (primary / "node_modules/pkg/.aws/config").write_text("secret=unapproved-sibling\n")
    notices = copy_dependency_tree(
        primary, private, "node_modules", approved_secret_paths=(approved,)
    )
    assert (private / approved).read_bytes() == (primary / approved).read_bytes()
    assert not (private / "node_modules/pkg/.aws/config").exists()
    assert "node_modules/pkg/.aws/config" in repr(notices)


@pytest.mark.parametrize(
    "body",
    [
        b"TOKEN=opaque-private-value\0\n",
        b"TOKEN=opaque-private-value\xff\n",
    ],
)
@pytest.mark.parametrize("value_only", [False, True])
def test_literal_binary_private_input_never_reaches_output_or_events(
    tmp_path: Path,
    body: bytes,
    value_only: bool,
) -> None:
    (tmp_path / ".env").write_bytes(body)
    (tmp_path / "ordinary.pyc").write_bytes(b"\0ordinary unclassified cache")
    values = private_output_values(tmp_path, (".env", "ordinary.pyc"))
    assert values and all(
        "ordinary unclassified cache" not in value for value in values
    )
    events: list[str] = []
    command = (
        "print(Path('.env').read_bytes().split(b'=')[1]"
        ".split(bytes([0]))[0].decode(errors='ignore'))"
        if value_only
        else "sys.stdout.buffer.write(Path('.env').read_bytes()); "
        "sys.stdout.buffer.flush()"
    )
    code, output, clipped, stopped = supervised_run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "print('useful diagnostic', flush=True); " + command,
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
    assert "opaque-private-value" not in output + repr(events)
    assert "useful diagnostic" in output


@pytest.mark.parametrize("filename", ["secrets.yaml", "settings.local"])
def test_yaml_flow_credentials_and_quoted_scalars_are_screened_before_events(
    tmp_path: Path, filename: str
) -> None:
    (tmp_path / filename).write_text(
        "database: {password: inline-private-value, host: example.test}\n"
        "services: {token: 'quoted private, value', ordinary: diagnostic}\n"
        'auth: {password: "escaped\\nprivate"}\n'
    )
    values = private_output_values(tmp_path, (filename,))
    assert {"inline-private-value", "quoted private, value", "escaped\nprivate"} <= set(
        values
    )
    if filename == "settings.local":
        assert "example.test" not in values and "diagnostic" not in values
    events: list[str] = []
    code, output, clipped, stopped = supervised_run(
        [
            sys.executable,
            "-c",
            "print('inline-private-value'); print('quoted private, value'); "
            "print('escaped\\nprivate'); print('ordinary diagnostic survives')",
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
    assert "inline-private-value" not in output + repr(events)
    assert "quoted private, value" not in output + repr(events)
    assert "escaped" not in output + repr(events)
    assert "ordinary diagnostic survives" in output


def test_overlapping_short_secrets_do_not_expand_generated_placeholders(
    tmp_path: Path,
) -> None:
    values = (*"abcdefghijklmnopqrstuvwxyz", "test", "failed")
    events: list[str] = []
    code, output, clipped, stopped = supervised_run(
        [sys.executable, "-c", "print('test failed')"],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        deadline=time.monotonic() + 20,
        remaining=4096,
        cancelled=lambda: False,
        on_output=events.append,
        sensitive_values=values,
    )
    assert code == 0 and stopped is None and not clipped
    assert output == "[private value] [private value]\n"
    assert "".join(events) == output
    assert len(output) < 100


def test_rendered_redaction_and_events_respect_capture_byte_budget(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    code, output, clipped, stopped = supervised_run(
        [sys.executable, "-c", "print('z' * 4000)"],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        deadline=time.monotonic() + 20,
        remaining=4096,
        cancelled=lambda: False,
        on_output=events.append,
        sensitive_values=("z",),
    )
    assert code == 0 and stopped is None and clipped
    assert len(output.encode()) <= 4096
    assert len("".join(events).encode()) <= 4096
    assert "z" not in output + repr(events)


def test_short_private_value_cannot_hide_recognized_token_signature(
    tmp_path: Path,
) -> None:
    from llm_cli.agent.source_policy import contains_secret_material

    (tmp_path / ".env").write_text("PROMPT_SUFFIX=p\n")
    values = private_output_values(tmp_path, (".env",))
    body = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    assert contains_secret_material("ghp_" + body)
    events: list[str] = []
    code, output, clipped, stopped = supervised_run(
        [
            sys.executable,
            "-c",
            "print('ghp_' + 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')",
        ],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        deadline=time.monotonic() + 20,
        remaining=4096,
        cancelled=lambda: False,
        on_output=events.append,
        sensitive_values=values,
    )
    assert code == 0 and stopped == "error" and clipped
    assert body not in output + repr(events)
    assert "recognized secret material" in output


def test_library_code_named_like_credentials_is_copied(tmp_path: Path) -> None:
    # anthropic/lib/credentials and google/auth/credentials.py are imported by
    # their packages; leaving them out made `import anthropic` fail.
    primary, private = tmp_path / "primary", tmp_path / "private"
    site = primary / ".venv/lib/python3.12/site-packages"
    (site / "sdk/lib/credentials").mkdir(parents=True)
    (site / "sdk/lib/credentials/__init__.py").write_text("PROVIDERS = ()\n")
    (site / "auth").mkdir()
    (site / "auth/credentials.py").write_text("class Credentials: ...\n")
    (site / "auth/credentials.json").write_text('{"token": "data-file"}\n')
    (site / "auth/.aws").mkdir()
    (site / "auth/.aws/config").write_text("region=us-east-1\n")
    private.mkdir()
    notices = copy_dependency_tree(primary, private, ".venv")
    copied = private / ".venv/lib/python3.12/site-packages"
    assert (copied / "sdk/lib/credentials/__init__.py").is_file()
    assert (copied / "auth/credentials.py").is_file()
    assert not (copied / "auth/credentials.json").exists()
    assert not (copied / "auth/.aws").exists()
    assert "auth/credentials.json" in repr(notices)
    assert "auth/.aws" in repr(notices)


def test_dependency_code_and_short_unnamed_values_are_not_redacted(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "DEBUG=true\nPORT=8000\nAPI_KEY=k\n"
        "DATABASE_URL=postgres://app:pw@localhost/db\n"
    )
    package = tmp_path / ".venv/lib/python3.12/site-packages/pkg"
    package.mkdir(parents=True)
    (package / "auth.py").write_text(
        'token = "r"\npassword: str = "i"\nsecret_key = "library-default-value"\n'
    )
    (tmp_path / "node_modules/pkg/.aws").mkdir(parents=True)
    (tmp_path / "node_modules/pkg/.aws/credentials").write_text(
        "aws_secret_access_key=dependency-private-value\n"
    )
    values = set(private_output_values(tmp_path, (".env", ".venv", "node_modules")))
    assert {"k", "pw", "postgres://app:pw@localhost/db"} <= values
    assert "dependency-private-value" in values
    assert not {"true", "8000", "r", "i", "library-default-value"} & values


def test_screened_source_contributes_only_long_values(tmp_path: Path) -> None:
    # A test file with fake keys is screened source, not a credential file; its
    # short literals are test data and must not hide digits in every output.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_auth.py").write_text(
        'api_key = "fake-provider-key-123"\nmax_tokens = 4\npassword = "pw"\n'
    )
    values = set(
        private_output_values(tmp_path, (), source_paths=("tests/test_auth.py",))
    )
    assert "fake-provider-key-123" in values
    assert not {"4", "pw"} & values


def test_cached_runtime_identity_still_sees_same_size_edits(tmp_path: Path) -> None:
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/data").write_text("first")
    first = runtime_identity(tmp_path, ["runtime"])
    assert runtime_identity(tmp_path, ["runtime"]) == first
    (tmp_path / "runtime/data").write_text("other")
    assert runtime_identity(tmp_path, ["runtime"]) != first


def test_verified_copy_reuses_digest_and_invalidates_same_size_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.execution.checks import _remember_runtime_copy
    from llm_cli.workspace import isolated

    source, target = tmp_path / "source", tmp_path / "target"
    (source / ".venv/lib/pkg").mkdir(parents=True)
    target.mkdir()
    original = source / ".venv/lib/pkg/data.py"
    original.write_text("VALUE = 'first'\n")
    expected = runtime_identity(source, [".venv"])
    isolated.copy_pinned_runtime_file(
        source,
        target,
        ".venv",
        copy_observer=lambda path, before, after: _remember_runtime_copy(
            source, target, path, before, after
        ),
    )
    reads: list[int] = []
    original_read = isolated._read

    def record(descriptor: int, limit: int) -> bytes:
        reads.append(descriptor)
        return original_read(descriptor, limit)

    monkeypatch.setattr(isolated, "_read", record)
    assert runtime_identity(target, [".venv"]) == expected
    assert not reads
    copied = target / ".venv/lib/pkg/data.py"
    before = copied.stat()
    copied.write_text("VALUE = 'other'\n")
    os.utime(copied, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert runtime_identity(target, [".venv"]) != expected
    assert len(reads) == 1
    assert runtime_identity(source, [".venv"]) == expected
    assert original.read_text() == "VALUE = 'first'\n"


def test_verified_copy_evidence_does_not_hide_source_race(tmp_path: Path) -> None:
    from llm_cli.execution.checks import _remember_runtime_copy
    from llm_cli.workspace.isolated import copy_pinned_runtime_file

    source, target = tmp_path / "source", tmp_path / "target"
    (source / "node_modules/pkg").mkdir(parents=True)
    target.mkdir()
    original = source / "node_modules/pkg/index.js"
    original.write_text("first")
    expected = runtime_identity(source, ["node_modules"])

    def changed(path: str, before: os.stat_result, after: os.stat_result) -> None:
        _remember_runtime_copy(source, target, path, before, after)
        original.write_text("other")

    copy_pinned_runtime_file(source, target, "node_modules", copy_observer=changed)
    assert runtime_identity(source, ["node_modules"]) != expected
    assert runtime_identity(target, ["node_modules"]) == expected
    assert (target / "node_modules/pkg/index.js").read_text() == "first"


def test_private_parallel_copy_is_bounded_and_drains_after_worker_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.workspace import isolated

    source, target = tmp_path / "source", tmp_path / "target"
    package = source / "node_modules/pkg"
    package.mkdir(parents=True)
    target.mkdir()
    for index in range(12):
        (package / str(index)).write_bytes(b"original " + str(index).encode())
    original_clone = isolated._clone
    lock = threading.Lock()
    active = peak = completed = 0
    failures = False

    def observed_clone(descriptor: int, parent: int, name: str) -> bool:
        nonlocal active, peak, completed
        if name == "target":
            return original_clone(descriptor, parent, name)
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.02)
            if failures and name == "3":
                raise RuntimeError("copy worker failed")
            return original_clone(descriptor, parent, name)
        finally:
            with lock:
                active -= 1
                completed += 1

    monkeypatch.setattr(isolated, "_clone", observed_clone)
    copy_dependency_tree(source, target, "node_modules", pinned_private=True)
    assert 1 < peak <= 4
    assert active == 0 and completed == 12
    for original in package.iterdir():
        copied = target / "node_modules/pkg" / original.name
        assert copied.read_bytes() == original.read_bytes()
        assert not copied.stat().st_mode & 0o077
        copied.write_bytes(b"private changes")
        assert original.read_bytes().startswith(b"original ")
    shutil.rmtree(target / "node_modules")
    failures = True
    with pytest.raises(RuntimeError, match="worker failed"):
        copy_dependency_tree(source, target, "node_modules", pinned_private=True)
    assert active == 0
    finished = completed
    time.sleep(0.05)
    assert completed == finished
    assert all(path.read_bytes().startswith(b"original ") for path in package.iterdir())


def test_parallel_probe_failure_never_clones_user_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.workspace import isolated

    source, target = tmp_path / "source", tmp_path / "target"
    package = source / "node_modules/pkg"
    package.mkdir(parents=True)
    target.mkdir()
    for index in range(8):
        (package / str(index)).write_bytes(b"user dependency bytes")

    def broken(descriptor: int, parent: int, name: str) -> bool:
        assert isolated._read(descriptor, 1024) == b"private clone probe"
        os.link("source", name, src_dir_fd=parent, dst_dir_fd=parent)
        return True

    monkeypatch.setattr(isolated, "_clone", broken)
    with pytest.raises(LlmCoordError, match="isolate dependency"):
        copy_dependency_tree(source, target, "node_modules", pinned_private=True)
    assert all(
        path.read_bytes() == b"user dependency bytes" for path in package.iterdir()
    )
