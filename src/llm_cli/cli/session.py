"""A durable top-level terminal session over short, fenced task attempts."""

from __future__ import annotations

import hashlib
import os
import secrets
import shlex
import sys
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO

from llm_cli.agent.modes import resolve_agent_mode, validate_agent_mode
from llm_cli.cli.active_controls import active_controls
from llm_cli.cli.composer import Composer
from llm_cli.cli.interrupts import EXIT_HINT
from llm_cli.cli.render import EventRenderer, checkout_event_text, question_text
from llm_cli.cli.status import TaskStatusUI
from llm_cli.cli.terminal import TerminalUI
from llm_cli.coordination.models import EFFORT_LEVELS
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.ids import new_id
from llm_cli.paths import AppPaths, reject_symlink_components
from llm_cli.protocol.client import DaemonClient


@dataclass(frozen=True, slots=True)
class _SessionCredentials:
    session_id: str
    resume_secret: str
    provider: str
    model: str
    cursor: int
    workspace_mode: str = "shared"
    effort: str | None = None
    agent_mode: str = "auto"


def run_session(
    client: DaemonClient,
    *,
    repository: Path,
    scopes: list[str],
    provider: str | None = None,
    model: str | None = None,
    resume_session_id: str | None = None,
    workspace_mode: str | None = None,
    publication_mode: str | None = None,
    agent_mode: str | None = None,
    effort: str | None = None,
    input_stream: TextIO | None = None,
    output: TextIO | None = None,
    plain: bool = False,
) -> int:
    """Open Loupe immediately; connect accounts and the daemon on demand."""

    from llm_cli.cli.shell import ChatShell

    return ChatShell(
        client,
        repository=repository,
        scopes=scopes,
        provider=provider,
        model=model,
        resume_session_id=resume_session_id,
        workspace_mode=workspace_mode,
        publication_mode=publication_mode,
        agent_mode=agent_mode,
        effort=effort,
        stdin=input_stream if input_stream is not None else sys.stdin,
        stream=output if output is not None else sys.stdout,
        plain=plain,
    ).run()


# A request the daemon validated and refused cannot have created a session;
# anything else -- a lost connection, an unavailable daemon, an internal fault
# -- leaves the outcome genuinely unknown.
_DETERMINISTIC_OPEN_REFUSALS = frozenset(
    {
        ErrorCode.CONFIG_INVALID,
        ErrorCode.REPOSITORY_NOT_FOUND,
        ErrorCode.REPOSITORY_UNSAFE,
        ErrorCode.PROTOCOL_MISMATCH,
    }
)


