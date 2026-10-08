"""Bound how long the agent keeps repairing a failing check.

A check that fails starts a repair: the agent edits and runs the check again.
Each later failing run, until the check passes, is a failed repair attempt.
When the attempts run out, or the same failure comes back three times in a row,
the repair stops: the agent may make no more edits or check runs, and the task
can only finish as partial, with its edits kept for review.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# Outcomes caused by the code under test. A check that could not run (a
# snapshot error, a cancellation) says nothing about the repair.
FAILED_STATES = frozenset({"failed", "timed_out", "source_mutated"})
# The same failure this many times in a row means the edits are not helping.
UNCHANGED_LIMIT = 3
_MAX_CHECKS = 100
_MAX_NAME_CHARACTERS = 256
_SIGNATURE = re.compile(r"[0-9a-f]{64}")
# Output that differs between runs of the same failure: timings, run and
# temporary-directory identifiers, and object addresses.
_VOLATILE = (
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|secs|seconds)\b"), "<time>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<address>"),
    (re.compile(r"(?<![0-9a-f])[0-9a-f]{16,}(?![0-9a-f])"), "<id>"),
    (re.compile(r"pytest-\d+"), "pytest-<n>"),
)


def failure_signature(output: str) -> str:
    """A digest of check output that ignores what varies between runs."""

    for pattern, replacement in _VOLATILE:
        output = pattern.sub(replacement, output)
    return hashlib.sha256(output.strip().encode("utf-8")).hexdigest()


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


@dataclass(slots=True)
class RepairTracker:
    """Consecutive failing runs of each check since it last passed."""

    failures: dict[str, int] = field(default_factory=dict)
    signatures: dict[str, str] = field(default_factory=dict)
    unchanged: dict[str, int] = field(default_factory=dict)
    # The check whose repair ran out; set once and kept for the task.
    exhausted: str | None = None

    def record(
        self, name: str, state: str, output: str, *, attempts: int
    ) -> str | None:
        """Count one check run; return a note for the agent, if any."""

        if self.exhausted is not None:
            return None
        if state == "passed":
            for table in (self.failures, self.signatures, self.unchanged):
                table.pop(name, None)
            return None
        if state not in FAILED_STATES:
            return None
        failures = self.failures.get(name, 0) + 1
        signature = failure_signature(output)
        unchanged = (
            self.unchanged.get(name, 0) + 1
            if self.signatures.get(name) == signature
            else 1
        )
        self.failures[name] = failures
        self.signatures[name] = signature
        self.unchanged[name] = unchanged
        used = failures - 1
        if used >= attempts or unchanged >= UNCHANGED_LIMIT:
            self.exhausted = name
            reason = (
                f"with the same failure the last {unchanged} times"
                if unchanged >= UNCHANGED_LIMIT
                else f"after {_plural(attempts, 'repair attempt')}"
            )
            return (
                f"[Repair limit reached: {name} failed {failures} times in a row, "
                f"{reason}. Make no more edits or check runs. Call finish_task with "
                'outcome "partial" and explain what you changed, what still fails, '
                "and what you would try next. Your edits are kept for review.]"
            )
        remaining = _plural(attempts - used, "repair attempt")
        if used == 0:
            return f"[{name} failed. Fix the cause and run it again; {remaining} left.]"
        same = " with the same failure as before" if unchanged > 1 else ""
        return (
            f"[Repair attempt {used} of {attempts} for {name} failed{same}; "
            f"{remaining} left.]"
        )

    def refusal(self) -> str:
        return (
            f"the repair limit for {self.exhausted} was reached; make no more edits "
            'or check runs, and call finish_task with outcome "partial"'
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "failures": dict(self.failures),
            "signatures": dict(self.signatures),
            "unchanged": dict(self.unchanged),
            "exhausted": self.exhausted,
        }

    @classmethod
    def from_dict(cls, saved: object) -> RepairTracker:
        """Restore checkpointed counts without accepting malformed ones."""

        if saved is None:
            return cls()
        if not isinstance(saved, Mapping) or set(saved) != {
            "failures",
            "signatures",
            "unchanged",
            "exhausted",
        }:
            raise ValueError("saved repair state is malformed")
        failures = _counts(saved["failures"])
        unchanged = _counts(saved["unchanged"])
        signatures = saved["signatures"]
        exhausted = saved["exhausted"]
        if (
            not isinstance(signatures, Mapping)
            or set(signatures) != set(failures)
            or set(unchanged) != set(failures)
            or any(
                not isinstance(value, str) or not _SIGNATURE.fullmatch(value)
                for value in signatures.values()
            )
            or any(unchanged[name] > failures[name] for name in failures)
            or (exhausted is not None and exhausted not in failures)
        ):
            raise ValueError("saved repair state is malformed")
        return cls(dict(failures), dict(signatures), dict(unchanged), exhausted)


def _counts(value: object) -> dict[str, int]:
    if (
        not isinstance(value, Mapping)
        or len(value) > _MAX_CHECKS
        or any(
            not isinstance(name, str)
            or not 0 < len(name) <= _MAX_NAME_CHARACTERS
            or type(count) is not int
            or count < 1
            for name, count in value.items()
        )
    ):
        raise ValueError("saved repair state is malformed")
    return dict(value)


__all__ = ["FAILED_STATES", "UNCHANGED_LIMIT", "RepairTracker", "failure_signature"]
