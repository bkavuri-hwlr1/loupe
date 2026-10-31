"""Only the trusted supervisor inherits the bounded cache lease descriptors."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from llm_cli.execution.checks import supervised_run
from llm_cli.workspace.installer_cache import DEFAULT_MAX_BYTES, InstallerCache


def test_installer_cannot_inherit_or_unlock_supervisor_cache_descriptors(
    tmp_path: Path,
) -> None:
    store = InstallerCache(tmp_path / "cache", DEFAULT_MAX_BYTES)
    with store.lease(()) as lease:
        assert lease.supervisor_fds and min(lease.supervisor_fds) >= 64
        code = (
            "import os,sys\n"
            "for fd in map(int,sys.argv[1:]):\n"
            " try: os.fstat(fd)\n"
            " except OSError: continue\n"
            " raise SystemExit('installer inherited a supervisor lease')\n"
            "print('lease descriptors private')\n"
        )
        result = supervised_run(
            [sys.executable, "-c", code, *map(str, lease.supervisor_fds)],
            cwd=tmp_path,
            env={},
            deadline=time.monotonic() + 10,
            remaining=4096,
            cancelled=lambda: False,
            on_output=lambda _: None,
            supervisor_fds=lease.supervisor_fds,
        )
        assert result[0] == 0, result
        assert result[1] == "lease descriptors private\n"


@pytest.mark.parametrize("fds", [(3,), (4,), (63,), (True,), (64, 64)])
def test_supervisor_rejects_reserved_low_or_duplicate_descriptors(
    tmp_path: Path, fds: tuple[int, ...]
) -> None:
    with pytest.raises(ValueError, match="descriptor"):
        supervised_run(
            [sys.executable, "-c", "raise SystemExit(99)"],
            cwd=tmp_path,
            env={},
            deadline=time.monotonic() + 5,
            remaining=4096,
            cancelled=lambda: False,
            on_output=lambda _: None,
            supervisor_fds=fds,
        )
