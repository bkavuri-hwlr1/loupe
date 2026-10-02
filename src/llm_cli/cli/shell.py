"""Loupe's local shell; accounts and projects connect only when needed."""

from __future__ import annotations

import shlex
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

from llm_cli.agent.modes import AGENT_MODES, resolve_agent_mode, validate_agent_mode
from llm_cli.build import code_identity
from llm_cli.cli import session
from llm_cli.cli.composer import Composer
from llm_cli.cli.connect import ConnectionMenu, normalize_provider
from llm_cli.cli.interrupts import EXIT_HINT, ExitRequested
from llm_cli.cli.models import ModelMenu
from llm_cli.cli.output import emit
from llm_cli.cli.terminal import TerminalUI, safe_text
from llm_cli.config.loader import load_settings
from llm_cli.coordination.scopes import normalize_scopes
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.protocol.client import DaemonClient
from llm_cli.providers.accounts import (
    SUPPORTED_PROVIDERS,
    account_status,
    load_preference,
    save_preference,
)
from llm_cli.providers.catalog import ModelOption, default_model, model_option

_SETTLED = {
    "awaiting_review",
    "reviewing",
    "completed",
    "failed",
    "cancelled",
    "ready_for_integration",
    "operator_attention",
}

_MODE_DESCRIPTIONS = {
    "plan": "Inspect and propose a plan; no edits or check commands.",
    "normal": "Prepare changes for review; /apply publishes them.",
    "auto": "Publish completed changes automatically after required checks.",
}


