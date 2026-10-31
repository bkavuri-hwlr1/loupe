"""A real daemon's death cannot turn a still-writing cache into an idle cache."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from authority_helpers import __file__ as authority_bootstrap
from test_rebuild_responsiveness import _private_path

from llm_cli.errors import LlmCoordError
from llm_cli.execution.sandbox import available_sandbox
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient
from llm_cli.workspace.installer_cache import DEFAULT_MAX_BYTES, InstallerCache

pytestmark = pytest.mark.skipif(
    available_sandbox() is None, reason="real command sandbox required"
)
_PYTHON = str(Path(sys.executable).resolve())


@contextlib.contextmanager
def _daemon(tmp_path: Path) -> Iterator[tuple[AppPaths, subprocess.Popen[bytes]]]:
    with TemporaryDirectory(prefix="lc-cache-rpc-", dir="/tmp") as runtime:
        environment = {
            **os.environ,
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(Path(runtime).resolve()),
            "PYTHONUNBUFFERED": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        paths = AppPaths.resolve("cache-lifecycle", environ=environment)
        paths.config_file.parent.mkdir(parents=True)
        paths.config_file.write_text(
            '[isolated.rebuild]\ncache = true\n".venv" = { argv = '
            + json.dumps([_PYTHON, "install.py"])
            + ", timeout_seconds = 20 }\n"
        )
        log = tmp_path / "daemon-output.log"
        with log.open("wb") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(authority_bootstrap),
                    "--profile",
                    paths.profile_id,
                ],
                cwd=tmp_path,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            deadline = time.monotonic() + 30
            while not paths.socket.exists():
                assert process.poll() is None, log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.02)
            yield paths, process
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def test_daemon_sigkill_keeps_prune_busy_until_term_ignoring_installer_is_stopped(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    repository = repository_factory(
        tmp_path,
        {
            "a.txt": "source\n",
            ".gitignore": ".venv/\n",
            "install.py": """from pathlib import Path
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
cache = Path(os.environ['XDG_CACHE_HOME'])
Path('.venv/started').write_text(str(os.getpid()))
while True:
    with (cache / 'heartbeat').open('ab') as output:
        output.write(b'x')
    time.sleep(.01)
""",
        },
    )
    with _daemon(tmp_path) as (paths, daemon):
        client = DaemonClient(paths, timeout_seconds=30)
        client.call("repo.add", {"path": str(repository)}, autostart=False)
        with ThreadPoolExecutor(max_workers=1) as pool:
            opening = pool.submit(
                client.call,
                "session.open",
                {
                    "path": str(repository),
                    "session_id": "death-lease",
                    "resume_token_hash": hashlib.sha256(b"death-lease").hexdigest(),
                    "workspace": "isolated",
                },
                autostart=False,
            )
            deadline = time.monotonic() + 15
            while True:
                private = _private_path(paths, "death-lease")
                if private is not None and (private / ".venv/started").exists():
                    break
                assert not opening.done(), opening.result()
                assert time.monotonic() < deadline, "installer never started"
                time.sleep(0.01)
            root = paths.data_dir / "installer-cache"
            while not (beats := list(root.glob("ns-*/xdg/heartbeat"))):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            heartbeat = beats[0]
            store = InstallerCache(root, DEFAULT_MAX_BYTES)
            daemon.kill()
            daemon.wait(timeout=10)
            try:
                # This is a separate open-file description after daemon death.
                # The supervisor's inherited lease must still exclude pruning.
                before = heartbeat.stat().st_size
                assert not store.prune(all_artifacts=True)
                deadline = time.monotonic() + 1
                while heartbeat.stat().st_size == before:
                    assert time.monotonic() < deadline, "no live post-death writer"
                    time.sleep(0.01)
                assert not store.prune(all_artifacts=True)
                deadline = time.monotonic() + 10
                while not store.prune(all_artifacts=True):
                    assert heartbeat.exists(), "cache removed under a live writer"
                    assert time.monotonic() < deadline, "writer did not stop"
                    time.sleep(0.02)
                assert not heartbeat.exists()
                with pytest.raises(LlmCoordError):
                    opening.result(timeout=10)
            finally:
                # Do not leave a writer behind if an assertion fails. The real
                # supervisor owns teardown; waiting is sufficient and avoids
                # ever signalling a possibly reused installer PID ourselves.
                limit = time.monotonic() + 10
                while not store.prune(all_artifacts=True) and time.monotonic() < limit:
                    time.sleep(0.02)
