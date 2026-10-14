"""Match untrusted Python regexes outside the daemon, with one search deadline."""

from __future__ import annotations

import atexit
import json
import os
import queue
import selectors
import subprocess
import sys
import time
from _thread import RLock
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

SEARCH_TIMEOUT_SECONDS = 5.0
_POLL_SECONDS = 0.05

# -I ignores both cwd and PYTHONPATH. The worker imports only the standard
# library, so neither a repository module nor editable-install resolution is
# needed. Compilation belongs here too: deeply nested patterns can be costly.
_WORKER = r"""
import json
import re
import sys

def match(request):
    try:
        expression = re.compile(request["pattern"])
    except (re.error, RecursionError, OverflowError):
        return {"error": "invalid regular expression"}
    matches = []
    seen = 0
    for relative, content in request["files"]:
        for number, line in enumerate(content.splitlines(), start=1):
            if expression.search(line):
                if seen >= request["offset"]:
                    # Keep one extra result to preserve continuation semantics.
                    stripped = line.strip()
                    matches.append([relative, number, stripped[:201]])
                    if len(matches) > request["limit"]:
                        return {"matches": matches}
                seen += 1
    return {"matches": matches}

for line in sys.stdin:
    try:
        print(json.dumps(match(json.loads(line))), flush=True)
    finally:
        # Reuse the process, never its prior source payload or regex cache.
        del line
        re.purge()

"""


class SearchError(RuntimeError):
    """A bounded search cannot return trustworthy results."""


@dataclass(slots=True)
class SearchBudget:
    """One deadline shared by snapshot acquisition, scanning and matching."""

    check_cancelled: Callable[[], None]
    deadline: float = field(
        default_factory=lambda: time.monotonic() + SEARCH_TIMEOUT_SECONDS
    )

    def remaining(self) -> float:
        self.check_cancelled()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SearchError(
                "search exceeded its time limit; simplify the pattern or narrow path"
            )
        return remaining

    @contextmanager
    def lock(self, lock: RLock) -> Iterator[None]:
        while not lock.acquire(timeout=min(_POLL_SECONDS, self.remaining())):
            pass
        try:
            self.remaining()
            yield
        finally:
            lock.release()


@dataclass(slots=True)
class SearchSnapshot:
    files: list[tuple[str, str]]
    fingerprint: str
    stop_reason: str | None = None
    # Files skipped because they contain recognized secret material.
    withheld: int = 0


# A fixed pool bounds idle children across brokers and shares them with batched
# reads. Slots are exclusive for an entire request; waiting uses its deadline.
_WORKER_COUNT = 4
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class SearchWorker:
    """One persistent isolated matcher, replaced after any interrupted request."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None

    def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()

    def match(self, payload: bytes, budget: SearchBudget) -> dict[str, Any]:
        try:
            if self.process is None or self.process.poll() is not None:
                self.close()
                self.process = subprocess.Popen(
                    [sys.executable, "-I", "-S", "-c", _WORKER],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
            process = self.process
            assert process.stdin is not None and process.stdout is not None
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            sent = 0
            response = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdin, selectors.EVENT_WRITE)
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    for key, _ in selector.select(
                        min(_POLL_SECONDS, budget.remaining())
                    ):
                        if key.fileobj is process.stdin:
                            try:
                                sent += os.write(key.fd, payload[sent : sent + 65536])
                            except BlockingIOError:
                                continue
                            if sent == len(payload):
                                selector.unregister(process.stdin)
                        else:
                            try:
                                block = os.read(key.fd, 65536)
                            except BlockingIOError:
                                continue
                            if not block:
                                raise SearchError("search worker stopped unexpectedly")
                            response.extend(block)
                            if len(response) > _MAX_RESPONSE_BYTES:
                                raise SearchError(
                                    "search worker response exceeded its limit"
                                )
                            if response.endswith(b"\n"):
                                budget.remaining()
                                result: dict[str, Any] = json.loads(response)
                                return result
                    if process.poll() is not None:
                        raise SearchError(
                            "search worker could not complete that pattern"
                        )
        except BaseException:
            # Cancellation can be a TaskCancelled or KeyboardInterrupt. Never
            # reuse a child with an unfinished request or leave it running.
            self.close()
            raise


class _SearchPool:
    def __init__(self) -> None:
        self.workers = [SearchWorker() for _ in range(_WORKER_COUNT)]
        self.available: queue.Queue[SearchWorker] = queue.Queue()
        for worker in self.workers:
            self.available.put(worker)

    @contextmanager
    def lease(self, budget: SearchBudget) -> Iterator[SearchWorker]:
        while True:
            try:
                worker = self.available.get(
                    timeout=min(_POLL_SECONDS, budget.remaining())
                )
                break
            except queue.Empty:
                pass
        try:
            budget.remaining()
            yield worker
        finally:
            self.available.put(worker)

    def close(self) -> None:
        for worker in self.workers:
            worker.close()


_POOL = _SearchPool()


def _close_pool() -> None:
    _POOL.close()


def _after_fork() -> None:
    global _POOL
    # These descriptors and PIDs belong to the parent. The child must neither
    # send requests through its pipes nor kill/reap the parent's workers.
    for worker in _POOL.workers:
        process = worker.process
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            worker.process = None
    _POOL = _SearchPool()


atexit.register(_close_pool)
os.register_at_fork(after_in_child=_after_fork)


def find_matches(
    pattern: str,
    files: list[tuple[str, str]],
    *,
    offset: int,
    limit: int,
    budget: SearchBudget,
) -> list[tuple[str, int, str]]:
    """Return a page plus one match through the bounded persistent pool."""

    budget.remaining()
    payload = (
        json.dumps(
            {"pattern": pattern, "files": files, "offset": offset, "limit": limit}
        )
        + "\n"
    ).encode("utf-8")
    try:
        with _POOL.lease(budget) as worker:
            result = worker.match(payload, budget)
    except (OSError, ValueError) as exc:
        raise SearchError("search worker could not complete that pattern") from exc
    if "error" in result:
        raise SearchError("that is not a valid regular expression")
    return [(item[0], item[1], item[2]) for item in result["matches"]]
