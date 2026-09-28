"""Exercise physical Ctrl+C bytes through a CLI process's controlling terminal."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import subprocess
import sys
import termios

import pytest
from test_cli_process_orchestration import _Cluster, _wait
from test_cli_process_orchestration import cluster as cluster


def _terminal_entry() -> None:
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    from llm_cli.cli.app import main

    main()


@pytest.mark.parametrize(
    ("active", "batched"),
    [(False, False), (True, False), (True, True)],
    ids=["idle", "running-task", "rapid-keys"],
)
def test_ctrl_c_twice_exits_real_cli_and_keeps_active_work(
    cluster: _Cluster, active: bool, batched: bool
) -> None:
    master, slave = pty.openpty()
    output = cluster.root / "terminal.log"
    with output.open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from test_cli_interrupts import _terminal_entry; _terminal_entry()",
                "--profile",
                "test",
                "chat",
                "--mode",
                "auto",
                "--plain",
                "--repo",
                str(cluster.repository),
                "--provider",
                "process-test",
                "--model",
                "terminal",
                "--scope",
                "docs/",
            ],
            env=cluster.env,
            cwd=cluster.repository,
            stdin=slave,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
    cluster.processes.append(process)
    os.close(slave)
    try:
        _wait(lambda: "  > " in output.read_text())
        if active:
            request = {"label": "a", "files": {"docs/a.md": "survived CLI exit\n"}}
            os.write(master, ("ORCHESTRATION " + json.dumps(request) + "\n").encode())
            cluster.ready("a")
            _wait(lambda: "Getting ready" in output.read_text())
            assert "staged docs/a.md" not in output.read_text()
        if batched:
            os.write(master, b"\x03\x03")
        else:
            os.write(master, b"\x03")
            _wait(lambda: "Ctrl+C again" in output.read_text())
            assert process.poll() is None
            os.write(master, b"\x03")
        assert process.wait(timeout=5) == 0, output.read_text()
        assert "Leaving Loupe" in output.read_text()
        assert "Traceback" not in output.read_text()
        if active:
            assert "--resume" in output.read_text()
            assert cluster.task("a")["state"] not in {"failed", "cancelled"}
            cluster.release("a")
            assert cluster.settled("a")["state"] == "completed"
            assert (
                cluster.repository / "docs/a.md"
            ).read_text() == "survived CLI exit\n"
    finally:
        os.close(master)
