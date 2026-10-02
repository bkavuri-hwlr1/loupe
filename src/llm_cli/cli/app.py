"""Stable command-line surface for the initial control-plane foundation."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from llm_cli import __version__
from llm_cli.agent.modes import AGENT_MODES, resolve_agent_mode
from llm_cli.build import code_identity
from llm_cli.cli.auth import auth_command
from llm_cli.cli.exit_codes import EXIT_BY_ERROR
from llm_cli.cli.interrupts import ExitRequested
from llm_cli.cli.output import emit
from llm_cli.cli.render import checkout_event_text
from llm_cli.cli.session import (
    _follow,
    open_or_resume_session,
    remove_session_secret,
    run_session,
    session_resume_secret,
)
from llm_cli.cli.terminal import TerminalUI
from llm_cli.config.loader import load_settings
from llm_cli.coordination.models import EFFORT_LEVELS
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.ids import new_id
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient

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

    claim = commands.add_parser("claim", help="inspect and operate durable claims")
    claim_commands = claim.add_subparsers(dest="claim_command", required=True)
    list_claims = claim_commands.add_parser("list")
    list_claims.add_argument("--repo", default=".")
    show_claim = claim_commands.add_parser("show")
    show_claim.add_argument("claim_id")
    release = claim_commands.add_parser("release")
    release.add_argument("claim_id")
    release.add_argument("--reason", required=True)
    renew = claim_commands.add_parser("renew")
    renew.add_argument("claim_id")
    renew.add_argument("--task", required=True)
    renew.add_argument("--fence", type=int, required=True)
    renew.add_argument("--attempt", type=int, default=1)
    claim_commands.add_parser("reconcile")
    return parser


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
        return client.call(f"db.{arguments.db_command}")
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
        if arguments.workspace_command == "status":
            return client.call(
                "workspace.status",
                {"path": str(Path(arguments.path).absolute())},
            )
        secret = session_resume_secret(client.paths, arguments.session_id)
        authenticated = {
            "session_id": arguments.session_id,
            "resume_secret": secret,
        }
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
            return client.call(
                "task." + arguments.task_command,
                {
                    "task_id": arguments.task_id,
                    **(
                        {"allow_unverified": arguments.allow_unverified}
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
            return client.call(
                "claim.release",
                {"claim_id": arguments.claim_id, "reason": arguments.reason},
            )
        if action == "renew":
            return client.call(
                "claim.renew",
                {
                    "task_id": arguments.task,
                    "claim_id": arguments.claim_id,
                    "fencing_token": arguments.fence,
                    "attempt": arguments.attempt,
                },
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
            publication_mode=arguments.publish,
            agent_mode=arguments.agent_mode,
        )
        return {
            "session_id": credentials.session_id,
            "provider": credentials.provider,
            "model": credentials.model,
            "cursor": credentials.cursor,
            "agent_mode": credentials.agent_mode,
        }
    if action == "list":
        params: dict[str, Any] = {"state": arguments.state}
        if arguments.repo is not None:
            params["path"] = str(Path(arguments.repo).absolute())
        return client.call("session.list", params)
    if action == "show":
        return client.call("session.show", {"session_id": arguments.session_id})
    secret = session_resume_secret(client.paths, arguments.session_id)
    authenticated = {
        "session_id": arguments.session_id,
        "resume_secret": secret,
    }
    if action == "resume":
        return client.call("session.resume", authenticated)
    if action == "close":
        result = client.call(
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
