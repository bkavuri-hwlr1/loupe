"""Real local package artifacts rebuild in fresh private trees without networking."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from test_dependency_rebuild import prepare as prepare_installer
from test_isolated_creation_safety import checkout
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.agent.source_policy import contains_secret_material
from llm_cli.config.models import RebuildConfig
from llm_cli.execution.sandbox import available_sandbox
from llm_cli.workspace.installer_cache import InstallerCache

pytestmark = pytest.mark.skipif(
    available_sandbox() is None, reason="real command sandbox required"
)
_PYTHON = str(Path(sys.executable).resolve())


class _IndexHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def package_index(tmp_path: Path):
    root = tmp_path / "index"
    directory = root / "simple" / "cache-fixture"
    directory.mkdir(parents=True)
    wheel_name = "cache_fixture-1.0-py3-none-any.whl"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        files = {
            "cache_fixture.py": b"value = 42\n",
            "cache_fixture-1.0.dist-info/METADATA": (
                b"Metadata-Version: 2.1\nName: cache-fixture\nVersion: 1.0\n"
            ),
            "cache_fixture-1.0.dist-info/WHEEL": (
                b"Wheel-Version: 1.0\nGenerator: scripted\n"
                b"Root-Is-Purelib: true\nTag: py3-none-any\n"
            ),
        }
        for name, body in files.items():
            wheel.writestr(name, body)
        wheel.writestr(
            "cache_fixture-1.0.dist-info/RECORD",
            "".join(name + ",,\n" for name in files)
            + "cache_fixture-1.0.dist-info/RECORD,,\n",
        )
    body = buffer.getvalue()
    (directory / wheel_name).write_bytes(body)
    (directory / "index.html").write_text(
        f'<a href="{wheel_name}#sha256={hashlib.sha256(body).hexdigest()}">'
        f"{wheel_name}</a>"
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_IndexHandler, directory=str(root))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/simple", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_second_open_uses_local_uv_cache_with_network_denied(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    package_index,
) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv package installer required")
    index, server = package_index
    env = batch_env
    (env.repository / "pyproject.toml").write_text("""[project]
name = "cache-rebuild-fixture"
version = "1.0"
requires-python = ">=3.12"
dependencies = ["cache-fixture==1.0"]
""")
    lock = subprocess.run(
        [uv, "lock", "--python", _PYTHON, "--index-url", index],
        cwd=env.repository,
        env={
            **os.environ,
            "UV_CACHE_DIR": str(tmp_path / "lock-only-cache"),
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_NO_CONFIG": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert lock.returncode == 0, lock.stderr
    isolate = prepare_installer(env, tmp_path)
    isolate.rebuild_cache = True
    argv = (uv, "sync", "--frozen", "--python", _PYTHON)
    isolate.rebuild = (RebuildConfig(".venv", argv, True),)
    first, notices = isolate.open(checkout(env), "cache-first")
    assert notices == ("Dependency rebuilt privately: .venv",), notices
    assert (Path(first.canonical_path) / ".venv").is_dir()
    # Stop the only index before a second open and explicitly deny networking.
    server.shutdown()
    isolate.rebuild = (RebuildConfig(".venv", (*argv, "--offline"), False),)
    second, notices = isolate.open(checkout(env), "cache-second")
    assert notices == ("Dependency rebuilt privately: .venv",), notices
    private = Path(second.canonical_path)
    assert private != Path(first.canonical_path)
    output = subprocess.run(
        [
            str(private / ".venv/bin/python"),
            "-c",
            "import cache_fixture; print(cache_fixture.value)",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert output.stdout == "42\n"
    assert all(
        path.stat().st_nlink == 1
        for path in (private / ".venv").rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def test_two_concurrent_private_installs_share_the_cache_lease(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    isolate = prepare_installer(
        batch_env,
        tmp_path,
        """from pathlib import Path
import os,time
cache = Path(os.environ['XDG_CACHE_HOME'])
(cache / 'shared-artifact').write_text('verified artifact')
time.sleep(.15)
Path('.venv/installed').write_text('independent dependency')
""",
    )
    isolate.rebuild_cache = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            pool.submit(isolate.open, checkout(batch_env), name)
            for name in ("cache-concurrent-one", "cache-concurrent-two")
        ]
        opened = [result.result(timeout=30) for result in results]
    roots = [Path(workspace.canonical_path) for workspace, notices in opened]
    assert roots[0] != roots[1]
    assert all(
        (root / ".venv/installed").read_text() == "independent dependency"
        for root in roots
    )
    assert all(
        notices == ("Dependency rebuilt privately: .venv",) for _, notices in opened
    )
    store = InstallerCache(tmp_path / "installer-cache", 512 * 1024**2)
    assert store.prune()


def test_rotated_non_signature_private_input_cannot_return_from_old_cache(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    old, new = "ordinaryCachedMarkerR7x2p8k4", "rotatedCachedMarkerH9v3d5s1"
    assert not contains_secret_material(old) and not contains_secret_material(new)
    script = """from pathlib import Path
import os
cache = Path(os.environ['XDG_CACHE_HOME'])
value = Path('.env').read_text().partition('=')[2]
marker = cache / 'private-artifact'
if value.startswith('ordinary'):
    marker.write_text(value)
    Path('.venv/installed').write_text('ready')
else:
    print(marker.read_text() if marker.exists() else 'old cache namespace unavailable')
    raise SystemExit(7)
"""
    isolate = prepare_installer(batch_env, tmp_path, script)
    isolate.rebuild_cache = True
    (batch_env.repository / ".env").write_text("MARKER=" + old)
    first, _ = isolate.open(
        checkout(batch_env), "cache-private-first", approved_secrets=(".env",)
    )
    (batch_env.repository / ".env").write_text("MARKER=" + new)
    second, notices = isolate.open(
        checkout(batch_env), "cache-private-second", approved_secrets=(".env",)
    )
    assert not (Path(second.canonical_path) / ".venv").exists()
    assert "old cache namespace unavailable" in "\n".join(notices)
    assert old not in repr(notices) and new not in repr(notices)
    with batch_env.store.connection() as connection:
        database = "\n".join(connection.iterdump())
    assert old not in database and new not in database
    for workspace in (first, second):
        journal = Path(workspace.git_dir) / "loupe-rebuild.json"
        assert old not in journal.read_text() and new not in journal.read_text()
    for path in batch_env.publisher.candidate_dir.iterdir():
        if path.is_file():
            assert old.encode() not in path.read_bytes()
            assert new.encode() not in path.read_bytes()
