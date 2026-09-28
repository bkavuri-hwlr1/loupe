"""Slow exclusion metadata cannot keep a search's publication barrier held."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import pytest

_PROBE = r"""
import json
import sys
import time
from pathlib import Path
from llm_cli.agent import bounded_search
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import ToolBroker

bounded_search.SEARCH_TIMEOUT_SECONDS = 0.2
cls = SharedToolBroker if sys.argv[2] == "shared" else ToolBroker
broker = cls(Path(sys.argv[1]), ("*",))
start = time.monotonic()
result = broker.invoke("search_text", {"pattern": "source", "path": sys.argv[3]})
lock_released = True
if sys.argv[2] == "shared":
    # Test from another thread: the search thread owns a reentrant lock.
    import threading
    acquired = []
    def try_lock():
        success = broker.publication_lock.acquire(timeout=0.1)
        acquired.append(success)
        if success:
            broker.publication_lock.release()
    other = threading.Thread(target=try_lock)
    other.start()
    other.join(0.3)
    lock_released = acquired == [True]
print(json.dumps({"error": result.is_error, "elapsed": time.monotonic() - start,
    "content": result.content, "lock_released": lock_released}))
"""


@pytest.mark.parametrize("kind", ["shared", "isolated"])
@pytest.mark.parametrize("path", ["source.txt", "."])
def test_git_exclusion_io_respects_search_deadline(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    kind: str,
    path: str,
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "source\n"})
    exclude = root / ".git/info/exclude"
    exclude.unlink()
    os.mkfifo(exclude)
    with subprocess.Popen(
        [sys.executable, "-I", "-c", _PROBE, str(root), kind, path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=3)
            assert process.returncode == 0, stderr
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    result = json.loads(stdout)
    assert result["error"]
    assert "time limit" in result["content"]
    assert result["elapsed"] < 1.0
    assert result["lock_released"]
