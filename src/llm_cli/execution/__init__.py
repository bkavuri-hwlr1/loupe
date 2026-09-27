"""Durable, provider-independent local task execution primitives."""

from llm_cli.execution.runner import (
    ExecutionResult,
    FixtureWrite,
    FixtureWriteRunner,
    parse_fixture_writes,
)

__all__ = [
    "ExecutionResult",
    "FixtureWrite",
    "FixtureWriteRunner",
    "parse_fixture_writes",
]
