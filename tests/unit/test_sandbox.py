"""Model-chosen commands run only inside a working operating-system sandbox."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from llm_cli.execution import sandbox
from llm_cli.execution.sandbox import SandboxPolicy, protected_home_paths, wrap

KIND = sandbox.available_sandbox()
needs_sandbox = pytest.mark.skipif(
    KIND is None, reason="no working operating-system sandbox on this machine"
)


def test_seatbelt_profile_denies_by_default_and_passes_paths_as_parameters(
    tmp_path: Path,
) -> None:
    policy = SandboxPolicy(
        writable=(tmp_path / "snap",),
        protected=(tmp_path / 'odd "path"',),
        readable=(tmp_path / 'odd "path"' / "dep",),
    )

    argv = wrap(sandbox.SEATBELT, policy, ["pytest", "-q"], cwd=tmp_path)

    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    profile = argv[2]
    assert profile.startswith("(version 1)\n(deny default)\n")
    # Paths never appear in profile syntax; they travel as -D parameters.
    assert '"path"' not in profile
    assert f"PROTECTED_0={tmp_path / 'odd "path"'}" in argv
    deny = profile.index("(deny file-read* ")
    exception = profile.index('(allow file-read* (subpath (param "READABLE_0")))')
    writable = profile.index("(allow file-read* file-write* ")
    assert deny < exception < writable
    assert "(allow network-outbound (remote unix-socket" in profile
    assert "network-outbound (remote ip" not in profile
    assert argv[-3:] == ["--", "pytest", "-q"]


def test_bubblewrap_masks_protected_paths_before_exceptions_and_writes(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "secret"
    dependency = protected / "dep"
    dependency.mkdir(parents=True)
    secret_file = tmp_path / "token"
    secret_file.write_text("x")
    snapshot = tmp_path / "snap"
    policy = SandboxPolicy(
        writable=(snapshot,),
        protected=(protected, secret_file, tmp_path / "missing"),
        readable=(dependency,),
    )

    argv = wrap(sandbox.BUBBLEWRAP, policy, ["make", "test"], cwd=snapshot)

    assert argv[0] == "bwrap"
    assert "--unshare-all" in argv and "--die-with-parent" in argv
    assert argv[argv.index("--ro-bind") : argv.index("--ro-bind") + 3] == [
        "--ro-bind",
        "/",
        "/",
    ]
    joined = " ".join(argv)
    assert "--tmpfs /tmp" in joined and "--tmpfs /run" in joined
    assert f"--tmpfs {protected}" in joined
    assert f"--ro-bind /dev/null {secret_file}" in joined
    assert str(tmp_path / "missing") not in joined
    masked = joined.index(f"--tmpfs {protected}")
    exposed = joined.index(f"--ro-bind {dependency} {dependency}")
    bound = joined.index(f"--bind {snapshot} {snapshot}")
    assert masked < exposed < bound
    assert argv[-5:] == ["--chdir", str(snapshot), "--", "make", "test"]


def test_unknown_sandbox_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported sandbox"):
        wrap("none", SandboxPolicy(writable=()), ["true"], cwd=tmp_path)


def test_home_secret_paths_cover_common_credential_stores() -> None:
    paths = protected_home_paths(Path("/home/user"))

    for relative in (".ssh", ".aws", ".config/gh", ".codex", "Library/Keychains"):
        assert Path("/home/user") / relative in paths


def test_platform_selects_its_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    assert sandbox.sandbox_kind() is None
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    assert sandbox.sandbox_kind() == sandbox.BUBBLEWRAP
    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    assert sandbox.sandbox_kind() is None


def test_a_sandbox_that_cannot_start_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "sandbox_kind", lambda: sandbox.BUBBLEWRAP)
    monkeypatch.setattr(sandbox, "_probe_result", {})
    calls: list[str] = []

    def failing_probe(kind: str) -> bool:
        calls.append(kind)
        return False

    monkeypatch.setattr(sandbox, "_probe", failing_probe)

    assert sandbox.available_sandbox() is None
    assert sandbox.available_sandbox() is None
    assert calls == [sandbox.BUBBLEWRAP]


def _run(
    policy: SandboxPolicy, argv: list[str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    assert KIND is not None
    return subprocess.run(
        wrap(KIND, policy, argv, cwd=cwd),
        cwd=cwd,
        env={"PATH": os.environ.get("PATH", os.defpath), "HOME": str(cwd)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path.resolve()
    paths = {
        "snapshot": root / "snapshot",
        "secret": root / "secret",
        "dependency": root / "secret" / "dependency",
        "outside": root / "outside",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    (paths["secret"] / "key.txt").write_text("private\n")
    (paths["dependency"] / "lib.txt").write_text("library\n")
    return paths


def _policy(layout: dict[str, Path]) -> SandboxPolicy:
    return SandboxPolicy(
        writable=(layout["snapshot"],),
        protected=(layout["secret"],),
        readable=(layout["dependency"],),
    )


@needs_sandbox
def test_real_sandbox_limits_writes_to_writable_paths(
    layout: dict[str, Path],
) -> None:
    snapshot, outside = layout["snapshot"], layout["outside"]

    inside = _run(_policy(layout), ["sh", "-c", "echo ok > made.txt"], snapshot)
    blocked = _run(
        _policy(layout), ["sh", "-c", f"echo no > {outside / 'made.txt'}"], snapshot
    )

    assert inside.returncode == 0 and (snapshot / "made.txt").exists()
    assert blocked.returncode != 0 and not (outside / "made.txt").exists()


@needs_sandbox
def test_real_sandbox_hides_protected_paths_except_readable_ones(
    layout: dict[str, Path],
) -> None:
    secret = _run(
        _policy(layout), ["cat", str(layout["secret"] / "key.txt")], layout["snapshot"]
    )
    dependency = _run(
        _policy(layout),
        ["cat", str(layout["dependency"] / "lib.txt")],
        layout["snapshot"],
    )

    assert secret.returncode != 0 and "private" not in secret.stdout
    assert dependency.returncode == 0 and dependency.stdout == "library\n"


@needs_sandbox
def test_real_sandbox_has_no_network(layout: dict[str, Path]) -> None:
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "except OSError:\n"
        "    print('blocked')\n"
        "else:\n"
        "    print('connected')\n"
    )

    result = _run(_policy(layout), [sys.executable, "-c", code], layout["snapshot"])

    assert result.stdout.strip() == "blocked", result.stderr


@needs_sandbox
def test_real_sandbox_allows_unix_sockets_only_in_writable_paths(
    layout: dict[str, Path], tmp_path: Path
) -> None:
    code = (
        "import socket\n"
        "server = socket.socket(socket.AF_UNIX)\n"
        "server.bind('s')\n"
        "server.listen(1)\n"
        "client = socket.socket(socket.AF_UNIX)\n"
        "client.connect('s')\n"
        "print('connected')\n"
    )
    inside = _run(_policy(layout), [sys.executable, "-c", code], layout["snapshot"])
    assert inside.stdout.strip() == "connected", inside.stderr

    # A host socket outside the sandbox's paths, like an SSH agent.
    host = socket.socket(socket.AF_UNIX)
    path = Path("/tmp") / f"loupe-test-{os.getpid()}.sock"
    path.unlink(missing_ok=True)
    host.bind(str(path))
    host.listen(1)
    try:
        reach = (
            "import socket, sys\n"
            "client = socket.socket(socket.AF_UNIX)\n"
            "try:\n"
            f"    client.connect({str(path)!r})\n"
            "except OSError:\n"
            "    print('blocked')\n"
            "else:\n"
            "    print('connected')\n"
        )
        outside = _run(
            _policy(layout), [sys.executable, "-c", reach], layout["snapshot"]
        )
    finally:
        host.close()
        path.unlink(missing_ok=True)
    assert outside.stdout.strip() == "blocked", outside.stderr
