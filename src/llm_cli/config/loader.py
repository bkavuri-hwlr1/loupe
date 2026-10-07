"""Strict TOML configuration loading for the small supported key set."""

from __future__ import annotations

import re
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from llm_cli.config.models import McpServerConfig, Settings
from llm_cli.coordination.models import EFFORT_LEVELS
from llm_cli.errors import ErrorCode, LlmCoordError

_ROOT_KEYS = {"agent", "core", "leases", "mcp"}
_MCP_SERVER_KEYS = {"command", "env", "approval", "timeout", "cwd"}
# Server names become part of tool names, which providers limit to letters,
# digits, "_" and "-"; a double underscore would make names ambiguous.
_MCP_SERVER_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]|_(?!_)){0,23}")
_MCP_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_MCP_SERVERS = 16
_CORE_KEYS = {"coordination_mode"}
_AGENT_KEYS = {"provider", "model", "commands", "explore", "explore_effort"}
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
    explore = agent.get("explore", settings.agent_explore)
    if not isinstance(explore, bool):
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID, "agent.explore must be true or false"
        )
    explore_effort = agent.get("explore_effort", settings.agent_explore_effort)
    if explore_effort == "task":
        explore_effort = None
    elif explore_effort is not None and explore_effort not in EFFORT_LEVELS:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            'agent.explore_effort must be "task" or an effort level, such as low',
        )
    updates: dict[str, Any] = {
        "coordination_mode": mode,
        "agent_provider": provider,
        "agent_model": model,
        "agent_commands": commands,
        "agent_explore": explore,
        "agent_explore_effort": explore_effort,
        "mcp_servers": _mcp_servers(_table(data, "mcp")),
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


def _mcp_servers(mcp: dict[str, Any]) -> tuple[McpServerConfig, ...]:
    if set(mcp) - {"servers"}:
        raise _unknown("mcp", set(mcp) - {"servers"})
    servers = _table(mcp, "servers")
    if len(servers) > _MAX_MCP_SERVERS:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            f"at most {_MAX_MCP_SERVERS} MCP servers can be configured",
        )
    configured = []
    for name, raw in servers.items():
        where = f"mcp.servers.{name}"
        if not _MCP_SERVER_NAME.fullmatch(name) or name.endswith("_"):
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                f"{where}: names use lowercase letters, digits, '-' and single "
                "'_', at most 24 characters",
            )
        if not isinstance(raw, dict):
            raise LlmCoordError(ErrorCode.CONFIG_INVALID, f"{where} must be a table")
        if set(raw) - _MCP_SERVER_KEYS:
            raise _unknown(where, set(raw) - _MCP_SERVER_KEYS)
        command = raw.get("command")
        if (
            not isinstance(command, list)
            or not command
            or len(command) > 64
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                f"{where}.command must be a list of 1 to 64 non-empty strings",
            )
        env = raw.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(key, str)
            and _MCP_ENV_NAME.fullmatch(key)
            and isinstance(value, str)
            for key, value in env.items()
        ):
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                f"{where}.env must map variable names to text",
            )
        approval = raw.get("approval", "ask")
        if approval not in {"ask", "allow"}:
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID, f"{where}.approval must be ask or allow"
            )
        timeout = raw.get("timeout", 120)
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                f"{where}.timeout must be a whole number of seconds from 1 to 600",
            )
        cwd = raw.get("cwd")
        if cwd is not None and (
            not isinstance(cwd, str) or not Path(cwd).is_absolute()
        ):
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID, f"{where}.cwd must be an absolute path"
            )
        configured.append(
            McpServerConfig(
                name=name,
                command=tuple(command),
                env=tuple(sorted(env.items())),
                approval=approval,
                timeout_seconds=timeout,
                cwd=Path(cwd) if cwd is not None else None,
            )
        )
    return tuple(configured)


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