def open_or_resume_session(
    client: DaemonClient,
    *,
    repository: Path,
    provider: str | None,
    model: str | None,
    resume_session_id: str | None,
    workspace_mode: str | None = None,
    publication_mode: str | None = None,
    agent_mode: str | None = None,
    effort: str | None = None,
) -> _SessionCredentials:
    if resume_session_id is not None:
        secret = session_resume_secret(client.paths, resume_session_id)
        resumed = client.call(
            "session.resume",
            {"session_id": resume_session_id, "resume_secret": secret},
        )
        session = _object(resumed, "session")
        cursor = _object(resumed, "cursor")
        credentials = _SessionCredentials(
            session_id=resume_session_id,
            resume_secret=secret,
            provider=_text(session, "provider"),
            model=_text(session, "model"),
            cursor=_integer(cursor, "transport_received_sequence"),
            workspace_mode=str(session.get("workspace_mode", "shared")),
            effort=_optional_effort(session),
            agent_mode=_session_mode(session),
        )
        if agent_mode is not None or publication_mode is not None:
            selected = resolve_agent_mode(agent_mode, publication_mode)
            if selected != credentials.agent_mode:
                credentials = set_session_mode(client, credentials, selected)
        return credentials

    selected_mode = resolve_agent_mode(agent_mode, publication_mode)
    session_id = new_id("session")
    secret = secrets.token_urlsafe(32)
    _write_secret(client.paths, session_id, secret)
    try:
        opened = client.call(
            "session.open",
            {
                "session_id": session_id,
                "resume_token_hash": _secret_hash(secret),
                "path": str(repository),
                "provider": provider,
                "model": model,
                "workspace": workspace_mode,
                "mode": selected_mode,
                **({"effort": effort} if effort is not None else {}),
            },
            idempotency_key=session_id,
        )
    except LlmCoordError as exc:
        if exc.code in _DETERMINISTIC_OPEN_REFUSALS:
            # The daemon rejected the request before committing anything, so
            # there is no session to resume. Pointing the operator at one would
            # send them after a session that does not exist.
            remove_session_secret(client.paths, session_id)
            raise
        # Otherwise the server may have committed the open before the client
        # lost its response. Keep the only resume secret and make the safe
        # recovery path explicit instead of silently allocating a second
        # session.
        raise LlmCoordError(
            exc.code,
            f"session open outcome is unknown; retry with --resume {session_id}",
            exc.details,
        ) from exc
    session = _object(opened, "session")
    try:
        _verify_session_mode(session, selected_mode)
    except LlmCoordError as exc:
        raise LlmCoordError(
            exc.code, f"{exc.message} Session available with --resume {session_id}."
        ) from exc
    if effort is not None and session.get("effort") != effort:
        # An older daemon can accept unknown fields yet silently discard them.
        # Keep the resume secret because opening may already be committed.
        raise LlmCoordError(
            ErrorCode.PROTOCOL_MISMATCH,
            "the daemon did not apply your effort setting; after active tasks "
            "finish, run 'loupe daemon restart' and reopen Loupe. "
            f"The unused session can be resumed with --resume {session_id}",
        )
    bootstrap_sequence = _integer(opened, "bootstrap_sequence")
    acknowledged = client.call(
        "session.ack",
        {
            "session_id": session_id,
            "resume_secret": secret,
            "sequence": bootstrap_sequence,
        },
    )
    cursor = _object(acknowledged, "cursor")
    return _SessionCredentials(
        session_id=session_id,
        resume_secret=secret,
        provider=_text(session, "provider"),
        model=_text(session, "model"),
        cursor=_integer(cursor, "transport_received_sequence"),
        workspace_mode=str(session.get("workspace_mode", workspace_mode or "shared")),
        effort=_optional_effort(session),
        agent_mode=_session_mode(session),
    )


def _session_mode(value: dict[str, Any]) -> str:
    try:
        return validate_agent_mode(value.get("agent_mode", "auto"))
    except ValueError as exc:
        raise LlmCoordError(
            ErrorCode.PROTOCOL_MISMATCH, "session mode is malformed"
        ) from exc


def _verify_session_mode(value: dict[str, Any], expected: str) -> None:
    # Old daemons can silently ignore new request fields. Never submit a plan
    # under a daemon that has not confirmed the requested restrictions.
    if value.get("agent_mode") != expected:
        raise LlmCoordError(
            ErrorCode.PROTOCOL_MISMATCH,
            "The daemon did not confirm the requested mode. After active tasks "
            "finish, run 'loupe daemon restart' and reopen Loupe.",
        )


def set_session_mode(
    client: DaemonClient, credentials: _SessionCredentials, mode: str
) -> _SessionCredentials:
    selected = validate_agent_mode(mode)
    response = client.call(
        "session.set_mode",
        {
            "session_id": credentials.session_id,
            "resume_secret": credentials.resume_secret,
            "mode": selected,
        },
    )
    value = _object(response, "session")
    _verify_session_mode(value, selected)
    return replace(credentials, agent_mode=selected)


