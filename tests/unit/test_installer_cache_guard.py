"""Configured checks cannot access persistent cache; other access stays intact."""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from llm_cli.execution import sandbox
from llm_cli.execution.sandbox import SandboxPolicy, wrap, wrap_cache_guard


@pytest.fixture
def cache_root(tmp_path: Path) -> Path:
    root = tmp_path / 'private "cache'
    root.mkdir(mode=0o700)
    (root / "artifact").write_text("cached artifact bytes")
    return root.resolve()


def test_cache_seatbelt_executes_argv_directly_without_general_reaper(
    tmp_path: Path, cache_root: Path
) -> None:
    command = wrap_cache_guard(
        sandbox.SEATBELT, cache_root, ["echo", "ready"], cwd=tmp_path
    )
    assert command[:2] == ["/usr/bin/sandbox-exec", "-p"]
    profile = command[2]
    assert profile.startswith("(version 1)\n(allow default)\n")
    assert '(deny file-read* file-write* (subpath (param "CACHE_ROOT")))' in profile
    assert "(deny network-outbound (remote unix-socket" in profile
    assert str(cache_root) not in profile
    assert f"CACHE_ROOT={cache_root}" in command
    assert command[-3:] == ["--", "echo", "ready"]
    assert sandbox._SEATBELT_LAUNCHER not in command
    assert "loupe-sandbox" not in command


def test_cache_bubblewrap_preserves_network_and_writes_beyond_mask(
    tmp_path: Path, cache_root: Path
) -> None:
    command = wrap_cache_guard(
        sandbox.BUBBLEWRAP, cache_root, ["echo", "ready"], cwd=tmp_path
    )
    assert command[command.index("--dev-bind") : command.index("--dev-bind") + 3] == [
        "--dev-bind", "/", "/"
    ]
    assert "--unshare-net" not in command and "--unshare-all" not in command
    assert "--unshare-pid" in command
    assert command.count("--tmpfs") == 1
    assert command[command.index("--tmpfs") + 1] == str(cache_root)
    assert command[command.index("--remount-ro") + 1] == str(cache_root)
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[-5:] == ["--chdir", str(tmp_path), "--", "echo", "ready"]


@pytest.mark.parametrize("unsafe", ["missing", "file", "symlink", "public"])
def test_cache_guard_refuses_an_unverified_root(tmp_path: Path, unsafe: str) -> None:
    root = tmp_path / "cache"
    if unsafe == "file":
        root.write_text("file")
    elif unsafe == "symlink":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
    elif unsafe == "public":
        root.mkdir(mode=0o755)
        root.chmod(0o755)
    with pytest.raises(ValueError, match="cache guard"):
        wrap_cache_guard(sandbox.SEATBELT, root, ["true"], cwd=tmp_path)


