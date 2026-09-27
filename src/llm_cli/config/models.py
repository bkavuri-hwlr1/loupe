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
    launch_lease_ms: int = 10 * 60 * 1_000
    work_lease_ms: int = 90 * 1_000
    renewal_interval_ms: int = 30 * 1_000
    reconciliation_interval_ms: int = 30 * 1_000
    shutdown_drain_ms: int = 10 * 1_000
    terminal_retention_ms: int = 30 * 24 * 60 * 60 * 1_000
    handoff_retention_ms: int = 90 * 24 * 60 * 60 * 1_000
    max_frame_bytes: int = 4 * 1024 * 1024


__all__ = ["Settings"]
