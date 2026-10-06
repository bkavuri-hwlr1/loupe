"""Validated compiled defaults for the initial local-only release."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    profile_id: str = "default"
    coordination_mode: str = "enforce"
    workspace_mode: str = "shared"
    agent_provider: str = "anthropic"
    agent_model: str | None = None
    # Sandboxed run_command policy: "ask" needs the user's approval in an
    # interactive session, "allow" runs without asking, "off" never offers it.
    agent_commands: str = "ask"
    # Offer the explore tool, whose read-only helpers spend extra model tokens.
    agent_explore: bool = True
    # The most reasoning effort explore helpers use; None means the task's own.
    # Helpers never use more effort than the task. Low effort halved helper
    # time in live runs without visibly weaker reports.
    agent_explore_effort: str | None = "low"
    launch_lease_ms: int = 10 * 60 * 1_000
    work_lease_ms: int = 90 * 1_000
    renewal_interval_ms: int = 30 * 1_000
    reconciliation_interval_ms: int = 30 * 1_000
    shutdown_drain_ms: int = 10 * 1_000
    terminal_retention_ms: int = 30 * 24 * 60 * 60 * 1_000
    handoff_retention_ms: int = 90 * 24 * 60 * 60 * 1_000
    max_frame_bytes: int = 4 * 1024 * 1024


__all__ = ["Settings"]