def test_cache_guard_refuses_root_replacement_during_resolution(
    tmp_path: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.resolve
    changed = False

    def replaced(path: Path, *args: object, **kwargs: object) -> Path:
        nonlocal changed
        if path == cache_root and not changed:
            changed = True
            path.rename(tmp_path / "former-cache")
            path.mkdir(mode=0o700)
        return original(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", replaced)
    with pytest.raises(ValueError, match="stable private root"):
        wrap_cache_guard(sandbox.SEATBELT, cache_root, ["true"], cwd=tmp_path)


def test_general_model_command_policy_keeps_its_existing_reaper(
    tmp_path: Path,
) -> None:
    command = wrap(
        sandbox.SEATBELT, SandboxPolicy(writable=(tmp_path,)), ["true"], cwd=tmp_path
    )
    assert command[2].startswith("(version 1)\n(deny default)\n")
    assert sandbox._SEATBELT_LAUNCHER in command


@pytest.mark.parametrize("alias", [False, True])
def test_real_cache_guard_denies_cache_and_preserves_outside_write_and_tcp(
    tmp_path: Path, cache_root: Path, alias: bool
) -> None:
    kind = sandbox.available_sandbox()
    if kind is None:
        pytest.skip("working command sandbox required")
    accessed = cache_root
    if alias:
        link = tmp_path / "parent-alias"
        link.symlink_to(cache_root.parent, target_is_directory=True)
        accessed = link / cache_root.name
    outside = tmp_path / "outside-write"
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(5)
        port = server.getsockname()[1]
        script = """from pathlib import Path
import socket,sys
root=Path(sys.argv[1])
actions=(
    lambda: (root/'artifact').read_bytes(),
    lambda: (root/'new').write_text('forbidden'),
)
for action in actions:
    try:
        action()
    except OSError:
        pass
    else:
        raise AssertionError('cache access allowed')
Path(sys.argv[2]).write_text('legacy write preserved')
sibling=Path(sys.argv[2]).with_name('ordinary-sibling')
sibling.mkdir()
sibling.rename(sibling.with_name('renamed-sibling'))
with socket.create_connection(('127.0.0.1',int(sys.argv[3])),timeout=2) as connected:
    connected.sendall(b'legacy network preserved')
"""
        completed = subprocess.run(
            wrap_cache_guard(
                kind,
                cache_root,
                [
                    sys.executable, "-I", "-c", script,
                    str(accessed), str(outside), str(port),
                ],
                cwd=tmp_path,
            ),
            cwd=tmp_path,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        with server.accept()[0] as connected:
            assert connected.recv(128) == b"legacy network preserved"
    assert outside.read_text() == "legacy write preserved"
    assert (cache_root / "artifact").read_text() == "cached artifact bytes"
    assert not (cache_root / "new").exists()


def test_real_cache_guard_denies_cache_unix_socket_connections() -> None:
    kind = sandbox.available_sandbox()
    if kind is None:
        pytest.skip("working command sandbox required")
    directory = "/private/tmp" if sys.platform == "darwin" else "/tmp"
    with tempfile.TemporaryDirectory(prefix="loupe-cg-", dir=directory) as temporary:
        root = Path(temporary).resolve()
        cache = root / "cache"
        cache.mkdir(mode=0o700)
        path = cache / "service.sock"
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
            server.listen()
            script = """import socket,sys
with socket.socket(socket.AF_UNIX) as connection:
    try:
        connection.connect(sys.argv[1])
    except OSError:
        pass
    else:
        raise AssertionError('cache socket access allowed')
"""
            completed = subprocess.run(
                wrap_cache_guard(
                    kind, cache, [sys.executable, "-I", "-c", script, str(path)],
                    cwd=root,
                ),
                cwd=root,
                capture_output=True,
                timeout=10,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr


def test_real_cache_guard_hides_cache_after_ancestor_rename(tmp_path: Path) -> None:
    kind = sandbox.available_sandbox()
    if kind is None:
        pytest.skip("working command sandbox required")
    parent = tmp_path / "profile"
    cache = parent / "cache"
    cache.mkdir(mode=0o700, parents=True)
    (cache / "artifact").write_text("cached artifact bytes")
    renamed = tmp_path / "moved-profile"
    script = """from pathlib import Path
import sys
parent,moved=map(Path,sys.argv[1:])
try:
    parent.rename(moved)
except OSError:
    pass
cache=(moved if moved.exists() else parent)/'cache'
try:
    (cache/'artifact').read_bytes()
except OSError:
    pass
else:
    raise AssertionError('cache read allowed after ancestor rename')
"""
    completed = subprocess.run(
        wrap_cache_guard(
            kind,
            cache,
            [sys.executable, "-I", "-c", script, str(parent), str(renamed)],
            cwd=tmp_path,
        ),
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    actual_parent = renamed if renamed.exists() else parent
    assert (actual_parent / "cache/artifact").read_text() == "cached artifact bytes"


def test_real_cache_guard_refuses_hardlink_alias_creation(tmp_path: Path) -> None:
    import hashlib

    kind = sandbox.available_sandbox()
    if kind is None:
        pytest.skip("working command sandbox required")
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    artifact = cache / "artifact"
    artifact.write_bytes(b"cached artifact bytes")
    before = hashlib.sha256(artifact.read_bytes()).hexdigest()
    alias = tmp_path / "outside-alias"
    script = """import os,sys
try:
    os.link(sys.argv[1],sys.argv[2])
except OSError:
    pass
else:
    with open(sys.argv[2],'rb') as reader:
        reader.read()
    with open(sys.argv[2],'wb') as writer:
        writer.write(b'forbidden alias write')
    raise AssertionError('cache hardlink alias created outside boundary')
"""
    completed = subprocess.run(
        wrap_cache_guard(
            kind,
            cache,
            [sys.executable, "-I", "-c", script, str(artifact), str(alias)],
            cwd=tmp_path,
        ),
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert not alias.exists()
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == before