class ChatShell:
    def __init__(
        self,
        client: DaemonClient,
        *,
        repository: Path,
        scopes: list[str],
        provider: str | None,
        model: str | None,
        resume_session_id: str | None,
        workspace_mode: str | None,
        stdin: TextIO,
        stream: TextIO,
        plain: bool,
        publication_mode: str | None = None,
        agent_mode: str | None = None,
        effort: str | None = None,
    ) -> None:
        if effort is not None and resume_session_id:
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                "--effort applies to new conversations; use /effort after resuming.",
            )
        self.agent_mode = resolve_agent_mode(agent_mode, publication_mode)
        self.client = client
        self.repository = repository
        self.scopes = scopes
        self.workspace_mode = workspace_mode or "shared"
        self.stdin, self.stream, self.plain = stdin, stream, plain
        self.ui = TerminalUI(stream, plain=plain)
        self.composer = Composer(stdin, stream, plain=plain)
        self.composer.on_mode_cycle = self._cycle_mode
        self.menu = ConnectionMenu(client.paths, self.ui, stdin, stream, plain=plain)
        self.models = ModelMenu(client.paths, self.ui, stdin, stream, plain=plain)
        self.credentials: session._SessionCredentials | None = None
        self.last_task: str | None = None
        self.keep_session = False
        # The code this CLI loaded, compared once with the daemon's.
        self._code = code_identity()
        self._daemon_checked = False
        try:
            self.provider, self.model, self.effort = self._initial_selection(
                provider, model
            )
        except (LlmCoordError, OSError):
            self.provider, self.model, self.effort = provider, model, None
            self.ui.notice(
                "Saved account settings need attention. Use /login to reconnect."
            )
        if effort is not None and self.provider is None:
            self.ui.notice(
                "--effort needs a connected AI. Use /login, then /effort to choose."
            )
        elif effort is not None:
            # An explicit level applies to this conversation; /effort saves one.
            try:
                self.effort = self._validated_effort(
                    self._current_model_option(), effort
                )
            except ValueError as exc:
                raise LlmCoordError(ErrorCode.CONFIG_INVALID, str(exc)) from exc
        if resume_session_id:
            self._check_daemon_code()
            self.credentials = session.open_or_resume_session(
                client,
                repository=repository,
                provider=provider,
                model=model,
                resume_session_id=resume_session_id,
                workspace_mode=workspace_mode,
                agent_mode=agent_mode,
                publication_mode=publication_mode,
            )
            self.provider, self.model = (
                self.credentials.provider,
                self.credentials.model,
            )
            self.effort = self.credentials.effort
            self.workspace_mode = self.credentials.workspace_mode
            self.agent_mode = self.credentials.agent_mode
            session._set_intent(client, self.credentials, scopes)
            tasks = self._tasks()
            if tasks:
                active = [task for task in tasks if task.get("state") not in _SETTLED]
                self.last_task = str((active or tasks)[-1]["task_id"])

    def _initial_selection(
        self, provider: str | None, model: str | None
    ) -> tuple[str | None, str | None, str | None]:
        try:
            preference = load_preference(self.client.paths)
        except (LlmCoordError, OSError):
            if not provider:
                raise
            # An explicit provider does not depend on the saved selection.
            preference = {}
        saved_provider = preference.get("provider")
        # Naming the saved provider again (for example with --provider) keeps its
        # saved model and effort. Only a different provider or model resets them.
        if saved_provider and (not provider or provider == saved_provider):
            saved_model = preference.get("model")
            saved_effort = preference.get("effort")
            return (
                str(saved_provider),
                model or (saved_model if isinstance(saved_model, str) else None),
                saved_effort
                if isinstance(saved_effort, str) and model in {None, saved_model}
                else None,
            )
        if provider:
            return provider, model, None
        settings = load_settings(
            self.client.paths.config_file, profile_id=self.client.paths.profile_id
        )
        choices = [settings.agent_provider, *SUPPORTED_PROVIDERS]
        for candidate in dict.fromkeys(choices):
            if self._ready(candidate):
                selected_model = (
                    settings.agent_model
                    if candidate == settings.agent_provider
                    else None
                )
                return candidate, model or selected_model, None
        return None, model, None

    def _ready(self, provider: str) -> bool:
        if provider not in SUPPORTED_PROVIDERS:
            # Explicitly configured third-party adapters retain their own auth.
            return True
        status = account_status(self.client.paths, provider)
        return bool(status.get("authenticated") or status.get("refreshable"))

    def _context(self) -> dict[str, Any]:
        selected_model = self._effective_model()
        self.composer.model = selected_model or self.provider or "Not connected"
        self.composer.effort = self.effort or "default"
        self.composer.scope = ", ".join(self.scopes)
        self.composer.mode = self.agent_mode
        return {
            "repository": self.repository,
            "scopes": self.scopes,
            "provider": self.provider or "",
            "model": selected_model,
            "effort": self.effort,
            "session_id": self.credentials.session_id
            if self.credentials
            else "Starts with your first message",
            "workspace_mode": self.workspace_mode,
            "agent_mode": self.agent_mode,
        }

    def _tasks(self) -> list[dict[str, Any]]:
        if self.credentials is None:
            return []
        records = self.client.call("task.list")
        if not isinstance(records, list):
            raise LlmCoordError(ErrorCode.PROTOCOL_MISMATCH, "task list is malformed")
        return [
            record
            for record in records
            if isinstance(record, dict)
            and record.get("session_id") == self.credentials.session_id
        ]

    def _require_idle(self) -> None:
        if self.credentials is None:
            return
        if any(task.get("state") not in _SETTLED for task in self._tasks()):
            raise LlmCoordError(
                ErrorCode.TASK_NOT_MUTABLE,
                "Your task is still running. Use /attach and let it finish "
                "before changing session settings.",
            )
        if self.last_task:
            try:
                task = self.client.call("task.show", {"task_id": self.last_task})
            except LlmCoordError as exc:
                if exc.code is ErrorCode.REPOSITORY_NOT_FOUND:
                    return
                raise
            if not isinstance(task, dict) or task.get("state") not in _SETTLED:
                raise LlmCoordError(
                    ErrorCode.TASK_NOT_MUTABLE,
                    "Your task is still active. Use /attach to follow it.",
                )

    def _close(self) -> None:
        if self.credentials is None:
            return
        self._require_idle()
        self.client.call(
            "session.close",
            {
                "session_id": self.credentials.session_id,
                "resume_secret": self.credentials.resume_secret,
            },
        )
        session.remove_session_secret(self.client.paths, self.credentials.session_id)
        self.credentials = None
        self.last_task = None

    def _switch(
        self, provider: str, model: str | None = None, effort: str | None = None
    ) -> None:
        if (self.provider, self.model, self.effort) == (provider, model, effort):
            if provider in SUPPORTED_PROVIDERS:
                save_preference(self.client.paths, provider, model, effort=effort)
            return
        had_conversation = self.credentials is not None
        self._close()
        self.provider, self.model, self.effort = provider, model, effort
        if provider in SUPPORTED_PROVIDERS:
            save_preference(self.client.paths, provider, model, effort=effort)
        if had_conversation:
            self.ui.notice("A fresh conversation will start with your next message.")

    def _ensure_session(self) -> bool:
        if self.provider is None or not self._ready(self.provider):
            selected = self.menu.choose(self.provider)
            if selected is None:
                self.ui.notice(
                    "Use /login when you're ready. Your prompt is in the input history."
                )
                return False
            self._switch(
                selected,
                self.model if selected == self.provider else None,
                self.effort if selected == self.provider else None,
            )
        if self.credentials is not None:
            return True
        self._check_daemon_code()
        try:
            self.client.call("repo.status", {"path": str(self.repository)})
        except LlmCoordError as exc:
            if exc.code is not ErrorCode.REPOSITORY_NOT_FOUND or (
                exc.details or {}
            ).get("registered_targets"):
                raise
            self.client.call("repo.add", {"path": str(self.repository)})
        self.credentials = session.open_or_resume_session(
            self.client,
            repository=self.repository,
            provider=self.provider,
            model=self.model,
            effort=self.effort,
            resume_session_id=None,
            workspace_mode=self.workspace_mode,
            agent_mode=self.agent_mode,
        )
        self.provider, self.model = self.credentials.provider, self.credentials.model
        self.effort = self.credentials.effort
        self.agent_mode = self.credentials.agent_mode
        session._set_intent(self.client, self.credentials, self.scopes)
        return True

    def _check_daemon_code(self) -> None:
        """Warn once when the background service runs different Loupe code.

        A daemon keeps the code it started with, and every installation using
        this profile shares it. Fixes in this CLI may not apply until restart.
        Restarting is left to the user: it interrupts running tasks, including
        those of other terminals.
        """

        if self._daemon_checked:
            return
        self._daemon_checked = True
        try:
            info = self.client.call("system.ping")
        except LlmCoordError:
            return  # The next request reports why the daemon is unavailable.
        if not isinstance(info, dict):
            return
        fingerprint = info.get("code_fingerprint")
        if fingerprint == self._code["fingerprint"]:
            return
        path = info.get("code_path")
        if not isinstance(fingerprint, str):
            problem = "is running an older version of Loupe"
        elif isinstance(path, str) and path != self._code["path"]:
            problem = f"is running Loupe from {safe_text(path)}, not this installation"
        else:
            problem = "is still running code from before your last update"
        self.ui.notice(
            f"The Loupe background service {problem}. After active tasks "
            "finish, run 'loupe daemon restart' to use this version.",
            style="warning",
        )

    def _effective_model(self) -> str:
        if self.model:
            return self.model
        if self.provider in SUPPORTED_PROVIDERS:
            return default_model(self.provider)
        return ""

    def _mode_command(self, args: list[str], *, announce: bool = True) -> None:
        if len(args) > 1:
            raise ValueError("Usage: /mode [plan|normal|auto]")
        if args:
            selected = validate_agent_mode(args[0])
            self._require_idle()
            if self.credentials is not None:
                self.credentials = session.set_session_mode(
                    self.client, self.credentials, selected
                )
            self.agent_mode = selected
        elif self.credentials is not None:
            # Another terminal can resume this session and change its next
            # task's mode. Read the canonical policy when showing the chooser.
            response = self.client.call(
                "session.show", {"session_id": self.credentials.session_id}
            )
            mode = session._session_mode(session._object(response, "session"))
            self.credentials = replace(self.credentials, agent_mode=mode)
            self.agent_mode = mode
        self._context()
        if announce:
            self.ui.notice(
                f"Mode: {self.agent_mode}. {_MODE_DESCRIPTIONS[self.agent_mode]}"
            )
        if not args:
            for mode, description in _MODE_DESCRIPTIONS.items():
                self.ui.notice(f"  /mode {mode:<6} {description}")

    def _current_model_option(self) -> ModelOption:
        assert self.provider is not None
        return model_option(
            self.provider, self._effective_model(), paths=self.client.paths
        )

    def _model_command(self, args: list[str]) -> None:
        if len(args) > 1:
            raise ValueError("Usage: /model [MODEL_ID | --refresh]")
        if self.provider is None:
            self.ui.notice("Choose an AI with /login or /provider first.")
            return
        self._require_idle()
        if args and args != ["--refresh"]:
            chosen = args[0]
            if not 0 < len(chosen) <= 256 or not all(
                33 <= ord(char) <= 126 for char in chosen
            ):
                raise ValueError(
                    "Model ID must be at most 256 characters without spaces."
                )
            self._switch(
                self.provider,
                chosen,
                self.effort if chosen == self._effective_model() else None,
            )
        else:
            if not self._ready(self.provider):
                self.ui.notice("Connect with /login to browse your account's models.")
                return
            option = self.models.choose_model(
                self.provider,
                self._effective_model(),
                refresh=args == ["--refresh"],
            )
            if option is None:
                return
            effort = None
            if option.efforts:
                selected = self.models.choose_effort(
                    option,
                    self.effort if option.id == self._effective_model() else None,
                )
                if selected is None:
                    self.ui.notice("Selection cancelled. Your model is unchanged.")
                    return
                effort = None if selected == "default" else selected
                if effort is not None and effort not in option.efforts:
                    raise ValueError("Choose an effort level shown for this model.")
            self._switch(self.provider, option.id, effort)
        self.ui.notice(
            f"Model set to {self._effective_model()} · "
            f"{self.effort or 'provider default'} effort.",
            style="success",
        )
        self.ui.notice("Your selection is saved. Use /effort to adjust thinking.")

    @staticmethod
    def _validated_effort(option: ModelOption, value: str) -> str | None:
        """Return the effort to request, or None for the provider's default."""

        selected = value.lower()
        effort = None if selected == "default" else selected
        if effort is not None and effort not in option.efforts:
            choices = ", ".join((*option.efforts, "default"))
            raise ValueError(f"Effort levels for {option.id}: {choices}.")
        return effort

    def _effort_command(self, args: list[str]) -> None:
        if len(args) > 1:
            raise ValueError("Usage: /effort [LEVEL | default]")
        if self.provider is None:
            self.ui.notice("Choose an AI with /login or /provider first.")
            return
        self._require_idle()
        option = self._current_model_option()
        if not args and not option.efforts:
            self.models.choose_effort(option, self.effort)
            return
        selected = (
            args[0].lower() if args else self.models.choose_effort(option, self.effort)
        )
        if selected is None:
            return
        effort = self._validated_effort(option, selected)
        self._switch(self.provider, self.model, effort)
        self.ui.notice(
            f"Effort set to {effort or 'provider default'} for {option.id}. "
            "Your selection is saved.",
            style="success",
        )

    def _resume_hint(self) -> None:
        if self.credentials is None:
            self.ui.notice("See you next time. Your account choice is saved.")
            return
        arguments = [
            "loupe",
            "--profile",
            self.client.paths.profile_id,
            "chat",
            "--repo",
            str(self.repository),
            "--resume",
            self.credentials.session_id,
        ]
        for scope in self.scopes:
            arguments.extend(["--scope", scope])
        self.ui.notice("Resume with: " + shlex.join(arguments))

    def _cycle_mode(self) -> None:
        next_index = (AGENT_MODES.index(self.agent_mode) + 1) % len(AGENT_MODES)
        self._mode_command([AGENT_MODES[next_index]], announce=False)

    def run(self) -> int:
        self.ui.banner(**self._context())
        if self.provider is None:
            self.ui.notice(
                "Welcome. Connect Codex, Anthropic, or OpenAI "
                "with /login whenever you're ready."
            )
            self.ui.notice("Use /cd PATH to choose a project, or /help to look around.")
        else:
            self.ui.notice(
                "/model to browse available models · /effort to adjust thinking"
            )
        try:
            while True:
                self._context()
                try:
                    instruction = self.composer.read().strip()
                except KeyboardInterrupt as exc:
                    self.composer.interrupts.handle(exc)
                    self.ui.notice("Draft cleared. " + EXIT_HINT)
                    continue
                except EOFError:
                    break
                if not instruction:
                    continue
                try:
                    if instruction.startswith("/"):
                        if not self._command(instruction):
                            break
                        continue
                    if not self._ensure_session():
                        continue
                    assert self.credentials is not None
                    self.credentials = session._refresh_changes(
                        self.client, self.credentials, self.stream
                    )
                    submitted = session._run_one(
                        self.client,
                        repository=self.repository,
                        scopes=self.scopes,
                        instruction=instruction,
                        credentials=self.credentials,
                        stream=self.stream,
                        input_stream=self.stdin,
                        plain=self.plain,
                        composer=self.composer,
                    )
                    if submitted:
                        self.last_task = submitted
                except (LlmCoordError, ValueError, OSError) as exc:
                    self.ui.error(
                        exc.message if isinstance(exc, LlmCoordError) else str(exc)
                    )
                    if isinstance(exc, LlmCoordError) and exc.code in {
                        ErrorCode.REPOSITORY_NOT_FOUND,
                        ErrorCode.REPOSITORY_UNSAFE,
                    }:
                        self.ui.notice("Choose an existing Git project with /cd PATH.")
                except KeyboardInterrupt as exc:
                    self.composer.interrupts.handle(exc)
                    self.ui.notice("Cancelled. " + EXIT_HINT)
        except ExitRequested:
            pass
        finally:
            if not self.keep_session and self.credentials is not None:
                try:
                    self._close()
                except (LlmCoordError, KeyboardInterrupt):
                    self.ui.notice(
                        "Session kept because work is active or its state is uncertain."
                    )
                    self._resume_hint()
            self.ui.notice("Leaving Loupe.")
        return 0

    def _command(self, instruction: str) -> bool:
        parts = shlex.split(instruction)
        command, args = parts[0], parts[1:]
        if command in {"/diff", "/checks", "/apply", "/undo", "/stop"}:
            allow = "--allow-unverified" in args
            ids = [arg for arg in args if arg != "--allow-unverified"]
            if len(ids) > 1 or (allow and command != "/apply"):
                raise ValueError(f"Usage: {command} [TASK_ID]")
            task_id = ids[0] if ids else self.last_task
            if not task_id:
                raise ValueError("Specify a task ID or run a task first.")
            method = "cancel" if command == "/stop" else command[1:]
            result = self.client.call(
                "task." + method, {"task_id": task_id, "allow_unverified": allow}
            )
            emit(result, as_json=False, stream=self.stream)
            return True
        if command in {"/exit", "/quit"}:
            return False
        if command == "/detach":
            self.keep_session = True
            self._resume_hint()
            return False
        if command in {"/login", "/provider"}:
            if len(args) > 1:
                raise ValueError(f"Usage: {command} [PROVIDER]")
            self._require_idle()
            selected = self.menu.choose(
                args[0] if args else None, login=command == "/login"
            )
            if selected:
                self._switch(
                    selected,
                    self.model if self.provider == selected else None,
                    self.effort if self.provider == selected else None,
                )
                self.ui.notice(
                    f"Ready with {selected}. Your choice is saved.", style="success"
                )
                self.ui.notice(
                    "Use /model to see models and effort levels for this account."
                )
        elif command == "/accounts":
            self.menu.status()
        elif command == "/logout":
            if len(args) > 1:
                raise ValueError("Usage: /logout [PROVIDER]")
            provider = normalize_provider(args[0]) if args else self.provider
            if provider is None:
                self.ui.notice("Choose an account to remove with /logout PROVIDER.")
                return True
            self._require_idle()
            if provider == self.provider:
                self._close()
            removed = self.menu.logout(provider)
            if removed is not None and removed == self.provider:
                self.provider, self.model, self.effort = None, None, None
        elif command in {"/model", "/models"}:
            self._model_command(args)
        elif command == "/effort":
            self._effort_command(args)
        elif command == "/mode":
            self._mode_command(args)
        elif command == "/cd":
            if len(args) != 1:
                raise ValueError("Usage: /cd PATH (quote paths containing spaces)")
            target = Path(args[0]).expanduser()
            if not target.is_absolute():
                target = self.repository / target
            target = target.resolve()
            if not target.is_dir():
                raise ValueError("That folder does not exist.")
            self._close()
            self.repository = target
            self.scopes = ["*"]
            self.ui.notice(f"Project: {target}")
        elif command in {"/help", "/?"}:
            self.ui.help()
        elif command == "/status":
            self.ui.status(**self._context())
        elif command == "/clear":
            if self.stream.isatty() and not self.plain:
                self.ui.console.clear()
            self.ui.banner(**self._context())
        elif command == "/history":
            if not self.composer.prompts:
                self.ui.notice("No prompts in this visit yet.")
            for index, prompt in enumerate(self.composer.prompts, 1):
                self.ui.notice(f"{index}. {prompt}")
        elif command == "/scope":
            if args:
                scopes = list(normalize_scopes(args))
                if self.credentials:
                    session._set_intent(self.client, self.credentials, scopes)
                self.scopes = scopes
            self.ui.notice(f"Scopes: {', '.join(self.scopes)}")
        elif command == "/changes":
            if self.provider is None and self.credentials is None:
                self.ui.notice(
                    "No new checkout changes. "
                    "Connect with /login to start a conversation."
                )
            elif self._ensure_session():
                assert self.credentials is not None
                self.credentials = session._refresh_changes(
                    self.client, self.credentials, self.stream, report_empty=True
                )
        elif command == "/tasks":
            emit(self._tasks(), as_json=False, stream=self.stream, plain=self.plain)
        elif command == "/attach":
            if len(args) > 1:
                raise ValueError("Usage: /attach [TASK_ID]")
            task_id = args[0] if args else self.last_task
            if task_id is None:
                self.ui.notice("No task to attach to yet. Use /attach TASK_ID.")
            else:
                session._follow(
                    self.client,
                    task_id=task_id,
                    stream=self.stream,
                    input_stream=self.stdin,
                    plain=self.plain,
                    composer=self.composer,
                )
        else:
            self.ui.error(f"Unknown command {command!r}. Use /help for commands.")
        return True
