"""Stable command-line surface for the initial control-plane foundation."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from llm_cli import __version__
from llm_cli.agent.modes import AGENT_MODES, resolve_agent_mode
from llm_cli.build import code_identity
from llm_cli.cli.auth import auth_command
from llm_cli.cli.exit_codes import EXIT_BY_ERROR
from llm_cli.cli.interrupts import ExitRequested
from llm_cli.cli.output import emit
from llm_cli.cli.render import checkout_event_text
from llm_cli.cli.session import (
    APPLY_TIMEOUT_SECONDS,
    _follow,
    _patient,
    _runtime_open_client,
    open_or_resume_session,
    remove_session_secret,
    run_session,
    session_resume_secret,
    switch_session_workspace,
)
from llm_cli.cli.terminal import TerminalUI
from llm_cli.config.loader import load_settings
from llm_cli.coordination.models import EFFORT_LEVELS
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.ids import new_id
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient, session_environment_params

if TYPE_CHECKING:
    from llm_cli.workspace.hydration import HydrationEntry, HydrationPlan

# Commands that write their own output as it arrives have nothing left for the
# emitter, and a plain None would be indistinguishable from a null result.
_RENDERED = object()
# Least to most reasoning. The selected model's own levels are checked later.
_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_EFFORT_CHOICES = (
    *(level for level in _EFFORT_ORDER if level in EFFORT_LEVELS),
    "default",
)
_EFFORT_HELP = (
    "reasoning effort for a new conversation, such as low, medium, high, or "
    "xhigh; 'default' lets the provider choose (use /effort to save a choice)"
)


_LEGACY_CLAIM_NOTICE = (
    "Legacy claim mutations are deprecated for one compatibility release and "
    "require a legacy checkout. Use 'loupe workspace cutover plan PATH' to "
    "review migration to workspace execution; retained claims remain inspectable."
)


def _legacy_claim_result(value: Any, *, as_json: bool) -> Any:
    if as_json:
        return value
    return {**value, "notice": _LEGACY_CLAIM_NOTICE}


def _runtime_consent_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--approve-secret",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "allow a private copy of this exact ignored runtime file in one new "
            "isolated workspace; repeat for more files (never recursive)"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loupe",
        description=(
            "Loupe — your coding agent in the terminal. "
            "Run without a command to start a conversation."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--mode", dest="agent_mode", choices=AGENT_MODES)
    parser.add_argument(
        "--effort", choices=_EFFORT_CHOICES, metavar="LEVEL", help=_EFFORT_HELP
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="disable terminal styling and the interactive editor",
    )
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("init", help="initialize private local state and databases")
    commands.add_parser("doctor", help="run local safety and feature checks")
    commands.add_parser(
        "demo", help="preview the Loupe interface without a model or daemon"
    )

    auth = commands.add_parser("auth", help="manage saved AI accounts")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    for action in ("login", "status", "logout"):
        auth_action = auth_commands.add_parser(action)
        auth_action.add_argument(
            "provider",
            nargs="?",
            choices=("codex", "anthropic", "openai"),
            default="codex",
        )
        if action == "login":
            method = auth_action.add_mutually_exclusive_group()
            method.add_argument(
                "--device", action="store_true", help="sign in using a device code"
            )
            method.add_argument(
                "--from-codex",
                action="store_true",
                help="copy Codex's current access token (expires; no refresh token)",
            )

    daemon = commands.add_parser("daemon", help="manage the local daemon")
    daemon_commands = daemon.add_subparsers(dest="daemon_command", required=True)
    daemon_commands.add_parser("start")
    daemon_commands.add_parser("status")
    daemon_commands.add_parser("stop")
    daemon_commands.add_parser("restart")

    database = commands.add_parser("db", help="inspect local databases")
    database_commands = database.add_subparsers(dest="db_command", required=True)
    database_commands.add_parser("status")
    database_commands.add_parser("check")
    backup = database_commands.add_parser(
        "backup", help="write verified online backups"
    )
    backup.add_argument("--out", type=Path, help="parent of a fresh backup directory")

    prune = database_commands.add_parser("prune", help="prune old settled task output")
    prune.add_argument(
        "--older-than", default="30d", help="positive age in days, e.g. 30d"
    )
    prune.add_argument("--apply", action="store_true", help="delete output and VACUUM")

    index = commands.add_parser("index", help="manage the optional local lexical index")
    index_commands = index.add_subparsers(dest="index_command", required=True)
    for action in ("status", "build", "rebuild", "drop"):
        index_action = index_commands.add_parser(action)
        index_action.add_argument("--repo", default=None if action == "status" else ".")
    search = commands.add_parser("search", help="search the local repository index")
    search_commands = search.add_subparsers(dest="search_command", required=True)
    lexical = search_commands.add_parser("lexical")
    lexical.add_argument("query")
    lexical.add_argument("--repo", default=".")
    lexical.add_argument("--limit", type=int, default=8)
    lexical.add_argument("--explain", action="store_true")

    graph = commands.add_parser(
        "graph", help="inspect the optional deterministic code graph"
    )
    graph_commands = graph.add_subparsers(dest="graph_command", required=True)
    neighbors = graph_commands.add_parser(
        "neighbors", help="show imports, tests, definitions and batches"
    )
    neighbors.add_argument("node")
    neighbors.add_argument("--repo", default=".")
    neighbors.add_argument(
        "--edge", choices=("imports", "tests", "defines", "changed-by")
    )
    neighbors.add_argument("--depth", type=int, choices=range(1, 5), default=1)

    config = commands.add_parser("config", help="inspect and edit validated settings")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show")
    config_set = config_commands.add_parser("set")
    config_set.add_argument("key", help="dotted key, such as agent.commands")
    config_set.add_argument("value", help="text or TOML value, such as allow or true")

    repository = commands.add_parser("repo", help="register and inspect repositories")
    repository_commands = repository.add_subparsers(dest="repo_command", required=True)
    add = repository_commands.add_parser("add")
    add.add_argument("path", nargs="?", default=".")
    add.add_argument("--target")
    add.add_argument("--local-only", action="store_true")
    repository_commands.add_parser("list")
    status = repository_commands.add_parser("status")
    status.add_argument("path", nargs="?", default=".")

    run = commands.add_parser(
        "run",
        help=(
            "run one task through a claimed worktree: the coding agent by "
            "default, the deterministic fixture driver with --fixture-write"
        ),
    )
    run.add_argument("task", help="what the agent should do, in plain language")
    run.add_argument("--repo", default=".")
    run.add_argument("--scope", action="append", required=True)
    run.add_argument("--provider", help="model-provider adapter for this task")
    run.add_argument("--model", help="provider model ID for this task")
    run.add_argument("--task-id")
    run.add_argument(
        "--mode",
        dest="agent_mode",
        choices=AGENT_MODES,
        default=argparse.SUPPRESS,
        help="use a shared-checkout task in plan, normal review, or auto mode",
    )
    run.add_argument(
        "--follow",
        action="store_true",
        help="follow the task's responses and tool activity until it settles",
    )
    run.add_argument(
        "--interactive",
        action="store_true",
        help="allow agent questions and follow the task in this terminal",
    )
    run.add_argument(
        "--claim-only",
        action="store_true",
        help="acquire or queue the claim without starting any driver",
    )
    run.add_argument(
        "--fixture-write",
        action="append",
        metavar="PATH=CONTENT",
        help=(
            "exercise the fenced worktree/publication lifecycle with a "
            "deterministic write; repeat for multiple files"
        ),
    )

    chat = commands.add_parser(
        "chat",
        help="open an interactive session: each prompt runs as its own task",
    )
    chat.add_argument("--repo", default=".")
    chat.add_argument(
        "--publish",
        choices=("auto", "review"),
        help="legacy alias for auto or normal mode",
    )
    chat.add_argument(
        "--mode", dest="agent_mode", choices=AGENT_MODES, default=argparse.SUPPRESS
    )
    chat.add_argument(
        "--effort",
        choices=_EFFORT_CHOICES,
        metavar="LEVEL",
        default=argparse.SUPPRESS,
        help=_EFFORT_HELP,
    )
    chat.add_argument(
        "--scope",
        action="append",
        help="editable path; repeat to narrow scope (default: whole repository)",
    )
    chat.add_argument(
        "--plain",
        action="store_true",
        default=argparse.SUPPRESS,
        help="use a plain line-oriented conversation",
    )
    chat.add_argument("--provider", help="model-provider adapter for this session")
    chat.add_argument("--model", help="provider model ID for this session")
    chat.add_argument("--resume", metavar="SESSION_ID", help="resume a prior chat")
    chat.add_argument(
        "--workspace",
        choices=("shared", "isolated"),
        help="where this session prepares edits; defaults to the shared checkout",
    )

    _runtime_consent_argument(chat)

    checks = commands.add_parser("checks", help="configure named verification commands")
    check_actions = checks.add_subparsers(dest="checks_command", required=True)
    for action in ("configure", "list"):
        sub = check_actions.add_parser(action)
        sub.add_argument("--repo", default=".")
        if action == "configure":
            sub.add_argument("--file", required=True)

    workspace = commands.add_parser(
        "workspace",
        help="inspect and operate the workspace a checkout coordinates through",
    )
    workspace_commands = workspace.add_subparsers(
        dest="workspace_command", required=True
    )
    workspace_status = workspace_commands.add_parser("status")
    workspace_status.add_argument("path", nargs="?", default=".")
    authority = workspace_commands.add_parser(
        "authority", help="inspect or explicitly prune stale physical ownership records"
    )
    authority_actions = authority.add_subparsers(
        dest="authority_command", required=True
    )
    authority_actions.add_parser(
        "status", help="list records and reasons they are retained"
    )
    authority_prune = authority_actions.add_parser(
        "prune", help="preview stale records; --apply removes them"
    )
    authority_prune.add_argument("--apply", action="store_true")
    authority_prune.add_argument(
        "--include-unrecorded",
        action="store_true",
        help="also consider records written before checkout paths were recorded",
    )
    authority_prune.add_argument(
        "--include-other-profiles",
        action="store_true",
        help="also consider records last owned by another profile or installation; "
        "a profile that still binds a removed record refuses its checkout",
    )
    cutover = workspace_commands.add_parser(
        "cutover", help="preview or explicitly migrate a checkout out of legacy claims"
    )
    cutover_actions = cutover.add_subparsers(dest="cutover_command", required=True)
    for action in ("plan", "apply", "resume", "recovery", "status", "export"):
        help_text = {
            "plan": "read the complete conversion preview without changing authority",
            "apply": "irreversibly begin cutover from an exact reviewed checkpoint",
            "resume": "resume the same durable cutover intent after interruption",
            "recovery": "preview current history for forward-only cutover recovery",
            "status": "inspect the authority regime and retained cutover intent",
            "export": "save retained legacy history to a new private JSON file",
        }[action]
        sub = cutover_actions.add_parser(action, help=help_text)
        sub.add_argument("path", nargs="?", default=".")
        if action in {"apply", "resume", "recovery"}:
            sub.add_argument("--checkpoint", required=True, metavar="TOKEN")
            sub.add_argument("--intent", required=action != "apply", metavar="ID")
        if action == "resume":
            sub.add_argument(
                "--accept-current-history", metavar="RECOVERY_CHECKPOINT",
                help="acknowledge exactly the metadata inventory shown by recovery",
            )
        if action == "apply":
            sub.add_argument(
                "--cancel-claim",
                action="append",
                default=[],
                metavar="CLAIM_ID",
                help="cancel only this queued legacy claim; repeat for more",
            )
        if action == "export":
            sub.add_argument("--out", required=True, type=Path, metavar="FILE")
    hydrate = workspace_commands.add_parser(
        "hydrate", help="preview ignored runtime hydration without copying files"
    )
    hydrate.add_argument("path", nargs="?", default=".")
    hydrate.add_argument("--plan", action="store_true", required=True)
    hydrate.add_argument(
        "--approve-secret",
        action="append",
        default=[],
        metavar="PATH",
        help="preview consent for this exact runtime file; never recursive or saved",
    )
    hydrate.add_argument("--max-entries", type=int, default=20_000)
    hydrate.add_argument("--max-metadata-bytes", type=int, default=4 * 1024 * 1024)
    hydrate.add_argument("--max-depth", type=int, default=64)
    hydrate.add_argument("--max-file-bytes", type=int, default=256 * 1024 * 1024)
    hydrate.add_argument("--max-bytes", type=int, default=2 * 1024 * 1024 * 1024)
    hydrate.add_argument(
        "--max-allocated-bytes", type=int, default=2 * 1024 * 1024 * 1024
    )
    hydrate.add_argument("--max-seconds", type=float, default=10.0)
    runtime = workspace_commands.add_parser(
        "runtime", help="explicit local operations on isolated runtime files"
    )
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_export = runtime_commands.add_parser(
        "export", help="preview or copy one private runtime entry to a new destination"
    )
    runtime_export.add_argument("path")
    runtime_export.add_argument("--session", required=True, dest="session_id")
    runtime_export.add_argument("--to", required=True, type=Path, dest="destination")
    runtime_export.add_argument("--yes", action="store_true")
    runtime_export.add_argument("--allow-secret", action="store_true")
    runtime_export.add_argument("--into-checkout", action="store_true")
    processes = workspace_commands.add_parser(
        "process", help="list or stop an isolated session's managed process groups"
    )
    process_actions = processes.add_subparsers(dest="process_command", required=True)
    process_list = process_actions.add_parser("list", help="show managed processes")
    process_list.add_argument("--session", required=True, dest="session_id")
    process_stop = process_actions.add_parser("stop", help="stop a whole managed group")
    process_stop.add_argument("process_id")
    process_stop.add_argument("--session", required=True, dest="session_id")
    workspace_observe = workspace_commands.add_parser(
        "observe", help="read source outside the owner's barrier without an edit base"
    )
    workspace_observe.add_argument("path")
    workspace_observe.add_argument("--repo", default=".")
    workspace_read = workspace_commands.add_parser(
        "read", help="read one shared file and return its exact base identity"
    )
    workspace_read.add_argument("session_id")
    workspace_read.add_argument("path")
    workspace_stage = workspace_commands.add_parser(
        "stage", help="stage one UTF-8 replacement from a private local file"
    )
    workspace_stage.add_argument("session_id")
    workspace_stage.add_argument("path")
    workspace_stage.add_argument("--content-file", required=True, type=Path)
    workspace_stage.add_argument("--candidate-id")
    workspace_stage.add_argument("--executable", action="store_true")
    workspace_publish = workspace_commands.add_parser(
        "publish", help="publish one staged candidate with an exact base compare"
    )
    workspace_publish.add_argument("session_id")
    workspace_publish.add_argument("candidate_id")

    session = commands.add_parser("session", help="inspect and manage durable chats")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    open_session = session_commands.add_parser("open")
    open_session.add_argument("--repo", default=".")
    open_session.add_argument("--provider")
    open_session.add_argument("--model")
    open_session.add_argument("--publish", choices=("auto", "review"))
    open_session.add_argument("--workspace", choices=("shared", "isolated"))
    _runtime_consent_argument(open_session)
    open_session.add_argument(
        "--mode", dest="agent_mode", choices=AGENT_MODES, default=argparse.SUPPRESS
    )
    list_sessions = session_commands.add_parser("list")
    list_sessions.add_argument("--repo")
    list_sessions.add_argument(
        "--state",
        choices=("opening", "active", "disconnected", "stale", "closed"),
    )
    show_session = session_commands.add_parser("show")
    show_session.add_argument("session_id")
    resume_session = session_commands.add_parser("resume")
    resume_session.add_argument("session_id")
    switch_session = session_commands.add_parser(
        "switch", help="change a conversation's workspace or recover a pending switch"
    )
    switch_session.add_argument("session_id")
    switch_action = switch_session.add_mutually_exclusive_group(required=True)
    switch_action.add_argument("--to", choices=("shared", "isolated"), dest="workspace")
    switch_action.add_argument("--confirm", metavar="SWITCH_INTENT_ID")
    switch_action.add_argument("--rollback", metavar="SWITCH_INTENT_ID")
    disposition = switch_session.add_mutually_exclusive_group()
    for choice in ("publish", "hold", "export", "retain", "discard"):
        disposition.add_argument(
            "--" + choice,
            action="store_const",
            dest="disposition",
            const=choice,
            help="explicit disposition of private state when returning to shared mode",
        )
    _runtime_consent_argument(switch_session)
    switch_session.add_argument(
        "--switch-intent-id",
        metavar="ID",
        help="stable ID for an idempotent --to request or retry",
    )
    switch_session.add_argument(
        "--sequence",
        type=int,
        metavar="SEQUENCE",
        help=(
            "exact target bootstrap sequence from session resume; "
            "required with --confirm"
        ),
    )
    close_session = session_commands.add_parser("close")
    close_session.add_argument("session_id")
    close_session.add_argument("--reason", default="operator_exit")
    session_events = session_commands.add_parser("events")
    session_events.add_argument("session_id")
    session_events.add_argument("--after", type=int, default=0, metavar="SEQUENCE")
    session_events.add_argument("--limit", type=int, default=100)
    watch_session = session_commands.add_parser("watch")
    watch_session.add_argument("session_id")
    watch_session.add_argument("--after", type=int, default=0, metavar="SEQUENCE")
    intent = session_commands.add_parser("intent")
    intent_commands = intent.add_subparsers(dest="intent_command", required=True)
    set_intent = intent_commands.add_parser("set")
    set_intent.add_argument("session_id")
    set_intent.add_argument("paths", nargs="+")
    set_intent.add_argument("--summary")
    clear_intent = intent_commands.add_parser("clear")
    clear_intent.add_argument("session_id")
    list_intents = intent_commands.add_parser("list")
    list_intents.add_argument("session_id")

    task = commands.add_parser("task", help="inspect initial task records")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_commands.add_parser("list")
    for action in ("diff", "checks", "apply", "undo", "cancel"):
        sub = task_commands.add_parser(action)
        sub.add_argument("task_id")
        if action == "apply":
            sub.add_argument("--allow-unverified", action="store_true")
    show_task = task_commands.add_parser("show")
    show_task.add_argument("task_id")
    events_task = task_commands.add_parser(
        "events", help="read durable lifecycle events visible to every session"
    )
    events_task.add_argument("task_id")
    events_task.add_argument("--after", type=int, default=0, metavar="SEQUENCE")
    events_task.add_argument("--limit", type=int, default=100)
    watch_task = task_commands.add_parser(
        "watch", help="follow one task's durable events until it settles"
    )
    watch_task.add_argument("task_id")
    watch_task.add_argument("--after", type=int, default=0, metavar="SEQUENCE")
    retry_task = task_commands.add_parser(
        "retry", help="advance a finished task to a new attempt so it can reclaim"
    )
    retry_task.add_argument("task_id")
    discard_task = task_commands.add_parser("discard")
    discard_task.add_argument("task_id")
    task_commands.add_parser(
        "recover",
        help=(
            "resolve executions a stopped daemon abandoned, deciding each "
            "publication against the reference it targeted"
        ),
    )

    claim = commands.add_parser(
        "claim",
        help="inspect retained claims; legacy mutations are deprecated",
        description=_LEGACY_CLAIM_NOTICE,
    )
    claim_commands = claim.add_subparsers(dest="claim_command", required=True)
    list_claims = claim_commands.add_parser("list")
    list_claims.add_argument("--repo", default=".")
    show_claim = claim_commands.add_parser("show")
    show_claim.add_argument("claim_id")
    release = claim_commands.add_parser("release", description=_LEGACY_CLAIM_NOTICE)
    release.add_argument("claim_id")
    release.add_argument("--reason", required=True)
    renew = claim_commands.add_parser("renew", description=_LEGACY_CLAIM_NOTICE)
    renew.add_argument("claim_id")
    renew.add_argument("--task", required=True)
    renew.add_argument("--fence", type=int, required=True)
    renew.add_argument("--attempt", type=int, default=1)
    claim_commands.add_parser("reconcile")
    return parser


def _hydration_preview_text(plan: HydrationPlan) -> str:
    """Bound human output independently of inventory size; JSON stays complete."""

    def display(value: str, limit: int = 140) -> str:
        # Quote controls/newlines before clipping so paths cannot forge lines or
        # inject terminal sequences. JSON retains the exact, unabridged paths.
        escaped = json.dumps(value, ensure_ascii=True)[1:-1]
        return escaped if len(escaped) <= limit else escaped[: limit - 3] + "..."

    groups: dict[str, list[HydrationEntry]] = {}
    for entry in plan.entries:
        groups.setdefault(entry.action, []).append(entry)
    copies = len(groups.get("clone_or_copy", [])) + len(groups.get("private_copy", []))
    decisions = sum(entry.requires_decision for entry in plan.entries)
    unknown = sum(
        entry.kind == "directory"
        and entry.descendants_inventoried is False
        and entry.action != "exclude_administration"
        for entry in plan.entries
    )
    state = "complete" if plan.complete else "incomplete; no copy candidates"
    lines = [
        f"Hydration preview: {state}. Decisions needed: {decisions}.",
        f"Checkout: {display(plan.checkout, 180)}",
        f"Inventory: {plan.scanned_entries} entries scanned; "
        f"{len(plan.entries)} policy entries; "
        f"{plan.inventory_metadata_bytes} metadata bytes charged.",
        f"Copy candidates: {copies} files; "
        f"{plan.planned_apparent_bytes} logical bytes; "
        f"{plan.planned_estimated_copy_bytes} estimated copy bytes (COW unprobed).",
    ]
    if unknown:
        lines.append(
            f"Uninspected directories: {unknown}; descendant counts/bytes are unknown "
            "and excluded from totals."
        )
    lines.append("Totals cover only inventoried candidate files.")
    if plan.issues:
        lines.append(f"Incomplete reasons ({len(plan.issues)}):")
        lines.extend(f"  {display(issue)}" for issue in plan.issues[:5])
        if len(plan.issues) > 5:
            lines.append(f"  ... {len(plan.issues) - 5} reasons omitted")
        lines.append("Resolve these reasons and repeat the bounded preview.")
    lines.append("Actions (all counts; up to 2 sample paths each, 8 total):")
    labels = {
        "private_tree": "Private dependency trees",
        "rebuild": "Approved private rebuilds",
        "clone_or_copy": "Copy candidates",
        "private_copy": "Approved private copies",
        "create_directory": "Directory containers",
        "exclude_administration": "Administration excluded",
        "needs_consent": "Consent needed",
        "needs_rebuild": "Rebuild needed",
        "needs_budget": "Budget decision needed",
        "needs_rescan": "Rescan needed",
        "refuse_symlink": "Symlinks refused",
        "refuse_special": "Special files refused",
        "refuse_nested_repository": "Nested repositories refused",
        "refuse_mount": "Mounts refused",
        "refuse_path_alias": "Path aliases refused",
    }
    remaining = 8
    for action in sorted(
        groups, key=lambda action: (not groups[action][0].requires_decision, action)
    ):
        group = groups[action]
        lines.append(f"  {len(group)} {labels.get(action, action.replace('_', ' '))}")
        samples = group[: min(2, remaining)]
        for entry in samples:
            lines.append(f"    {display(entry.path, 100)}: {display(entry.reason)}")
        remaining -= len(samples)
        if len(group) > len(samples):
            lines.append(f"    ... {len(group) - len(samples)} entries omitted")
    lines.extend(
        [
            "Full entries, observations and limits: "
            "loupe --json workspace hydrate PATH --plan",
            "Preview only; nothing copied. A fresh validated plan is required "
            "before files can be copied.",
        ]
    )
    return "\n".join(lines)


def _graph_neighbors_text(result: dict[str, Any]) -> str:
    if result.get("state") != "ready":
        return (
            "Code graph: "
            + str(result.get("state", "unavailable"))
            + (
                " — " + str(result.get("notice") or result.get("error"))
                if result.get("notice") or result.get("error")
                else ""
            )
        )
    nodes = {item["node_id"]: item for item in result.get("nodes", [])}
    lines = [f"Code graph: {result['node']} (generation {result['generation']})"]
    for item in result.get("edges", []):
        source = nodes.get(item["source"], {})
        target = nodes.get(item["target"], {})
        left = (
            source.get("relative_path")
            if source.get("kind") == "file"
            else source.get("name", item["source"])
        )
        right = (
            target.get("relative_path")
            if target.get("kind") == "file"
            else target.get("name", item["target"])
        )
        label = " [stale indexed version]" if item.get("stale") else ""
        lines.append(f"  {left} --{item['type']}--> {right}{label}")
        lines.append(
            f"    {item['path']}:{item['start_line']} {item['parser']} "
            f"{item['parser_version']} {item['indexed_revision']}"
        )
    if not result.get("edges"):
        lines.append("  No matching graph edges.")
    for failure in result.get("parse_failures", []):
        lines.append(
            f"  Parse skipped: {failure['relative_path']} ({failure['error_code']})"
        )
    if result.get("truncated"):
        lines.append("  Results capped; narrow --edge or --depth.")
    return "\n".join(lines)


def _lexical_search_text(result: dict[str, Any]) -> str:
    if result.get("state") != "ready":
        return "Lexical index: " + str(result.get("state", "unavailable")) + (
            "; " + str(result["error"]) if result.get("error") else ""
        )
    if not result.get("results"):
        return "No lexical matches."
    lines: list[str] = []
    for item in result["results"]:
        lines.extend((
            f"{item['path']}:{item['start_line']}-{item['end_line']} "
            f"[{item['label']}]",
            f"  revision {item['indexed_revision']}",
            f"  identity {item['content_identity']} generation {item['generation']}",
        ))
        if item.get("explain"):
            lines.append(f"  lexical: {item['explain']['reason']}")
        lines.extend("  " + line for line in str(item["text"]).splitlines())
        lines.append("")
    return "\n".join(lines).rstrip()


def _cutover_plan_text(value: Any) -> str:
    """Render every actionable record without repeating the raw evidence body."""
    if not isinstance(value, dict):
        raise LlmCoordError(
            ErrorCode.PROTOCOL_MISMATCH, "cutover preview response is not an object"
        )
    groups: dict[str, list[dict[str, Any]]] = {}
    for name in ("mappings", "dispositions", "blockers"):
        items = value.get(name)
        if not isinstance(items, list) or any(
            not isinstance(item, dict) for item in items
        ):
            raise LlmCoordError(
                ErrorCode.PROTOCOL_MISMATCH,
                f"cutover preview has no complete {name} inventory",
            )
        groups[name] = items

    def label(item: object) -> str:
        if item is None:
            return "unknown"
        if not isinstance(item, (str, int, bool)):
            raise LlmCoordError(
                ErrorCode.PROTOCOL_MISMATCH, "cutover preview label is not a scalar"
            )
        return str(item).replace("\r", "\\r").replace("\n", "\\n")

    lines = [
        "Cutover preview (read-only; apply is irreversible)",
        f"Regime: {label(value.get('regime'))}; "
        f"generation: {label(value.get('generation'))}; "
        f"ready: {label(value.get('ready'))}",
        f"Checkpoint: {label(value.get('checkpoint'))}",
        f"Mappings ({len(groups['mappings'])}):",
    ]
    for mapping in groups["mappings"]:
        provenance = mapping.get("provenance")
        if isinstance(provenance, list):
            provenance_text = f"{len(provenance)} registration records"
        else:
            provenance_text = label(provenance)
        lines.extend((
            f"  {label(mapping.get('repo_key', mapping.get('repository_id')))}: "
            f"target={label(mapping.get('target_ref'))}; "
            f"adapter={label(mapping.get('integration_adapter'))}",
            f"    path={label(mapping.get('canonical_path'))}; "
            f"provenance={provenance_text}",
        ))
    lines.append(f"Dispositions ({len(groups['dispositions'])}):")
    for item in groups["dispositions"]:
        allowed = item.get("allowed", [])
        if not isinstance(allowed, list):
            raise LlmCoordError(
                ErrorCode.PROTOCOL_MISMATCH, "cutover disposition has invalid actions"
            )
        actions = ", ".join(label(action) for action in allowed) or "none"
        lines.append(
            f"  {label(item.get('kind'))} {label(item.get('id'))}: "
            f"state={label(item.get('state'))}; default={label(item.get('default'))}; "
            f"allowed={actions}"
        )
    lines.append(f"Blockers ({len(groups['blockers'])}):")
    for item in groups["blockers"]:
        lines.append(
            f"  {label(item.get('kind'))} {label(item.get('id'))}: "
            f"{label(item.get('reason'))}"
        )
    lines.append(
        "Use loupe --json workspace cutover plan PATH for complete evidence."
    )
    return "\n".join(lines)


def _write_cutover_export(destination: Path, value: Any) -> dict[str, Any]:
    """Publish an explicit metadata export privately, without overwriting a path."""
    if not isinstance(value, dict):
        raise LlmCoordError(
            ErrorCode.PROTOCOL_MISMATCH, "cutover export response is not an object"
        )
    destination = destination.expanduser().absolute()
    pending: Path | None = None
    try:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=".loupe-cutover-",
            delete=False,
        ) as handle:
            pending = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        # link() refuses both existing files and symlinks, including a racing
        # writer. The temporary file is private (0600) before any bytes exist.
        os.link(pending, destination, follow_symlinks=False)
    except (OSError, TypeError, ValueError) as exc:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            f"could not write new private cutover export: {exc}",
        ) from exc
    finally:
        if pending is not None:
            with suppress(OSError):
                pending.unlink()
    return {"exported_to": str(destination), "metadata_only": True}


def dispatch(arguments: argparse.Namespace, client: DaemonClient) -> Any:
    command = arguments.command
    if command == "chat" or (
        command == "session" and arguments.session_command == "open"
    ):
        try:
            resolve_agent_mode(arguments.agent_mode, arguments.publish)
        except ValueError as exc:
            raise LlmCoordError(ErrorCode.CONFIG_INVALID, str(exc)) from exc
    if getattr(arguments, "agent_mode", None) is not None and not (
        command in {"chat", "run"}
        or (command == "session" and arguments.session_command == "open")
    ):
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            "--mode is available for chat, run, or session open",
        )
    if getattr(arguments, "effort", None) is not None and command != "chat":
        raise LlmCoordError(ErrorCode.CONFIG_INVALID, "--effort is available for chat")
    if command == "demo":
        from llm_cli.cli.demo import demo_events, run_demo

        if arguments.as_json:
            return {"simulated": True, "events": demo_events()}
        run_demo(plain=arguments.plain)
        return _RENDERED
    if command == "auth":
        return auth_command(arguments, client.paths)
    if command == "init":
        return client.call("system.init")
    if command == "doctor":
        return client.call("system.doctor")
    if command == "daemon":
        action = arguments.daemon_command
        if action == "start":
            return _with_code_match(client.call("system.ping"))
        if action == "status":
            return _with_code_match(client.call("system.ping", autostart=False))
        if action == "stop":
            return client.call("system.shutdown", autostart=False)
        if action == "restart":
            try:
                client.call("system.shutdown", autostart=False)
            except LlmCoordError as exc:
                if exc.code is not ErrorCode.DAEMON_UNAVAILABLE:
                    raise
            # The old daemon first drains running tasks; a successor started
            # before it exits is refused by the profile's singleton lock.
            settings = load_settings(
                client.paths.config_file, profile_id=client.paths.profile_id
            )
            if not client.wait_until_stopped(settings.shutdown_drain_ms / 1_000 + 15):
                raise LlmCoordError(
                    ErrorCode.DAEMON_UNAVAILABLE,
                    "the previous Loupe background service has not exited yet; "
                    f"run 'loupe daemon restart' again, or see {client.paths.log_file}",
                )
            return _with_code_match(client.call("system.ping"))
    if command == "db":
        if arguments.db_command == "backup":
            return client.call(
                "db.backup",
                {
                    "out": str(arguments.out.expanduser().absolute())
                    if arguments.out
                    else None
                },
            )
        if arguments.db_command == "prune":
            return client.call(
                "db.prune",
                {
                    "older_than": arguments.older_than,
                    "apply": arguments.apply,
                },
            )
        return client.call(f"db.{arguments.db_command}")
    if command == "index":
        index_params = (
            {"path": str(Path(arguments.repo).expanduser().resolve())}
            if arguments.repo
            else {}
        )
        return _patient(client, 90).call(
            f"index.{arguments.index_command}", index_params
        )
    if command == "search":
        result = client.call(
            "search.lexical",
            {
                "path": str(Path(arguments.repo).expanduser().resolve()),
                "query": arguments.query,
                "limit": arguments.limit,
                "explain": arguments.explain,
            },
        )
        return result if arguments.as_json else _lexical_search_text(result)
    if command == "graph":
        result = client.call(
            "graph.neighbors",
            {
                "path": str(Path(arguments.repo).expanduser().resolve()),
                "node": arguments.node,
                "edge": arguments.edge,
                "depth": arguments.depth,
            },
        )
        return result if arguments.as_json else _graph_neighbors_text(result)
    if command == "config":
        if arguments.config_command == "show":
            return client.call("config.show")
        return client.call(
            "config.set", {"key": arguments.key, "value": arguments.value}
        )
    if command == "repo":
        action = arguments.repo_command
        if action == "add":
            return client.call(
                "repo.add",
                {
                    "path": str(Path(arguments.path).absolute()),
                    "target": arguments.target,
                    "coordinate_by_remote": not arguments.local_only,
                },
            )
        if action == "list":
            return client.call("repo.list")
        if action == "status":
            return client.call(
                "repo.status", {"path": str(Path(arguments.path).absolute())}
            )
    if command == "run":
        if arguments.interactive and arguments.as_json:
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                "run --interactive cannot be combined with --json",
            )
        if arguments.interactive and arguments.claim_only:
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                "run --interactive cannot be combined with --claim-only",
            )
        if arguments.agent_mode is not None:
            return _run_in_mode(arguments, client)
        result = client.call(
            "task.run",
            {
                "title": arguments.task,
                "path": str(Path(arguments.repo).absolute()),
                "scopes": arguments.scope,
                "task_id": arguments.task_id,
                "fixture_writes": arguments.fixture_write or [],
                "claim_only": arguments.claim_only,
                "interactive": arguments.interactive,
                "provider": arguments.provider,
                "model": arguments.model,
                **(
                    session_environment_params(client.paths)
                    if not arguments.claim_only and not arguments.fixture_write
                    else {}
                ),
            },
            idempotency_key=arguments.task_id,
        )
        if (arguments.follow or arguments.interactive) and not arguments.claim_only:
            if not isinstance(result, dict) or not isinstance(result.get("task"), dict):
                raise LlmCoordError(
                    ErrorCode.PROTOCOL_MISMATCH, "task response has no task"
                )
            task_id = result["task"].get("task_id")
            if not isinstance(task_id, str):
                raise LlmCoordError(
                    ErrorCode.PROTOCOL_MISMATCH, "task response has no ID"
                )
            return _watch(client, task_id, 0, arguments.as_json, arguments.plain)
        return result
    if command == "workspace":
        if arguments.workspace_command == "authority":
            prune = arguments.authority_command == "prune"
            result = client.call(
                "workspace.authority.maintenance",
                {
                    "prune": prune,
                    "apply": prune and arguments.apply,
                    "include_unrecorded": prune and arguments.include_unrecorded,
                    "include_other_profiles": prune
                    and arguments.include_other_profiles,
                },
            )
            if arguments.as_json:
                return result
            lines = [f"Authority records: {result['root']}"]
            for record in result["records"]:
                action = "removed" if record["removed"] else record["reason"]
                path = record.get("checkout_path") or "path not recorded"
                profile = record.get("owner_profile") or "unknown"
                lines.append(
                    f"{record['physical_key']}: {action} ({path}; "
                    f"last owner profile {profile})"
                )
            if prune and not arguments.apply:
                lines.append("Dry run. Use --apply to remove eligible records.")
            return "\n".join(lines)
        if arguments.workspace_command == "cutover":
            action = arguments.cutover_command
            cutover_params: dict[str, Any] = {
                "path": str(Path(arguments.path).expanduser().absolute())
            }
            if action in {"apply", "resume", "recovery"}:
                cutover_params["checkpoint"] = arguments.checkpoint
                if arguments.intent is not None:
                    cutover_params["intent_id"] = arguments.intent
            if action == "resume" and arguments.accept_current_history is not None:
                cutover_params["accept_current_history"] = (
                    arguments.accept_current_history
                )
            if action == "apply":
                cutover_params["dispositions"] = {
                    claim_id: "cancel" for claim_id in arguments.cancel_claim
                }
            result = client.call(f"workspace.cutover.{action}", cutover_params)
            if action == "export":
                return _write_cutover_export(arguments.out, result)
            if action in {"plan", "recovery"} and not arguments.as_json:
                text = _cutover_plan_text(result)
                if action == "recovery":
                    text = text.replace(
                        "Cutover preview (read-only; apply is irreversible)",
                        "Cutover recovery preview (read-only; resume is forward-only)",
                        1,
                    )
                    text += (
                        "\nIntent: " + str(result["intent_id"])
                        + "\nRecovery checkpoint: " + str(result["recovery_checkpoint"])
                        + "\nOwnership gaps: " + str(len(result["ownership_gaps"]))
                    )
                    for gap in result["ownership_gaps"]:
                        text += (
                            "\n  " + str(gap["checkout_id"])
                            + " event " + str(gap["sequence"])
                            + "; previous profile="
                            + json.dumps(gap["previous_profile"])
                            + "; previous database="
                            + str(gap.get("previous_database_instance_id") or "unknown")
                        )
                    if result.get("pending_owner") is not None:
                        text += (
                            "\nCurrent foreign owner: "
                            + json.dumps(result["pending_owner"])
                            + "\nAfter that owner releases, run this without an "
                            "acknowledgement to record the ownership gap:\n  "
                            + result["reacquire_command"]
                            + "\nIt will stop for attention; then review a fresh "
                            "recovery preview before acknowledging its token."
                        )
                return text
            return result
        if arguments.workspace_command == "hydrate":
            from llm_cli.workspace.hydration import HydrationBudgets, plan_hydration

            profile_paths = getattr(client, "paths", None)
            plan = plan_hydration(
                Path(arguments.path),
                budgets=HydrationBudgets(
                    max_entries=arguments.max_entries,
                    max_metadata_bytes=arguments.max_metadata_bytes,
                    max_depth=arguments.max_depth,
                    max_file_bytes=arguments.max_file_bytes,
                    max_total_bytes=arguments.max_bytes,
                    max_allocated_bytes=arguments.max_allocated_bytes,
                    max_seconds=arguments.max_seconds,
                ),
                approved_secret_paths=arguments.approve_secret,
                rebuild=(
                    load_settings(
                        profile_paths.config_file, profile_id=profile_paths.profile_id
                    ).isolated_rebuild
                    if profile_paths is not None
                    else ()
                ),
            )
            return (
                plan.to_dict() if arguments.as_json else _hydration_preview_text(plan)
            )
        if arguments.workspace_command == "status":
            return client.call(
                "workspace.status",
                {"path": str(Path(arguments.path).absolute())},
            )
        if arguments.workspace_command == "observe":
            return client.call(
                "workspace.observe",
                {
                    "path": str(Path(arguments.repo).absolute()),
                    "relative_path": arguments.path,
                },
            )
        secret = session_resume_secret(client.paths, arguments.session_id)
        authenticated = {
            "session_id": arguments.session_id,
            "resume_secret": secret,
        }
        if arguments.workspace_command == "process":
            process_params = dict(authenticated)
            if arguments.process_command == "stop":
                process_params["process_id"] = arguments.process_id
            process_client = (
                _patient(client, 60)
                if arguments.process_command == "stop"
                else client
            )
            result = process_client.call(
                "workspace.process." + arguments.process_command, process_params
            )
            if arguments.as_json:
                return result
            records = (
                result["processes"] if arguments.process_command == "list" else [result]
            )
            if not records:
                return "No managed processes for this session."
            return "\n".join(
                f"{record['process_id']}: {record['state']} "
                f"(PID {record.get('pid') or 'not started'}). {record['stop_command']}"
                for record in records
            )
        if arguments.workspace_command == "runtime":
            # The daemon refuses symlinked destination ancestors so a link can't
            # be swapped in mid-copy. Resolve the user's own spelling first:
            # macOS /tmp and $TMPDIR (/var/folders) are symlinks.
            destination = arguments.destination.expanduser().absolute()
            destination = destination.parent.resolve() / destination.name
            result = client.call(
                "workspace.runtime.export",
                {
                    **authenticated,
                    "relative_path": arguments.path,
                    "destination": str(destination),
                    "yes": arguments.yes,
                    "allow_secret": arguments.allow_secret,
                    "into_checkout": arguments.into_checkout,
                },
            )
            if arguments.as_json:
                return result
            summary = (
                f"Runtime export: {result['kind']}, {result['bytes']} bytes, "
                f"{result['entries']} entries, {result['sensitivity']}."
            )
            if result["state"] == "exported":
                return summary + " Exported to the new local destination."
            required = [
                flag
                for key, flag in (
                    ("requires_yes", "--yes"),
                    ("requires_allow_secret", "--allow-secret"),
                )
                if result[key]
            ]
            return summary + " Preview only; requires " + " and ".join(required) + "."
        if arguments.workspace_command == "read":
            return client.call(
                "workspace.read_file",
                {**authenticated, "relative_path": arguments.path},
            )
        if arguments.workspace_command == "stage":
            try:
                content = arguments.content_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID,
                    f"could not read candidate content file: {exc}",
                ) from exc
            observed = client.call(
                "workspace.read_file",
                {**authenticated, "relative_path": arguments.path},
            )
            if not isinstance(observed, dict) or not isinstance(
                observed.get("identity"), dict
            ):
                raise LlmCoordError(
                    ErrorCode.PROTOCOL_MISMATCH,
                    "workspace read response has no identity",
                )
            stage_params: dict[str, Any] = {
                **authenticated,
                "candidate_id": arguments.candidate_id or new_id("candidate"),
                "relative_path": arguments.path,
                "base": observed["identity"],
                "content": content,
            }
            if "workspace_binding_generation" in observed:
                stage_params["workspace_binding_generation"] = observed[
                    "workspace_binding_generation"
                ]
            if arguments.executable:
                stage_params["executable"] = True
            return client.call(
                "workspace.stage_file",
                stage_params,
            )
        if arguments.workspace_command == "publish":
            return client.call(
                "workspace.publish_candidate",
                {**authenticated, "candidate_id": arguments.candidate_id},
            )
    if command == "checks":
        params: dict[str, Any] = {"path": str(Path(arguments.repo).absolute())}
        if arguments.checks_command == "configure":
            import tomllib

            try:
                with Path(arguments.file).open("rb") as handle:
                    params["config"] = tomllib.load(handle)
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID,
                    f"could not read checks configuration: {exc}",
                ) from exc
        return client.call("checks." + arguments.checks_command, params)
    if command == "chat":
        run_session(
            client,
            repository=Path(arguments.repo).absolute(),
            scopes=list(arguments.scope or ["*"]),
            provider=arguments.provider,
            model=arguments.model,
            resume_session_id=arguments.resume,
            workspace_mode=arguments.workspace,
            approved_runtime_files=tuple(arguments.approve_secret),
            publication_mode=arguments.publish,
            agent_mode=arguments.agent_mode,
            effort=arguments.effort,
            plain=arguments.plain,
        )
        return _RENDERED
    if command == "session":
        return _dispatch_session(arguments, client)
    if command == "task":
        if arguments.task_command in {"diff", "checks", "apply", "undo", "cancel"}:
            caller = (
                _patient(client, APPLY_TIMEOUT_SECONDS)
                if arguments.task_command == "apply"
                else client
            )
            return caller.call(
                "task." + arguments.task_command,
                {
                    "task_id": arguments.task_id,
                    **(
                        {
                            "allow_unverified": arguments.allow_unverified,
                            **session_environment_params(client.paths),
                        }
                        if arguments.task_command == "apply"
                        else {}
                    ),
                },
            )
        if arguments.task_command == "list":
            return client.call("task.list")
        if arguments.task_command == "show":
            return client.call("task.show", {"task_id": arguments.task_id})
        if arguments.task_command == "events":
            return client.call(
                "task.events",
                {
                    "task_id": arguments.task_id,
                    "after_sequence": arguments.after,
                    "limit": arguments.limit,
                },
            )
        if arguments.task_command == "watch":
            return _watch(
                client,
                arguments.task_id,
                arguments.after,
                arguments.as_json,
                arguments.plain,
            )
        if arguments.task_command == "retry":
            return client.call("task.retry", {"task_id": arguments.task_id})
        if arguments.task_command == "discard":
            return client.call("task.discard", {"task_id": arguments.task_id})
        if arguments.task_command == "recover":
            return client.call("task.recover")
    if command == "claim":
        action = arguments.claim_command
        if action == "list":
            return client.call(
                "claim.list", {"path": str(Path(arguments.repo).absolute())}
            )
        if action == "show":
            return client.call("claim.show", {"claim_id": arguments.claim_id})
        if action == "release":
            return _legacy_claim_result(
                client.call(
                    "claim.release",
                    {"claim_id": arguments.claim_id, "reason": arguments.reason},
                ),
                as_json=arguments.as_json,
            )
        if action == "renew":
            return _legacy_claim_result(
                client.call(
                    "claim.renew",
                    {
                        "task_id": arguments.task,
                        "claim_id": arguments.claim_id,
                        "fencing_token": arguments.fence,
                        "attempt": arguments.attempt,
                    },
                ),
                as_json=arguments.as_json,
            )
        if action == "reconcile":
            return client.call("claim.reconcile")
    raise LlmCoordError(ErrorCode.CONFIG_INVALID, "unsupported command")


def _run_in_mode(arguments: argparse.Namespace, client: DaemonClient) -> object:
    """Run one prompt in an explicit, resumable shared-checkout conversation."""

    if arguments.claim_only or arguments.fixture_write:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            "run --mode cannot be combined with --claim-only or --fixture-write",
        )
    if arguments.task_id:
        try:
            client.call("task.show", {"task_id": arguments.task_id})
        except LlmCoordError as exc:
            if exc.code is not ErrorCode.REPOSITORY_NOT_FOUND:
                raise
        else:
            raise LlmCoordError(
                ErrorCode.TASK_NOT_MUTABLE,
                "That task ID already exists. Continue it with "
                + shlex.join(
                    [
                        "loupe",
                        "--profile",
                        client.paths.profile_id,
                        "task",
                        "watch",
                        arguments.task_id,
                    ]
                ),
            )
    repository = Path(arguments.repo).absolute()
    credentials = open_or_resume_session(
        client,
        repository=repository,
        provider=arguments.provider,
        model=arguments.model,
        resume_session_id=None,
        agent_mode=arguments.agent_mode,
    )
    resume_args = [
        "loupe",
        "--profile",
        client.paths.profile_id,
        "chat",
        "--repo",
        str(repository),
        "--resume",
        credentials.session_id,
    ]
    for scope in arguments.scope:
        resume_args.extend(["--scope", scope])
    resume = shlex.join(resume_args)
    task_id = arguments.task_id or new_id("task")
    try:
        result = client.call(
            "task.run",
            {
                "title": arguments.task,
                "path": str(repository),
                "scopes": arguments.scope,
                "task_id": task_id,
                "interactive": arguments.interactive,
                "session_id": credentials.session_id,
                "resume_secret": credentials.resume_secret,
                "mode": credentials.agent_mode,
                "provider": credentials.provider,
                "model": credentials.model,
            },
            idempotency_key=task_id,
        )
        if not isinstance(result, dict):
            raise LlmCoordError(
                ErrorCode.PROTOCOL_MISMATCH, "task response is malformed"
            )
        if arguments.follow or arguments.interactive:
            return _watch(client, task_id, 0, arguments.as_json, arguments.plain)
        return {
            **result,
            "session_id": credentials.session_id,
            "agent_mode": credentials.agent_mode,
            "resume_command": resume,
        }
    finally:
        # A lost response or detached viewer must leave a discoverable session.
        # Keep its native context for turning a plan into an implementation.
        TerminalUI(sys.stderr, plain=arguments.plain).notice("Resume with: " + resume)


def _with_code_match(status: Any) -> Any:
    """Say whether the daemon runs the same Loupe code as this command."""

    if isinstance(status, dict):
        status = {
            **status,
            "matches_this_cli": status.get("code_fingerprint")
            == code_identity()["fingerprint"],
        }
    return status


def _dispatch_session(arguments: argparse.Namespace, client: DaemonClient) -> Any:
    action = arguments.session_command
    if action == "open":
        credentials = open_or_resume_session(
            client,
            repository=Path(arguments.repo).absolute(),
            provider=arguments.provider,
            model=arguments.model,
            resume_session_id=None,
            workspace_mode=arguments.workspace,
            approved_runtime_files=tuple(arguments.approve_secret),
            publication_mode=arguments.publish,
            agent_mode=arguments.agent_mode,
        )
        return {
            "session_id": credentials.session_id,
            "provider": credentials.provider,
            "model": credentials.model,
            "cursor": credentials.cursor,
            "agent_mode": credentials.agent_mode,
            "workspace_mode": credentials.workspace_mode,
            "workspace_notices": list(credentials.workspace_notices),
        }
    if action == "list":
        params: dict[str, Any] = {"state": arguments.state}
        if arguments.repo is not None:
            params["path"] = str(Path(arguments.repo).absolute())
        return client.call("session.list", params)
    if action == "show":
        return client.call("session.show", {"session_id": arguments.session_id})
    if action == "switch":
        return switch_session_workspace(
            client,
            arguments.session_id,
            workspace=arguments.workspace,
            disposition=arguments.disposition,
            approved_runtime_files=tuple(arguments.approve_secret),
            switch_intent_id=arguments.switch_intent_id,
            confirm_intent_id=arguments.confirm,
            rollback_intent_id=arguments.rollback,
            sequence=arguments.sequence,
        )
    secret = session_resume_secret(client.paths, arguments.session_id)
    authenticated = {
        "session_id": arguments.session_id,
        "resume_secret": secret,
    }
    if action == "resume":
        return _runtime_open_client(client, None, resume=True).call(
            "session.resume",
            {**authenticated, **session_environment_params(client.paths)},
        )
    if action == "close":
        result = _patient(client, 60).call(
            "session.close", {**authenticated, "reason": arguments.reason}
        )
        remove_session_secret(client.paths, arguments.session_id)
        return result
    if action == "events":
        return client.call(
            "session.events",
            {**authenticated, "after": arguments.after, "limit": arguments.limit},
        )
    if action == "watch":
        return _watch_session(client, authenticated, arguments.after, arguments.as_json)
    if action == "intent":
        if arguments.intent_command == "set":
            return client.call(
                "session.set_intent",
                {
                    **authenticated,
                    "paths": arguments.paths,
                    "summary": arguments.summary,
                },
            )
        if arguments.intent_command == "clear":
            return client.call("session.clear_intent", authenticated)
        if arguments.intent_command == "list":
            return client.call("session.intents", authenticated)
    raise LlmCoordError(ErrorCode.CONFIG_INVALID, "unsupported session command")


def _watch(
    client: DaemonClient, task_id: str, after: int, as_json: bool, plain: bool = False
) -> object:
    """Follow one task's events, rendering each as the daemon writes it.

    Ctrl-C detaches the viewer without touching the task: the work belongs to
    the daemon, not to whoever happens to be watching.
    """

    if not as_json:
        _follow(
            client,
            task_id=task_id,
            stream=sys.stdout,
            input_stream=sys.stdin,
            plain=plain,
            after=after,
        )
        return _RENDERED
    cursor = after
    try:
        while True:
            for event in client.stream(
                "task.attach", {"task_id": task_id, "after": cursor}
            ):
                sequence = event.get("sequence")
                if isinstance(sequence, int):
                    cursor = sequence
                json.dump(event, sys.stdout, ensure_ascii=False, separators=(",", ":"))
                sys.stdout.write("\n")
                sys.stdout.flush()
            task = client.call("task.show", {"task_id": task_id})
            if not isinstance(task, dict) or task.get("state") in {
                "awaiting_review",
                "reviewing",
                "completed",
                "failed",
                "cancelled",
                "ready_for_integration",
                "operator_attention",
            }:
                break
    except KeyboardInterrupt:
        pass
    return _RENDERED


def _watch_session(
    client: DaemonClient,
    authenticated: dict[str, str],
    after: int,
    as_json: bool,
) -> object:
    """Follow a checkout stream and acknowledge the exact delivered suffix."""

    cursor = after
    try:
        for event in client.stream("session.attach", {**authenticated, "after": after}):
            sequence = event.get("sequence")
            if isinstance(sequence, int):
                cursor = sequence
            if as_json:
                json.dump(event, sys.stdout, ensure_ascii=False, separators=(",", ":"))
                sys.stdout.write("\n")
            else:
                line = checkout_event_text(
                    event, own_session_id=authenticated["session_id"]
                )
                if line is not None:
                    print(line)
            sys.stdout.flush()
    except KeyboardInterrupt:
        if not as_json:
            print("  (detached; the session remains active)")
    finally:
        if cursor > after:
            with suppress(LlmCoordError):
                client.call("session.ack", {**authenticated, "sequence": cursor})
    return _RENDERED


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        if arguments.as_json:
            parser.error("--json requires a command; use task watch for streaming JSON")
        if not sys.stdin.isatty():
            parser.print_help()
            return
        # Apply chat defaults while preserving global options.
        defaults = parser.parse_args(["chat"])
        for key, value in vars(defaults).items():
            if key not in {"profile", "as_json", "plain", "agent_mode", "effort"}:
                setattr(arguments, key, value)
    if arguments.command == "chat" and arguments.as_json:
        parser.error("chat is interactive; use run or task watch with --json")
    try:
        paths = AppPaths.resolve(arguments.profile)
        client = DaemonClient(paths)
        result = dispatch(arguments, client)
        if result is not _RENDERED:
            emit(result, as_json=arguments.as_json, plain=arguments.plain)
    except LlmCoordError as exc:
        if arguments.as_json:
            json.dump(
                {
                    "ok": False,
                    "error": {
                        "code": exc.code.value,
                        "message": exc.message,
                        "details": exc.details,
                    },
                },
                sys.stdout,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            sys.stdout.write("\n")
        else:
            ui = TerminalUI(sys.stderr, plain=arguments.plain)
            ui.error(f"{exc.code.value}: {exc.message}")
            if exc.code is ErrorCode.REPOSITORY_NOT_FOUND:
                ui.notice("Register this repository with: loupe repo add .")
            if exc.code is ErrorCode.PROVIDER_UNAVAILABLE:
                ui.notice(
                    "Check provider credentials. For ChatGPT: loupe auth login codex"
                )
        raise SystemExit(EXIT_BY_ERROR.get(exc.code, 70)) from exc
    except ExitRequested:
        # The standalone question editor uses the same double-Ctrl+C gesture
        # as chat. Leaving its viewer must not cancel the daemon-owned task.
        raise SystemExit(0) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except BrokenPipeError:
        # A consumer such as `head` closed its pipe; avoid a shutdown traceback.
        sys.stdout = open("/dev/null", "w")  # noqa: SIM115
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()
