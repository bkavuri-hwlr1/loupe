"""Only explicitly configured installers can opt into outbound networking."""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

from llm_cli.execution import sandbox
from llm_cli.execution.sandbox import SandboxPolicy, wrap


def test_rebuild_network_flag_is_default_deny_for_both_backends(tmp_path: Path) -> None:
    closed = SandboxPolicy(writable=(tmp_path,))
    opened = SandboxPolicy(writable=(tmp_path,), network=True)
    assert (
        "(allow network-outbound (remote ip))"
        not in wrap(sandbox.SEATBELT, closed, ["true"], cwd=tmp_path)[2]
    )
    assert (
        "(allow network-outbound (remote ip))"
        in wrap(sandbox.SEATBELT, opened, ["true"], cwd=tmp_path)[2]
    )
    assert "--share-net" not in wrap(sandbox.BUBBLEWRAP, closed, ["true"], cwd=tmp_path)
    allowed = wrap(sandbox.BUBBLEWRAP, opened, ["true"], cwd=tmp_path)
    assert allowed.index("--unshare-all") < allowed.index("--share-net")


@pytest.mark.parametrize("network", [False, True])
def test_real_sandbox_network_access_matches_explicit_permission(
    tmp_path: Path, network: bool
) -> None:
    kind = sandbox.available_sandbox()
    if kind is None:
        pytest.skip("working sandbox required")
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        address, port = server.getsockname()
        command = [
            sys.executable,
            "-c",
            f"import socket; socket.create_connection(({address!r}, {port}), "
            "timeout=0.5).close()",
        ]
        completed = subprocess.run(
            wrap(
                kind,
                SandboxPolicy(writable=(tmp_path,), network=network),
                command,
                cwd=tmp_path,
            ),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    assert (completed.returncode == 0) == network, completed.stderr
