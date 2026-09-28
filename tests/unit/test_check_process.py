"""Check supervision tolerates exited process groups without hiding live errors."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from llm_cli.execution import check_process


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin zombie group signaling")
def test_owner_shutdown_survives_a_zombie_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        start_new_session=True,
        env={"PATH": os.defpath},
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["ps", "-p", str(child.pid), "-o", "stat="],
                env={"PATH": os.defpath},
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            if state.startswith("Z"):
                break
            time.sleep(0.01)
        assert state.startswith("Z"), state
        # Keep the exited child unreaped to exercise Darwin's actual EPERM.
        with pytest.raises(PermissionError):
            os.killpg(child.pid, signal.SIGTERM)

        first_poll = True

        def poll() -> int | None:
            nonlocal first_poll
            if first_poll:
                first_poll = False
                # The child exits just after the supervisor observes it alive.
                return None
            return child.poll()

        proxy = Mock(pid=child.pid, poll=poll, wait=child.wait)
        selector = Mock()
        selector.select.return_value = [(0, 1)]
        original_read = os.read
        monkeypatch.setattr(check_process.subprocess, "Popen", lambda *a, **kw: proxy)
        monkeypatch.setattr(
            check_process.selectors, "DefaultSelector", lambda: selector
        )
        monkeypatch.setattr(check_process.sys, "stdin", Mock(fileno=lambda: 0))
        monkeypatch.setattr(
            check_process.os,
            "read",
            lambda fd, size: b"" if fd == 0 else original_read(fd, size),
        )
        assert check_process.main() == 125
        assert child.wait(timeout=5) == 0
    finally:
        child.wait(timeout=5)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_persistent_group_permission_error_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    child = Mock(pid=123, poll=Mock(return_value=None))
    clock = Mock(return_value=0.0)
    monkeypatch.setattr(check_process.sys, "platform", platform)
    monkeypatch.setattr(
        check_process.os, "killpg", Mock(side_effect=PermissionError("cannot signal"))
    )
    monkeypatch.setattr(check_process.time, "monotonic", clock)
    monkeypatch.setattr(
        check_process.time, "sleep", lambda _: setattr(clock, "return_value", 2.0)
    )
    with pytest.raises(PermissionError, match="cannot signal"):
        check_process._signal_group(child, signal.SIGKILL)
