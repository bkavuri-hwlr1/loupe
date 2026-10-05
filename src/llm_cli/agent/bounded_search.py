"""Match untrusted Python regexes outside the daemon, with one search deadline."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from _thread import RLock
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

SEARCH_TIMEOUT_SECONDS = 5.0
_POLL_SECONDS = 0.05

# -I ignores both cwd and PYTHONPATH. The worker imports only the standard
# library, so neither a repository module nor editable-install resolution is
# needed. Compilation belongs here too: deeply nested patterns can be costly.
_WORKER = r"""
import json
import re
import sys

request = json.load(sys.stdin)
try:
    expression = re.compile(request["pattern"])
except (re.error, RecursionError, OverflowError):
    print(json.dumps({"error": "invalid regular expression"}))
    sys.exit(0)
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
                    print(json.dumps({"matches": matches}))
                    sys.exit(0)
            seen += 1
print(json.dumps({"matches": matches}))
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


def find_matches(
    pattern: str,
    files: list[tuple[str, str]],
    *,
    offset: int,
    limit: int,
    budget: SearchBudget,
) -> list[tuple[str, int, str]]:
    """Return a page plus one match; always reap the worker on stop/failure."""

    budget.remaining()
    payload: str | None = json.dumps(
        {"pattern": pattern, "files": files, "offset": offset, "limit": limit}
    )
    with subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", _WORKER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    ) as process:
        try:
            while True:
                try:
                    stdout, _ = process.communicate(
                        payload, timeout=min(_POLL_SECONDS, budget.remaining())
                    )
                    break
                except subprocess.TimeoutExpired:
                    # communicate resumes its buffered input/output on retry.
                    payload = None
            budget.remaining()
            if process.returncode != 0:
                raise SearchError("search worker could not complete that pattern")
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
    result = json.loads(stdout)
    if "error" in result:
        raise SearchError("that is not a valid regular expression")
    return [(item[0], item[1], item[2]) for item in result["matches"]]
