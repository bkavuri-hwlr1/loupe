"""Subprocess supervisor: stdin EOF means its owning daemon is gone.

This file is executed directly by the configured Python interpreter. It never
imports application state or credentials. Descendants stay in a process group
owned by this supervisor, including when the direct child exits first.

A descendant can leave that group, as a daemon started with setsid() does.
On Linux the supervisor is a child subreaper: such a process becomes its child
once its own parent exits, and is stopped when the command ends. Under the
macOS sandbox, ``--sandbox-reaper`` passes descriptors 3 and 4 to the reaper
that ``sandbox.py`` starts inside the sandbox. Closing 4 asks it to kill every
process in that sandbox, and 3 closes when it has.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import selectors
import signal
import subprocess
import sys
import time

_REAPER_FLAG = "--sandbox-reaper"
# PR_SET_CHILD_SUBREAPER from <linux/prctl.h>.
_PR_SET_CHILD_SUBREAPER = 36


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


def _become_subreaper() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    return bool(libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0)


def _reap_orphans(child_pid: int) -> None:
    """Reap adopted descendants that exited, leaving the command's own status."""

    if not sys.platform.startswith("linux"):
        return
    while True:
        try:
            info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return
        if info is None or info.si_pid == child_pid:
            return
        with contextlib.suppress(ChildProcessError):
            os.waitpid(info.si_pid, 0)


def _children() -> list[int]:
    """This process's live children, from /proc."""

    me, found = os.getpid(), []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                stat = handle.read()
        except OSError:
            continue
        # The command name may contain spaces and parentheses; later fields
        # cannot. The parent's ID is the second field after it.
        fields = stat[stat.rfind(b")") + 1 :].split()
        if len(fields) > 1 and int(fields[1]) == me:
            found.append(int(entry))
    return found


def _stop_descendants() -> None:
    """Kill and reap every process this subreaper adopted, within a bound."""

    deadline = time.monotonic() + 5
    while True:
        # A child's ID cannot be reused before it is reaped, so this never
        # signals an unrelated process.
        for pid in _children():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        if time.monotonic() >= deadline:
            return
        # Killed children's own children are adopted next.
        time.sleep(0.01)


def _reaper_pipes() -> tuple[int, int]:
    """Put the reaper's ends on descriptors 3 and 4; return the supervisor's."""

    alive_read, alive_write = os.pipe()
    trigger_read, trigger_write = os.pipe()
    # Move every end clear of 3 and 4 first, so placing the reaper's ends
    # cannot overwrite another one.
    ends = [
        fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
        for fd in (alive_read, alive_write, trigger_read, trigger_write)
    ]
    for fd in (alive_read, alive_write, trigger_read, trigger_write):
        os.close(fd)
    alive_read, alive_write, trigger_read, trigger_write = ends
    for target, fd in ((3, alive_write), (4, trigger_read)):
        try:
            os.fstat(target)
        except OSError:
            os.dup2(fd, target)
            os.close(fd)
        else:
            raise RuntimeError(f"descriptor {target} is already in use")
    return alive_read, trigger_write


def _run_reaper(alive: int, trigger: int) -> None:
    """Ask the in-sandbox reaper to kill everything, and wait until it has."""

    os.close(trigger)
    selector = selectors.DefaultSelector()
    selector.register(alive, selectors.EVENT_READ)
    deadline = time.monotonic() + 2
    try:
        while (left := deadline - time.monotonic()) > 0:
            if selector.select(left) and not os.read(alive, 1):
                return
    finally:
        selector.close()
        os.close(alive)


def main() -> int:
    argv = sys.argv[1:]
    reaper = argv[:1] == [_REAPER_FLAG]
    if reaper:
        argv = argv[1:]
    subreaper = _become_subreaper()
    alive = trigger = None
    if reaper:
        alive, trigger = _reaper_pipes()
    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=(3, 4) if reaper else (),
        )
    finally:
        if reaper:
            os.close(3)
            os.close(4)
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin, selectors.EVENT_READ)
    stopped = False
    try:
        while child.poll() is None:
            if selector.select(0.1) and not os.read(sys.stdin.fileno(), 1):
                stopped = True
                break
            if subreaper:
                _reap_orphans(child.pid)
    finally:
        selector.close()
        _signal_group(child, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if alive is not None and trigger is not None:
            _run_reaper(alive, trigger)
        _signal_group(child, signal.SIGKILL)
        code = child.wait()
        if subreaper:
            _stop_descendants()
    return 125 if stopped else (code if code >= 0 else 128 - code)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as exc:
        print(f"Could not start check: {exc}", file=sys.stderr)
        raise SystemExit(127) from None
