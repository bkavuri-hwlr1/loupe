"""Validated compiled defaults for the initial local-only release."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """One configured Model Context Protocol server."""

    name: str
    command: tuple[str, ...] = ()
    # Added to the server's minimal environment; Loupe's own is not passed.
    env: tuple[tuple[str, str], ...] = ()
    # "ask" needs the user's approval in an interactive session; "allow" does
    # not. Servers that need approval are not offered without someone to ask.
    approval: str = "ask"
    timeout_seconds: int = 120
    # The server's working directory; None means the user's home directory,
    # so no checkout is implied.
    cwd: Path | None = None
    # A Streamable HTTP endpoint instead of a stdio command.
    url: str | None = None
    # Header name -> environment variable name; resolved values are never saved.
    headers: tuple[tuple[str, str], ...] = ()
    # User assertion about external authority, not a sandbox guarantee.
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class HookConfig:
    """A user command run in a sandboxed snapshot around the agent's tools.

    A "pre_tool" hook runs before tool calls whose names match ``match`` and
    can block them by exiting with status 2. A "post_edit" hook runs after an
    edit to paths matching ``match`` (glob patterns); "{paths}" in its command
    expands to those paths, and changes it makes to them are staged.
    """

    kind: str
    match: tuple[str, ...]
    command: tuple[str, ...]
    timeout_seconds: int = 60
    # Shown in the conversation; defaults to the program and first argument.
    name: str | None = None


@dataclass(frozen=True, slots=True)
class RebuildConfig:
    """Explicit permission to construct one private dependency root."""

    root: str
    argv: tuple[str, ...]
    network: bool = False
    timeout_seconds: int = 60


@dataclass(frozen=True, slots=True)
class Settings:
    profile_id: str = "default"
    coordination_mode: str = "enforce"
    refuse_staged_paths: bool = False
    workspace_mode: str = "shared"
    # Native file-change watchers narrow outside-edit and isolated safe-point
    # scans: "auto" where supported, "off" keeps periodic scans only.
    workspace_watchers: str = "auto"
    knowledge_index: str = "off"
    agent_provider: str = "anthropic"
    agent_model: str | None = None
    # Sandboxed run_command policy: "ask" needs the user's approval in an
    # interactive session, "allow" runs without asking, "off" never offers it.
    agent_commands: str = "ask"
    # Forward only the opening/resuming terminal, ephemerally and opt-in.
    agent_command_environment: str = "minimal"
    isolated_rebuild: tuple[RebuildConfig, ...] = ()
    isolated_rebuild_cache: bool = False
    isolated_rebuild_cache_max_bytes: int = 512 * 1024**2
    # Offer the explore tool, whose read-only helpers spend extra model tokens.
    agent_explore: bool = True
    # The most reasoning effort explore helpers use; None means the task's own.
    # Helpers never use more effort than the task. Low effort halved helper
    # time in live runs without visibly weaker reports.
    agent_explore_effort: str | None = "low"
    # One independent read-only review before finishing an edit task.
    agent_review: str = "off"
    agent_review_effort: str | None = "low"
    # Read-only web_fetch: "ask" needs approval for each new domain in a task,
    # "allow" fetches any public page, "off" never offers the tool. Domains in
    # agent_web_domains ("docs.python.org", "*.readthedocs.io") need no
    # approval.
    agent_web_fetch: str = "ask"
    agent_web_domains: tuple[str, ...] = field(default=())
    # HTTP CONNECT proxy; None uses HTTP_PROXY/HTTPS_PROXY. NO_PROXY still applies.
    agent_web_proxy: str | None = None
    mcp_servers: tuple[McpServerConfig, ...] = field(default=())
    hooks: tuple[HookConfig, ...] = field(default=())
    launch_lease_ms: int = 10 * 60 * 1_000
    work_lease_ms: int = 90 * 1_000
    renewal_interval_ms: int = 30 * 1_000
    reconciliation_interval_ms: int = 30 * 1_000
    shutdown_drain_ms: int = 10 * 1_000
    terminal_retention_ms: int = 30 * 24 * 60 * 60 * 1_000
    handoff_retention_ms: int = 90 * 24 * 60 * 60 * 1_000
    max_frame_bytes: int = 4 * 1024 * 1024


__all__ = ["HookConfig", "McpServerConfig", "RebuildConfig", "Settings"]