def _optional_effort(session: dict[str, Any]) -> str | None:
    value = session.get("effort")
    if value is None:
        return None
    if not isinstance(value, str) or value not in EFFORT_LEVELS:
        raise LlmCoordError(ErrorCode.PROTOCOL_MISMATCH, "session effort is malformed")
    return value


def _set_intent(
    client: DaemonClient, credentials: _SessionCredentials, scopes: list[str]
) -> None:
    client.call(
        "session.set_intent",
        {
            "session_id": credentials.session_id,
            "resume_secret": credentials.resume_secret,
            "paths": scopes,
        },
    )


def _refresh_changes(
    client: DaemonClient,
    credentials: _SessionCredentials,
    stream: TextIO,
    *,
    report_empty: bool = False,
) -> _SessionCredentials:
    cursor = credentials.cursor
    displayed = False
    while True:
        events = client.call(
            "session.events",
            {
                "session_id": credentials.session_id,
                "resume_secret": credentials.resume_secret,
                "after": cursor,
                "limit": 100,
            },
        )
        if not isinstance(events, list):
            raise LlmCoordError(
                ErrorCode.PROTOCOL_MISMATCH, "session events are malformed"
            )
        if not events:
            break
        for event in events:
            if not isinstance(event, dict):
                raise LlmCoordError(
                    ErrorCode.PROTOCOL_MISMATCH, "session event is malformed"
                )
            sequence = event.get("sequence")
            if not isinstance(sequence, int):
                raise LlmCoordError(
                    ErrorCode.PROTOCOL_MISMATCH, "session event has no sequence"
                )
            cursor = sequence
            line = checkout_event_text(event, own_session_id=credentials.session_id)
            if line is not None:
                stream.write(line + "\n")
                displayed = True
        if len(events) < 100:
            break
    if cursor != credentials.cursor:
        client.call(
            "session.ack",
            {
                "session_id": credentials.session_id,
                "resume_secret": credentials.resume_secret,
                "sequence": cursor,
            },
        )
    if report_empty and not displayed:
        stream.write("  No new checkout changes.\n")
    if cursor != credentials.cursor or report_empty:
        stream.flush()
    return replace(credentials, cursor=cursor)


def _run_one(
    client: DaemonClient,
    *,
    repository: Path,
    scopes: list[str],
    instruction: str,
    credentials: _SessionCredentials,
    stream: TextIO,
    input_stream: TextIO,
    plain: bool = False,
    composer: Composer | None = None,
) -> str | None:
    task_id = new_id("chat")
    ui = TerminalUI(stream, plain=plain)
    try:
        client.call(
            "task.run",
            {
                "title": instruction,
                "path": str(repository),
                "scopes": scopes,
                "task_id": task_id,
                "fixture_writes": [],
                "interactive": True,
                "provider": credentials.provider,
                "model": credentials.model,
                "mode": credentials.agent_mode,
                "session_id": credentials.session_id,
                "resume_secret": credentials.resume_secret,
            },
            idempotency_key=task_id,
        )
    except LlmCoordError as exc:
        ui.error(exc.message)
        if (exc.details or {}).get("agent_mode") in {"plan", "normal", "auto"}:
            ui.notice("Use /mode to refresh this session's mode, then submit again.")
            return None
        active_task_id = (exc.details or {}).get("active_task_id")
        if exc.code is ErrorCode.TASK_NOT_MUTABLE and isinstance(active_task_id, str):
            ui.notice(f"Follow the running task with /attach {active_task_id}.")
            return active_task_id
        ui.notice(f"Inspect or reconnect with /attach {task_id}.")
        # A lost response may follow a durable acceptance. Retain the known ID
        # so exit checks the task before closing its parent session.
        return task_id
    except KeyboardInterrupt as exc:
        if composer is not None:
            composer.interrupts.handle(exc)
            ui.notice(EXIT_HINT)
        ui.notice(f"Submission interrupted; check its outcome with /attach {task_id}.")
        return task_id

    try:
        _follow(
            client,
            task_id=task_id,
            stream=stream,
            input_stream=input_stream,
            plain=plain,
            composer=composer,
        )
    except KeyboardInterrupt as exc:
        if composer is not None:
            composer.interrupts.handle(exc)
            ui.notice(EXIT_HINT)
        ui.notice(f"Detached; reconnect with /attach {task_id}.")
    return task_id


