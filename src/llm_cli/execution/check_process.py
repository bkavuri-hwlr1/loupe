"""Subprocess supervisor: stdin EOF means its owning daemon is gone.

This file is executed directly by the configured Python interpreter. It never
imports application state or credentials. Descendants stay in a process group
owned by this supervisor, including when the direct child exits first.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time


def _signal_group(child: subprocess.Popen[bytes], signum: int) -> None:
    deadline = time.monotonic() + 1
    while True:
        try:
            os.killpg(child.pid, signum)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            if sys.platform != "darwin":
                raise
            # Darwin can return EPERM for a group containing only zombies.
            # Reap our child and allow orphaned descendants to be reaped too.
            child.poll()
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def main() -> int:
    child = subprocess.Popen(
        sys.argv[1:], stdin=subprocess.DEVNULL, start_new_session=True
    )
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin, selectors.EVENT_READ)
    stopped = False
    try:
        while child.poll() is None:
            if selector.select(0.1) and not os.read(sys.stdin.fileno(), 1):
                stopped = True
                break
    finally:
        selector.close()
        _signal_group(child, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        _signal_group(child, signal.SIGKILL)
        code = child.wait()
    return 125 if stopped else (code if code >= 0 else 128 - code)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as exc:
        print(f"Could not start check: {exc}", file=sys.stderr)
        raise SystemExit(127) from None
