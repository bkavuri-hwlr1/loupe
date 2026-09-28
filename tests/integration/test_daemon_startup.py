"""Daemon autostart must not import Python from the repository or its environment."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from llm_cli.paths import AppPaths
from llm_cli.protocol import client as client_module
from llm_cli.protocol.client import DaemonClient


@pytest.mark.parametrize("injection", ["working_directory", "pythonpath"])
@pytest.mark.parametrize("relative_paths", [False, True], ids=["absolute", "relative"])
def test_autostart_ignores_untrusted_python_import_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    injection: str,
    relative_paths: bool,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    injected = repository if injection == "working_directory" else tmp_path / "python"
    injected.mkdir(exist_ok=True)
    marker = tmp_path / "untrusted-code-ran"
    payload = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('untrusted startup')\n"
        "raise SystemExit(77)\n"
    )
    package = injected / "llm_cli"
    package.mkdir()
    (package / "__init__.py").write_text(payload)
    if injection == "pythonpath":
        # Startup hooks run before -m resolves the package unless isolated mode
        # also prevents the inherited environment from extending sys.path.
        (injected / "sitecustomize.py").write_text(payload)
        monkeypatch.setenv("PYTHONPATH", str(injected))
    else:
        monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.chdir(repository)

    children: list[subprocess.Popen[bytes]] = []
    original_popen = subprocess.Popen

    def start(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(client_module.subprocess, "Popen", start)
    # Keep the Unix socket below the platform's short pathname limit.
    with tempfile.TemporaryDirectory(prefix="lc-start-", dir="/tmp") as runtime:
        for name, path in {
            "LLM_COORD_CONFIG_HOME": tmp_path / "config",
            "LLM_COORD_DATA_HOME": tmp_path / "data",
            "LLM_COORD_STATE_HOME": tmp_path / "state",
            "LLM_COORD_RUNTIME_DIR": Path(runtime).resolve(),
        }.items():
            monkeypatch.setenv(
                name, os.path.relpath(path, repository) if relative_paths else str(path)
            )
        paths = AppPaths.resolve("startup-test")
        client = DaemonClient(paths, timeout_seconds=3)
        try:
            client._start_and_wait()
            assert not marker.exists(), "daemon startup executed untrusted Python"
            assert len(children) == 1
            assert children[0].poll() is None, paths.log_file.read_text()
            assert client.call("system.ping", autostart=False)
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=10)