def _follow(
    client: DaemonClient,
    *,
    task_id: str,
    stream: TextIO,
    input_stream: TextIO,
    plain: bool = False,
    composer: Composer | None = None,
    after: int = 0,
) -> None:
    """Render one task's durable stream, answering questions as they arrive."""

    with TaskStatusUI(
        stream,
        plain=plain,
        status=composer.status_text if composer is not None else None,
    ) as ui:
        _follow_with_status(
            client,
            task_id=task_id,
            stream=stream,
            input_stream=input_stream,
            plain=plain,
            composer=composer,
            after=after,
            ui=ui,
        )


def _follow_with_status(
    client: DaemonClient,
    *,
    task_id: str,
    stream: TextIO,
    input_stream: TextIO,
    plain: bool,
    composer: Composer | None,
    after: int,
    ui: TaskStatusUI,
) -> None:
    cursor = after
    renderer = EventRenderer(stream, plain=plain)
    renderer.ui = ui
    reconnect = _reattach_command(client, task_id, composer)
    while True:
        pending: tuple[str, str] | None = None
        try:
            with active_controls(
                client,
                task_id,
                input_stream,
                stream,
                interrupts=composer.interrupts if composer is not None else None,
                notice=ui.notice,
            ):
                for event in _events(client, task_id=task_id, after=cursor):
                    sequence = event.get("sequence")
                    if isinstance(sequence, int):
                        cursor = sequence
                    question = question_text(event)
                    if question is not None:
                        current = client.call("task.question", {"task_id": task_id})
                        payload = event.get("payload", {})
                        question_id = payload.get("question_id")
                        if (
                            isinstance(current, dict)
                            and current.get("pending")
                            and isinstance(question_id, str)
                            and current.get("question_id") == question_id
                        ):
                            pending = (question, question_id)
                            break
                    renderer.render(event)
        except LlmCoordError as exc:
            renderer.finish()
            ui.error(exc.message)
            ui.notice(f"Reconnect with {reconnect}")
            return
        except KeyboardInterrupt as exc:
            renderer.finish()
            if composer is not None:
                composer.interrupts.handle(exc)
                ui.notice(EXIT_HINT)
            ui.notice(f"Detached; the task keeps running. {reconnect}")
            return
        if pending is None:
            # The transport ends on idle timeout as well as task completion.
            # Reconnect from the last sequence if a quiet model is still busy.
            try:
                task = client.call("task.show", {"task_id": task_id})
            except LlmCoordError as exc:
                renderer.finish()
                ui.error(exc.message)
                return
            if not isinstance(task, dict) or task.get("state") in {
                "awaiting_review",
                "reviewing",
                "completed",
                "failed",
                "cancelled",
                "ready_for_integration",
                "operator_attention",
            }:
                renderer.finish()
                return
            continue
        renderer.finish()
        # The question editor owns its own toolbar and terminal redraws.
        ui.stop()
        answered = _answer(
            client,
            task_id=task_id,
            question=pending[0],
            question_id=pending[1],
            stream=stream,
            input_stream=input_stream,
            composer=composer,
            plain=plain,
        )
        if not answered:
            return
        ui.start()


