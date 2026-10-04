"""Strict TOML configuration loading for the small supported key set."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from llm_cli.config.models import Settings
from llm_cli.errors import ErrorCode, LlmCoordError

_ROOT_KEYS = {"agent", "core", "leases"}
_CORE_KEYS = {"coordination_mode"}
_AGENT_KEYS = {"provider", "model", "commands"}
_LEASE_KEYS = {
    "launch_ms",
    "work_ms",
    "renewal_interval_ms",
    "reconciliation_interval_ms",
}


def load_settings(path: Path, *, profile_id: str = "default") -> Settings:
    settings = Settings(profile_id=profile_id)
    if not path.exists():
        return settings
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID, f"configuration could not be read: {exc}"
        ) from exc
    unknown_root = set(data) - _ROOT_KEYS
    if unknown_root:
        raise _unknown("configuration", unknown_root)
    core = _table(data, "core")
    agent = _table(data, "agent")
    leases = _table(data, "leases")
    if set(core) - _CORE_KEYS:
        raise _unknown("core", set(core) - _CORE_KEYS)
    if set(leases) - _LEASE_KEYS:
        raise _unknown("leases", set(leases) - _LEASE_KEYS)
    if set(agent) - _AGENT_KEYS:
        raise _unknown("agent", set(agent) - _AGENT_KEYS)

    mode = core.get("coordination_mode", settings.coordination_mode)
    if mode not in {"off", "observe", "enforce"}:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            "core.coordination_mode must be off, observe, or enforce",
        )
    provider = _bounded_text(
        agent.get("provider", settings.agent_provider),
        "agent.provider",
        maximum=64,
    )
    raw_model = agent.get("model", settings.agent_model)
    model = (
        None
        if raw_model is None
        else _bounded_text(raw_model, "agent.model", maximum=256)
    )
    commands = agent.get("commands", settings.agent_commands)
    if commands not in {"ask", "allow", "off"}:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID, "agent.commands must be ask, allow, or off"
        )
    updates: dict[str, Any] = {
        "coordination_mode": mode,
        "agent_provider": provider,
        "agent_model": model,
        "agent_commands": commands,
    }
    mapping = {
        "launch_ms": "launch_lease_ms",
        "work_ms": "work_lease_ms",
        "renewal_interval_ms": "renewal_interval_ms",
        "reconciliation_interval_ms": "reconciliation_interval_ms",
    }
    for key, attribute in mapping.items():
        if key in leases:
            value = leases[key]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID, f"leases.{key} must be a positive integer"
                )
            updates[attribute] = value
    candidate = replace(settings, **updates)
    if candidate.renewal_interval_ms >= candidate.work_lease_ms:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            "lease renewal interval must be shorter than the work lease",
        )
    return candidate


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise LlmCoordError(ErrorCode.CONFIG_INVALID, f"{name} must be a TOML table")
    return value


def _unknown(section: str, keys: set[str]) -> LlmCoordError:
    joined = ", ".join(sorted(keys))
    return LlmCoordError(
        ErrorCode.CONFIG_INVALID, f"unknown {section} key(s): {joined}"
    )


def _bounded_text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            f"{name} must be non-empty text no longer than {maximum} characters",
        )
    return value


__all__ = ["load_settings"]
