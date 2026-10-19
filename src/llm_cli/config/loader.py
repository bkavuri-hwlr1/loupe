"""Strict TOML configuration loading for the small supported key set."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from llm_cli.config.models import HookConfig, McpServerConfig, Settings
from llm_cli.coordination.models import EFFORT_LEVELS
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.web.fetch import WebFetchError, check_proxy

_ROOT_KEYS = {"agent", "core", "hooks", "leases", "mcp", "knowledge"}
_HOOK_KINDS = ("pre_tool", "post_edit")
_HOOK_KEYS = {"match", "command", "timeout", "name"}
_MAX_HOOKS = 16
_MCP_SERVER_KEYS = {
    "command",
    "env",
    "approval",
    "timeout",
    "cwd",
    "url",
    "headers",
    "read_only",
}
_MCP_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}")
_MCP_RESERVED_HEADERS = {
    "accept",
    "content-type",
    "content-length",
    "host",
    "connection",
    "transfer-encoding",
    "mcp-session-id",
    "mcp-protocol-version",
}
# Server names become part of tool names, which providers limit to letters,
# digits, "_" and "-"; a double underscore would make names ambiguous.
_MCP_SERVER_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]|_(?!_)){0,23}")
_MCP_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_MCP_SERVERS = 16
_CORE_KEYS = {"coordination_mode"}
_AGENT_KEYS = {
    "provider",
    "model",
    "commands",
    "command_environment",
    "explore",
    "explore_effort",
    "review",
    "review_effort",
    "web_fetch",
    "web_domains",
    "web_proxy",
}
# A hostname, optionally led by "*." for its subdomains.
_WEB_DOMAIN = re.compile(
    r"(\*\.)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)
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
    knowledge = _table(data, "knowledge")
    if set(knowledge) - {"index"}:
        raise _unknown("knowledge", set(knowledge) - {"index"})
    knowledge_index = knowledge.get("index", settings.knowledge_index)
    if not isinstance(knowledge_index, str) or knowledge_index not in {
        "off",
        "lexical",
    }:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID, 'knowledge.index must be "off" or "lexical"'
        )
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
    command_environment = agent.get(
        "command_environment", settings.agent_command_environment
    )
    if not isinstance(command_environment, str) or command_environment not in {
        "minimal",
        "session",
    }:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            'agent.command_environment must be "minimal" or "session"',
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
    review = agent.get("review", settings.agent_review)
    if not isinstance(review, str) or review not in {"off", "on"}:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID, 'agent.review must be "off" or "on"'
        )
    review_effort = agent.get("review_effort", settings.agent_review_effort)
    if review_effort == "task":
        review_effort = None
    elif review_effort is not None and (
        not isinstance(review_effort, str) or review_effort not in EFFORT_LEVELS
    ):
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            'agent.review_effort must be "task" or an effort level, such as low',
        )
    web_fetch = agent.get("web_fetch", settings.agent_web_fetch)
    if web_fetch not in {"ask", "allow", "off"}:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID, "agent.web_fetch must be ask, allow, or off"
        )
    web_domains = agent.get("web_domains", list(settings.agent_web_domains))
    if (
        not isinstance(web_domains, list)
        or len(web_domains) > 64
        or not all(
            isinstance(domain, str) and _WEB_DOMAIN.fullmatch(domain)
            for domain in web_domains
        )
    ):
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            "agent.web_domains must list at most 64 lowercase hostnames, each "
            'optionally starting with "*."',
        )
    web_proxy = agent.get("web_proxy", settings.agent_web_proxy)
    if web_proxy is not None:
        if not isinstance(web_proxy, str) or len(web_proxy) > 2_048:
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID, "agent.web_proxy must be an HTTP proxy URL"
            )
        try:
            check_proxy(web_proxy)
        except WebFetchError as exc:
            raise LlmCoordError(ErrorCode.CONFIG_INVALID, str(exc)) from None
    updates: dict[str, Any] = {
        "coordination_mode": mode,
        "knowledge_index": knowledge_index,
        "agent_provider": provider,
        "agent_model": model,
        "agent_commands": commands,
        "agent_command_environment": command_environment,
        "agent_explore": explore,
        "agent_explore_effort": explore_effort,
        "agent_review": review,
        "agent_review_effort": review_effort,
        "agent_web_fetch": web_fetch,
        "agent_web_domains": tuple(web_domains),
        "agent_web_proxy": web_proxy,
        "mcp_servers": _mcp_servers(_table(data, "mcp")),
        "hooks": _hooks(_table(data, "hooks")),
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


def _hooks(hooks: dict[str, Any]) -> tuple[HookConfig, ...]:
    if set(hooks) - set(_HOOK_KINDS):
        raise _unknown("hooks", set(hooks) - set(_HOOK_KINDS))
    configured: list[HookConfig] = []
    for kind in _HOOK_KINDS:
        entries = hooks.get(kind, [])
        if not isinstance(entries, list) or len(entries) > _MAX_HOOKS:
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                f"hooks.{kind} must be a list of at most {_MAX_HOOKS} tables; "
                f"write each as [[hooks.{kind}]]",
            )
        for index, raw in enumerate(entries, 1):
            where = f"hooks.{kind} #{index}"
            if not isinstance(raw, dict):
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID, f"{where} must be a table"
                )
            if set(raw) - _HOOK_KEYS:
                raise _unknown(where, set(raw) - _HOOK_KEYS)
            match = raw.get("match")
            if (
                not isinstance(match, list)
                or not 1 <= len(match) <= 32
                or not all(isinstance(item, str) and item for item in match)
            ):
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID,
                    f"{where}.match must list 1 to 32 non-empty patterns",
                )
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
            if kind == "pre_tool" and "{paths}" in command:
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID,
                    f"{where}: only post_edit hooks receive {{paths}}",
                )
            timeout = raw.get("timeout", 60)
            if type(timeout) is not int or not 1 <= timeout <= 600:
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID,
                    f"{where}.timeout must be a whole number of seconds from 1 to 600",
                )
            name = raw.get("name")
            if name is not None:
                name = _bounded_text(name, f"{where}.name", maximum=40)
            configured.append(
                HookConfig(kind, tuple(match), tuple(command), timeout, name)
            )
    return tuple(configured)


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


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
        url = raw.get("url")
        if (command is None) == (url is None):
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                f"{where} must configure exactly one of command or url",
            )
        if command is not None and (
            not isinstance(command, list)
            or not command
            or len(command) > 64
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                f"{where}.command must be a list of 1 to 64 non-empty strings",
            )
        if url is not None:
            from llm_cli.agent.source_policy import contains_secret_material

            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                valid_url = (
                    parsed is not None
                    and parsed.scheme in {"http", "https"}
                    and bool(parsed.hostname)
                    # Header values are credentials; only local HTTP is plain.
                    and (parsed.scheme == "https" or _loopback(parsed.hostname))
                    and parsed.port != 0
                    and parsed.username is None
                    and parsed.password is None
                    and not parsed.fragment
                    and len(url) <= 2_048
                    and not any(ord(char) < 33 or ord(char) > 126 for char in url)
                    and not contains_secret_material(url)
                )
            except ValueError:
                valid_url = False
            if not valid_url:
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID,
                    f"{where}.url must be an HTTPS endpoint without credentials "
                    "(plain HTTP only on localhost)",
                )
            if "env" in raw or "cwd" in raw:
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID,
                    f"{where}: env and cwd apply only to command servers",
                )
        headers = raw.get("headers", {})
        if (
            not isinstance(headers, dict)
            or len(headers) > 32
            or not all(
                isinstance(key, str)
                and _MCP_HEADER_NAME.fullmatch(key)
                and key.lower() not in _MCP_RESERVED_HEADERS
                and isinstance(value, str)
                and _MCP_ENV_NAME.fullmatch(value)
                for key, value in headers.items()
            )
            or len({key.lower() for key in headers}) != len(headers)
            or (headers and url is None)
        ):
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                f"{where}.headers must map HTTP header names to environment "
                "variable names (protocol headers are reserved)",
            )
        read_only = raw.get("read_only", False)
        if type(read_only) is not bool:
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID, f"{where}.read_only must be true or false"
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
        if not isinstance(approval, str) or approval not in {"ask", "allow"}:
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
                command=tuple(command or ()),
                env=tuple(sorted(env.items())),
                approval=approval,
                timeout_seconds=timeout,
                cwd=Path(cwd) if cwd is not None else None,
                url=url,
                headers=tuple(sorted(headers.items())),
                read_only=read_only,
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