def _answer(
    client: DaemonClient,
    *,
    task_id: str,
    question: str,
    stream: TextIO,
    input_stream: TextIO,
    question_id: str | None = None,
    composer: Composer | None = None,
    plain: bool = False,
) -> bool:
    ui = TerminalUI(stream, plain=plain)
    ui.notice(f"Loupe needs your input\n{question}")
    ui.notice("Type your answer, /stop to cancel, or Ctrl+C to answer later.")
    reconnect = _reattach_command(client, task_id, composer)
    editor = composer or Composer(input_stream, stream, plain=plain)
    try:
        while True:
            reply = editor.read(answer=True).strip()
            if reply:
                break
            ui.notice("Enter an answer, or Ctrl-C to leave the question pending.")
    except (KeyboardInterrupt, EOFError) as exc:
        if isinstance(exc, KeyboardInterrupt):
            editor.interrupts.handle(exc)
            ui.notice(EXIT_HINT)
        ui.notice(f"Question left pending. Reconnect with {reconnect}")
        return False
    try:
        if reply == "/stop":
            client.call("task.cancel", {"task_id": task_id})
            ui.notice("Stop requested; pending edits will be retained.")
            return False
        params = {"task_id": task_id, "answer": reply}
        if question_id is not None:
            params["question_id"] = question_id
        client.call("task.answer", params)
    except LlmCoordError as exc:
        ui.error(exc.message)
        ui.notice(f"Check the current question with {reconnect}")
        return False
    except KeyboardInterrupt as exc:
        editor.interrupts.handle(exc)
        ui.notice(EXIT_HINT)
        ui.notice(f"Answer delivery interrupted. Check with {reconnect}")
        return False
    return True


def _reattach_command(
    client: DaemonClient, task_id: str, composer: Composer | None
) -> str:
    if composer is not None:
        return f"/attach {task_id}"
    return shlex.join(
        ["loupe", "--profile", client.paths.profile_id, "task", "watch", task_id]
    )


def _events(
    client: DaemonClient, *, task_id: str, after: int
) -> Iterator[dict[str, Any]]:
    return client.stream("task.attach", {"task_id": task_id, "after": after})


def _secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _write_secret(paths: AppPaths, session_id: str, secret: str) -> None:
    paths.ensure()
    target = paths.session_secret_file(session_id)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise LlmCoordError(
            ErrorCode.INTERNAL_RECOVERABLE, "session resume-secret file already exists"
        ) from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(secret)
        handle.flush()
        os.fsync(handle.fileno())


def session_resume_secret(paths: AppPaths, session_id: str) -> str:
    target = paths.session_secret_file(session_id)
    reject_symlink_components(target)
    try:
        stat = target.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise LlmCoordError(
            ErrorCode.SESSION_AUTH_REQUIRED,
            "this profile has no locally retained resume secret for that session",
        ) from exc
    if not target.is_file() or stat.st_mode & 0o077:
        raise LlmCoordError(
            ErrorCode.SESSION_AUTH_REQUIRED,
            "session resume-secret file is not private and regular",
        )
    secret = target.read_text(encoding="utf-8").strip()
    if not secret:
        raise LlmCoordError(ErrorCode.SESSION_AUTH_REQUIRED, "session secret is empty")
    return secret


def remove_session_secret(paths: AppPaths, session_id: str) -> None:
    target = paths.session_secret_file(session_id)
    with suppress(FileNotFoundError):
        reject_symlink_components(target)
        target.unlink()


def _object(value: object, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LlmCoordError(
            ErrorCode.PROTOCOL_MISMATCH, f"session response lacks {key}"
        )
    nested = value.get(key)
    if not isinstance(nested, dict):
        raise LlmCoordError(
            ErrorCode.PROTOCOL_MISMATCH, f"session response lacks {key}"
        )
    return nested


def _text(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise LlmCoordError(ErrorCode.PROTOCOL_MISMATCH, f"session {key} is malformed")
    return item


def _integer(value: dict[str, Any], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or item < 0:
        raise LlmCoordError(ErrorCode.PROTOCOL_MISMATCH, f"session {key} is malformed")
    return item


__all__ = [
    "open_or_resume_session",
    "remove_session_secret",
    "run_session",
    "session_resume_secret",
]
