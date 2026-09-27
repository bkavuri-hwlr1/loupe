"""Configuration-driven bounds for one supervised agent execution.

Every limit here exists because an agent loop has no natural stopping point.
The defaults come from the implementation plan's agent-runtime section; a
policy layer may lower them, but nothing in the execution path may raise them
at runtime, which is why the record is frozen.
"""

from __future__ import annotations

from dataclasses import dataclass

_KIB = 1024
_MIB = 1024 * _KIB
MAX_ANSWER_CHARACTERS = 256 * _KIB
MAX_TASK_SUMMARY_CHARACTERS = 2_000


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """Bounds applied to a single task attempt."""

    wall_clock_seconds: int = 2 * 60 * 60
    max_tool_calls: int = 500
    max_tool_output_bytes: int = 64 * _KIB
    max_read_files: int = 200
    max_read_bytes: int = 2 * _MIB
    max_write_bytes: int = 2 * _MIB
    max_search_results: int = 200
    max_scanned_files: int = 5_000

    def __post_init__(self) -> None:
        if (
            min(
                self.wall_clock_seconds,
                self.max_tool_calls,
                self.max_tool_output_bytes,
                self.max_read_files,
                self.max_read_bytes,
                self.max_write_bytes,
                self.max_search_results,
                self.max_scanned_files,
            )
            <= 0
        ):
            raise ValueError("every execution limit must be positive")

    @property
    def tool_call_warning_threshold(self) -> int:
        """When to warn that a run is heading for its hard cap.

        Derived rather than configured: an independent field can be set above
        the cap it is meant to precede, which makes the warning useless exactly
        when a policy tightens the cap.
        """

        return max(1, self.max_tool_calls * 4 // 5)


DEFAULT_LIMITS = ExecutionLimits()

__all__ = [
    "DEFAULT_LIMITS",
    "MAX_ANSWER_CHARACTERS",
    "MAX_TASK_SUMMARY_CHARACTERS",
    "ExecutionLimits",
]
