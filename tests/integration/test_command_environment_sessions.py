"""Scripted terminal environment privacy and recovery without live models."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
from test_shared_agent_execution import (
    ScriptedProvider,
    _assert_completed,
    _call,
    _finish,
    _register,
    _request,
    _start,
    _tools,
)

from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.source_policy import contains_secret_material
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import LlmCoordError
from llm_cli.execution import commands, shared
from llm_cli.observability.logging import JsonLogger
from llm_cli.providers.base import ModelTurn


def _session_service(base: DaemonService, boot: str = "c9-boot") -> DaemonService:
    return DaemonService(
        base.paths,
        replace(
            base.settings, agent_command_environment="session", agent_commands="allow"
        ),
        asyncio.Event(),
        boot_id=boot,
        authority_root=base.physical_authority.root,
    )


async def _open(
    service: DaemonService,
    repository: Path,
    model: str,
    environment: dict[str, str],
    *,
    workspace: str = "shared",
    mode: str = "auto",
) -> dict[str, str]:
    credentials = {"session_id": model, "resume_secret": "resume-c9-" + model}
    opened = await service.handle(
        _request(
            "session.open",
            {
                "session_id": model,
                "path": str(repository),
                "resume_token_hash": hashlib.sha256(
                    credentials["resume_secret"].encode()
                ).hexdigest(),
                "provider": "scripted",
                "model": model,
                "workspace": workspace,
                "mode": mode,
                "command_environment": environment,
            },
        )
    )
    await service.handle(
        _request(
            "session.ack",
            {
                **credentials,
                "sequence": opened["bootstrap_sequence"],
            },
        )
    )
    return credentials


@pytest.mark.parametrize("workspace", ["shared", "isolated"])
@pytest.mark.parametrize("separator", [None, "\n", "\r\n"])
def test_session_commands_and_checks_screen_values_before_storage(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    workspace: str,
    separator: str | None,
) -> None:
    # This is a process-output test, not a signature-screening test.
    canary = "c9-terminal-private-canary-92b5ce6d"
    if separator is not None:
        canary += separator + "c9-terminal-second-private-line-4068fd3a"
    assert not contains_secret_material(canary)
    monkeypatch.setenv("PROJECT_MODE", "wrong-daemon-terminal")
    monkeypatch.setattr(shared, "available_sandbox", lambda: "test")
    monkeypatch.setattr(commands, "wrap", lambda kind, policy, argv, *, cwd: list(argv))
    script = (
        "import os; "
        "assert os.environ['PROJECT_MODE']=='from-opening-terminal'; "
        "assert os.environ['AWS_PROFILE']=='local-test'; "
        "assert not any(n in os.environ for n in "
        "['OPENAI_API_KEY','CUSTOM_BILLING_KEY',"
        "'LOUPE_INTERNAL','LLM_COORD_PRIVATE']); "
        "assert os.environ['HOME']!='terminal-home'; "
        "assert os.environ['TMPDIR']!='terminal-tmp'; "
        "assert os.environ['XDG_CACHE_HOME']!='terminal-cache'; "
        "print(os.environ['PROJECT_PASSWORD']); print('C9 project checks passed')"
    )
    provider = ScriptedProvider(
        "c9-" + workspace,
        [
            _tools(
                _call("read_file", path="a.txt"),
                _call("write_file", path="a.txt", content="updated\n"),
            ),
            _tools(_call("run_command", argv=[sys.executable, "-c", script])),
            _finish(),
        ],
    )

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"a.txt": "original\n"})
        service = _session_service(service_factory(tmp_path))
        service.providers.register(
            "scripted",
            lambda model: provider,
            credential_environment_names=("CUSTOM_BILLING_KEY",),
        )
        service.initialize()
        logger = JsonLogger(service.paths.log_file)
        logger.write("daemon.ready", provider="scripted")
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            await service.handle(
                _request(
                    "checks.configure",
                    {
                        "path": str(repository),
                        "config": {
                            "checks": {
                                "project": {
                                    "argv": [sys.executable, "-c", script],
                                    "env": {
                                        "HOME": "terminal-home",
                                        "CUSTOM_BILLING_KEY": "override-provider-value",
                                        "OPENAI_API_KEY": "override-known-key",
                                    },
                                }
                            }
                        },
                    },
                )
            )
            environment = {
                "PATH": os.environ["PATH"],
                "PWD": str(repository),
                "PROJECT_MODE": "from-opening-terminal",
                "AWS_PROFILE": "local-test",
                "PROJECT_PASSWORD": canary,
                "OPENAI_API_KEY": "opaque-provider-key",
                "CUSTOM_BILLING_KEY": "custom-provider-value",
                "LOUPE_INTERNAL": "loupe-value",
                "LLM_COORD_PRIVATE": "coordinator-value",
                "HOME": "terminal-home",
                "TMPDIR": "terminal-tmp",
                "XDG_CACHE_HOME": "terminal-cache",
            }
            credentials = await _open(
                service, repository, provider.model, environment, workspace=workspace
            )
            background = await _start(
                service, repository, credentials, "c9-processes", ("*",)
            )
            await asyncio.wait_for(background, 30)
            _assert_completed(service, "c9-processes")
            assert (repository / "a.txt").read_text() == "updated\n"
            with service.store.connection() as con:
                rows = con.execute(
                    "SELECT state,output FROM check_runs WHERE task_id='c9-processes'"
                ).fetchall()
                dump = "\n".join(con.iterdump())
            assert rows and all(row["state"] == "passed" for row in rows), rows
            assert all("[private value]" in row["output"] for row in rows)
            assert all("C9 project checks passed" in row["output"] for row in rows)
            events = [
                asdict(event)
                for event in service.store.list_task_events("c9-processes", limit=500)
            ]
            for kind in ("command.output", "check.output"):
                output = "".join(
                    str(e["payload"]["text"]) for e in events if e["event_type"] == kind
                )
                assert (
                    "[private value]" in output and "C9 project checks passed" in output
                )
            shown = await service.handle(
                _request("session.show", {"session_id": provider.model})
            )
            plan = shown["command_environment"]
            assert plan["state"] == "ready"
            assert "PROJECT_MODE" in plan["included_names"]
            assert "CUSTOM_BILLING_KEY" in plan["removed_names"]
            if workspace == "isolated":
                assert "PWD" in plan["rewritten_names"]
            stored = (
                dump
                + json.dumps(events)
                + json.dumps(shown)
                + repr(provider.results)
                + service.paths.control_db.read_bytes().decode("latin-1")
                + service.paths.log_file.read_text()
            )
            for fragment in (canary, *canary.splitlines()):
                assert fragment not in stored
            assert (
                service.session_environments.get(provider.model).values[
                    "PROJECT_PASSWORD"
                ]
                == canary
            )
            await service.handle(_request("session.close", credentials))
            assert (
                service.session_environments.plan(provider.model)["state"]
                == "awaiting_session_environment"
            )
        finally:
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["resume", "cancel", "close"])
def test_restarted_launch_waits_for_own_terminal_without_blocking_other_sessions(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
    action: str,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path, {"a.txt": "base a\n", "b.txt": "base b\n"}
        )
        dead = _session_service(service_factory(tmp_path))

        def crash(results: object) -> ModelTurn:
            raise KeyboardInterrupt

        first = ScriptedProvider(
            "c9-restarted",
            [
                _tools(
                    _call("read_file", path="a.txt"),
                    _call("write_file", path="a.txt", content="updated\n"),
                ),
                crash,
            ],
        )
        _register(dead, first)
        dead.initialize()
        await dead.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open(
            dead, repository, first.model, {"PROJECT_MODE": "origin"}
        )
        registered = dead.store.list_repositories()[0]
        task = dead.store.create_task(
            repository_id=registered.repository_id,
            session_id=first.model,
            task_id="c9-waiting",
            title="update a",
        )
        claim = dead.coordinator.request_claim(task.task_id, ["a.txt"])
        dead.store.save_execution_launch(
            task_id=task.task_id,
            attempt=task.attempt,
            driver="coding_agent",
            instructions=task.title,
            interactive=False,
            parameters={"provider": first.name, "model": first.model},
        )
        with pytest.raises(KeyboardInterrupt):
            dead._execute(
                repository=registered,
                task=task,
                claim=claim,
                driver=CodingAgentHarness(first),
                instructions=task.title,
            )
        assert dead.store.get_execution(task.task_id, 1) is not None
        dead.close()
        resumed = ScriptedProvider(first.model, [_finish()])
        unrelated = ScriptedProvider(
            "c9-other", [_tools(_call("read_file", path="b.txt")), _finish()]
        )
        live = _session_service(dead, boot="c9-after-restart")
        _register(live, resumed, unrelated)
        live.initialize()
        try:
            assert (task.task_id, 1) not in live._background_tasks
            assert (
                live._task_view(task.task_id)["state"] == "awaiting_session_environment"
            )
            assert (
                live._environment_plan(first.model)["state"]
                == "awaiting_session_environment"
            )
            # Idempotent scans keep the checkpoint and one durable notice.
            live._schedule_ready_launches()
            live._schedule_ready_launches()
            waiting = [
                event
                for event in live.store.list_task_events(task.task_id)
                if event.event_type == "execution.awaiting_session_environment"
            ]
            assert len(waiting) == 1
            other_credentials = await _open(
                live, repository, unrelated.model, {"PROJECT_MODE": "other"}
            )
            work = await _start(
                live, repository, other_credentials, "c9-unrelated", ("b.txt",)
            )
            await asyncio.wait_for(work, 20)
            _assert_completed(live, "c9-unrelated")
            if action == "resume":
                await live.handle(
                    _request(
                        "session.resume",
                        {
                            **credentials,
                            "command_environment": {"PROJECT_MODE": "origin"},
                        },
                    )
                )
                await asyncio.wait_for(live._background_tasks[(task.task_id, 1)], 20)
                _assert_completed(live, task.task_id)
                assert (repository / "a.txt").read_text() == "updated\n"
                writes = [
                    e
                    for e in live.store.list_task_events(task.task_id)
                    if e.event_type == "tool.called"
                    and e.payload.get("tool") == "write_file"
                ]
                assert len(writes) == 1
            else:
                if action == "cancel":
                    await live.handle(
                        _request("task.cancel", {"task_id": task.task_id})
                    )
                else:
                    await live.handle(_request("session.close", credentials))
                assert live.workflow.get(task.task_id)["status"] == "cancelled"
                assert (
                    live.store.get_claim(
                        live.store.get_task(task.task_id).current_claim_id
                    ).state
                    == "released"
                )
                assert (repository / "a.txt").read_text() == "base a\n"
            assert not resumed.steps if action == "resume" else len(resumed.steps) == 1
        finally:
            live.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("workspace", ["shared", "isolated"])
@pytest.mark.parametrize("reconnect", ["resume", "closed-apply", "active-authority"])
def test_retained_apply_waits_for_reconnect_and_rechecks_changed_environment(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
    workspace: str,
    reconnect: str,
) -> None:
    canary = "c9-apply-private-canary-428fa98d"
    assert not contains_secret_material(canary)

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"a.txt": "base\n"})
        provider = ScriptedProvider(
            "c9-held",
            [
                _tools(
                    _call("read_file", path="a.txt"),
                    _call("write_file", path="a.txt", content="applied\n"),
                ),
                _finish(),
            ],
        )
        dead = _session_service(service_factory(tmp_path))
        _register(dead, provider)
        dead.initialize()
        await dead.handle(_request("repo.add", {"path": str(repository)}))
        await dead.handle(
            _request(
                "checks.configure",
                {
                    "path": str(repository),
                    "config": {
                        "checks": {
                            "environment": {
                                "argv": [
                                    sys.executable,
                                    "-c",
                                    "import os; print(os.environ['PROJECT_MODE']); "
                                    "print(os.environ.get("
                                    "'PROJECT_PASSWORD', 'no-secret'))",
                                ],
                            }
                        }
                    },
                },
            )
        )
        credentials = await _open(
            dead,
            repository,
            provider.model,
            {"PROJECT_MODE": "original-terminal"},
            workspace=workspace,
            mode="normal",
        )
        work = await _start(dead, repository, credentials, "c9-held-task", ("*",))
        await asyncio.wait_for(work, 20)
        assert dead.workflow.get("c9-held-task")["status"] == "awaiting_review"
        assert (repository / "a.txt").read_text() == "base\n"
        if reconnect == "closed-apply":
            await dead.handle(_request("session.close", credentials))
        dead.close()
        live = _session_service(dead, boot="c9-held-after-restart")
        _register(live, ScriptedProvider(provider.model, []))
        live.initialize()
        JsonLogger(live.paths.log_file).write("daemon.ready", provider="scripted")
        try:
            with pytest.raises(LlmCoordError, match="awaiting_session_environment"):
                await live.handle(_request("task.apply", {"task_id": "c9-held-task"}))
            assert (repository / "a.txt").read_text() == "base\n"
            if reconnect == "closed-apply":
                with pytest.raises(LlmCoordError, match="closed session"):
                    await live.handle(
                        _request(
                            "session.resume",
                            {
                                **credentials,
                                "command_environment": {"PROJECT_PASSWORD": canary},
                            },
                        )
                    )
                assert (
                    live.session_environments.plan(provider.model)["state"]
                    == "awaiting_session_environment"
                )
            environment = {
                "PROJECT_MODE": "reconnected-terminal",
                "PROJECT_PASSWORD": canary,
            }
            if reconnect != "closed-apply":
                await live.handle(
                    _request(
                        "session.resume",
                        {
                            **credentials,
                            "command_environment": environment,
                        },
                    )
                )
                params: dict[str, Any] = {"task_id": "c9-held-task"}
                if reconnect == "active-authority":
                    params["command_environment"] = {
                        "PROJECT_MODE": "wrong-applying-terminal",
                        "PROJECT_PASSWORD": "other-terminal-private-value",
                    }
            else:
                params = {"task_id": "c9-held-task", "command_environment": environment}
            await live.handle(_request("task.apply", params))
            if reconnect == "closed-apply":
                assert live.store.get_session(provider.model).state == "closed"
                assert (
                    live.session_environments.plan(provider.model)["state"]
                    == "awaiting_session_environment"
                )
            assert (repository / "a.txt").read_text() == "applied\n"
            with live.store.connection() as con:
                rows = con.execute(
                    "SELECT state,output FROM check_runs WHERE task_id='c9-held-task'"
                ).fetchall()
                dump = "\n".join(con.iterdump())
            events = repr(live.store.list_task_events("c9-held-task", limit=500))
            assert canary not in dump + events + live.paths.log_file.read_text()
            assert any("[private value]" in row["output"] for row in rows)
            assert len(rows) == 2
            assert any(
                row["state"] == "stale" and "original-terminal" in row["output"]
                for row in rows
            )
            assert any(
                row["state"] == "passed" and "reconnected-terminal" in row["output"]
                for row in rows
            )
        finally:
            live.close()

    asyncio.run(scenario())


def test_hook_uses_terminal_environment_and_screens_private_output(
    tmp_path: Path,
    repository_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import RLock

    from llm_cli.execution.commands import CommandRunner
    from llm_cli.execution.environment import CommandEnvironment

    repository = repository_factory(tmp_path, {"a.txt": "base\n"})
    canary = "c9-hook-private-canary-083923bb"
    assert not contains_secret_material(canary)
    environment = CommandEnvironment(
        {"PROJECT_MODE": "hook-terminal", "PROJECT_PASSWORD": canary}
    )
    monkeypatch.setattr(commands, "wrap", lambda kind, policy, argv, *, cwd: list(argv))
    runner = CommandRunner(
        root=repository,
        snapshot_root=tmp_path / "snapshots",
        candidates=lambda: (),
        cancelled=lambda: False,
        emit=lambda kind, payload: None,
        lock=RLock(),
        sandbox="test",
        environment=lambda: environment,
    )
    run = runner.run_hook(
        [
            sys.executable,
            "-c",
            "import os; "
            "assert os.environ['PROJECT_MODE']=='hook-terminal'; "
            "print(os.environ['PROJECT_PASSWORD']); print('C9 hook passed')",
        ],
        10,
        hook_input={"tool": "read_file"},
    )
    assert run.state == "completed" and run.exit_code == 0
    assert "[private value]" in run.output and "C9 hook passed" in run.output
    assert canary not in repr(run)


def test_command_metadata_screens_literal_environment_secret_arguments(
    tmp_path: Path,
    repository_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import RLock

    from llm_cli.execution.commands import CommandRunner
    from llm_cli.execution.environment import CommandEnvironment

    repository = repository_factory(tmp_path, {"a.txt": "base\n"})
    canary = "c9-metadata-private-canary-63f9bad1"
    assert not contains_secret_material(canary)
    environment = CommandEnvironment({"PROJECT_PASSWORD": canary})
    events: list[dict[str, object]] = []
    monkeypatch.setattr(commands, "wrap", lambda kind, policy, argv, *, cwd: list(argv))
    runner = CommandRunner(
        root=repository,
        snapshot_root=tmp_path / "snapshots",
        candidates=lambda: (),
        cancelled=lambda: False,
        emit=lambda kind, payload: events.append(payload),
        lock=RLock(),
        sandbox="test",
        environment=lambda: environment,
    )
    result = runner.run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", canary], ".", 10
    )
    assert not result.is_error
    assert "[private value]" in result.content
    assert canary not in repr(events) + result.content


def test_reconnect_during_check_cleanup_cannot_resurrect_old_success(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.execution import checks
    from llm_cli.execution.checks import (
        CheckRunner,
        verification_current,
        verification_status,
    )

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"a.txt": "base\n"})
        service = _session_service(service_factory(tmp_path))
        _register(service, ScriptedProvider("c9-finalization", []))
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            await service.handle(
                _request(
                    "checks.configure",
                    {
                        "path": str(repository),
                        "config": {
                            "checks": {
                                "env": {
                                    "argv": [
                                        sys.executable,
                                        "-c",
                                        "import os; print(os.environ['PROJECT_MODE'])",
                                    ],
                                }
                            }
                        },
                    },
                )
            )
            credentials = await _open(
                service, repository, "c9-finalization", {"PROJECT_MODE": "old-terminal"}
            )
            registered = service.store.list_repositories()[0]
            task = service.store.create_task(
                repository_id=registered.repository_id,
                session_id=credentials["session_id"],
                task_id="c9-finalization-task",
                title="check",
            )
            row = service.workflow.ensure(task.task_id)
            checker = CheckRunner(
                service.workflow,
                row,
                repository,
                tmp_path / "checks",
                lambda: (),
                lambda: False,
                lambda kind, payload: None,
                service.workspace_batches.lock,
                environment=lambda: service._command_environment(
                    credentials["session_id"]
                ),
            )
            session = service.store.get_session(credentials["session_id"])
            original = checks.remove_managed_worktree
            reconnected = False

            def reconnect_after_cleanup(*args: Any, **kwargs: Any) -> None:
                nonlocal reconnected
                original(*args, **kwargs)
                if not reconnected:
                    reconnected = True
                    service._accept_session_environment(
                        session,
                        {"command_environment": {"PROJECT_MODE": "new-terminal"}},
                    )

            monkeypatch.setattr(
                checks, "remove_managed_worktree", reconnect_after_cleanup
            )
            outcome = checker.run("env")
            assert reconnected and outcome.is_error
            assert not checker._passed("env")
            assert not verification_current(service.store, row, repository, ())
            assert verification_status(service.store, row, repository, ()) != "verified"
            with service.store.connection() as con:
                stale = con.execute(
                    "SELECT state FROM check_runs WHERE task_id=?", (task.task_id,)
                ).fetchall()
            assert len(stale) == 1 and stale[0]["state"] == "error"
            # A fresh invocation sees the new terminal and can establish proof.
            outcome = checker.run("env")
            assert not outcome.is_error and "new-terminal" in outcome.content
            assert checker._passed("env")
        finally:
            service.close()

    asyncio.run(scenario())


def test_invalidated_environment_cannot_emit_private_command_metadata(
    tmp_path: Path,
    repository_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import RLock

    from llm_cli.execution.commands import CommandRunner
    from llm_cli.execution.environment import CommandEnvironment

    repository = repository_factory(tmp_path, {"a.txt": "base\n"})
    canary = "c9-concurrent-private-canary-63fa981c"
    environment = CommandEnvironment({"PROJECT_PASSWORD": canary})
    events: list[dict[str, object]] = []

    def invalidated_before_getter() -> CommandEnvironment:
        environment.invalidate()
        return environment

    monkeypatch.setattr(commands, "wrap", lambda kind, policy, argv, *, cwd: list(argv))
    runner = CommandRunner(
        root=repository,
        snapshot_root=tmp_path / "snapshots",
        candidates=lambda: (),
        cancelled=lambda: False,
        emit=lambda kind, payload: events.append(payload),
        lock=RLock(),
        sandbox="test",
        environment=invalidated_before_getter,
    )
    result = runner.run([sys.executable, "-c", "print('unused')", canary], ".", 10)
    assert result.is_error and "awaiting_session_environment" in result.content
    assert canary not in repr(events) + result.content
    assert not any("argv" in event for event in events)


def test_closed_held_apply_uses_frozen_runtime_for_environment_path_rewrite(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path, {"a.txt": "base\n", ".gitignore": "runtime.txt\n"}
        )
        runtime = repository / "runtime.txt"
        runtime.write_text("retained-runtime")
        provider = ScriptedProvider(
            "c9-frozen-path",
            [
                _tools(
                    _call("read_file", path="a.txt"),
                    _call("write_file", path="a.txt", content="applied\n"),
                ),
                _finish(),
            ],
        )
        dead = _session_service(service_factory(tmp_path))
        _register(dead, provider)
        dead.initialize()
        await dead.handle(_request("repo.add", {"path": str(repository)}))
        await dead.handle(
            _request(
                "checks.configure",
                {
                    "path": str(repository),
                    "config": {
                        "runtime_paths": ["runtime.txt"],
                        "checks": {
                            "runtime": {
                                "argv": [
                                    sys.executable,
                                    "-c",
                                    "import os,pathlib; "
                                    "assert pathlib.Path(os.environ['PROJECT_RUNTIME'])"
                                    ".read_text()=='retained-runtime'; "
                                    "print('held-runtime-verified')",
                                ],
                            }
                        },
                    },
                },
            )
        )
        environment = {"PROJECT_RUNTIME": str(runtime), "PWD": str(repository)}
        credentials = await _open(
            dead,
            repository,
            provider.model,
            environment,
            workspace="isolated",
            mode="normal",
        )
        work = await _start(
            dead, repository, credentials, "c9-frozen-path-task", ("*",)
        )
        await asyncio.wait_for(work, 20)
        assert dead.workflow.get("c9-frozen-path-task")["status"] == "awaiting_review"
        session = dead.store.get_session(provider.model)
        workspace = dead.store.get_workspace(session.workspace_id)
        runtime.unlink()
        dead.isolated_workspaces.runtime.refresh(workspace.workspace_id)
        assert not (Path(workspace.canonical_path) / "runtime.txt").exists()
        await dead.handle(_request("session.close", credentials))
        dead.close()
        live = _session_service(dead, boot="c9-frozen-after-restart")
        _register(live, ScriptedProvider(provider.model, []))
        live.initialize()
        try:
            await live.handle(
                _request(
                    "task.apply",
                    {
                        "task_id": "c9-frozen-path-task",
                        "command_environment": environment,
                    },
                )
            )
            assert (repository / "a.txt").read_text() == "applied\n"
            with live.store.connection() as con:
                rows = con.execute(
                    "SELECT state,output FROM check_runs "
                    "WHERE task_id='c9-frozen-path-task'"
                ).fetchall()
            assert len(rows) == 2 and any(
                row["state"] == "passed" and "held-runtime-verified" in row["output"]
                for row in rows
            )
        finally:
            live.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["check", "command"])
def test_shared_checkout_paths_use_proposal_snapshot(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    monkeypatch.setattr(shared, "available_sandbox", lambda: "test")
    monkeypatch.setattr(commands, "wrap", lambda kind, policy, argv, *, cwd: list(argv))
    module = "src/c9_proposal.py"
    proposed = "VALUE = 'proposal'\n"
    script = (
        "from pathlib import Path; import c9_proposal; "
        "print('Imported value:', c9_proposal.VALUE, flush=True); "
        "assert c9_proposal.VALUE == 'proposal'; "
        "assert Path(c9_proposal.__file__).resolve() == "
        "Path.cwd() / 'src' / 'c9_proposal.py'; "
        "print('C9 snapshot import passed')"
    )
    turns = [
        _tools(
            _call("read_file", path=module),
            _call("write_file", path=module, content=proposed),
        )
    ]
    if operation == "command":
        turns.extend(
            [
                _tools(_call("run_command", argv=[sys.executable, "-B", "-c", script])),
                _tools(_call("run_command", argv=["c9-project-probe"])),
                _tools(_call("run_command", argv=["c9-dependency-probe"])),
            ]
        )
    turns.append(_finish())
    provider = ScriptedProvider("c9-shared-path-" + operation, turns)

    async def scenario() -> None:
        repository = repository_factory(
            tmp_path,
            {
                module: "VALUE = 'checkout'\n",
                "bin/c9-project-probe": (
                    "#!/bin/sh\nprintf 'C9 project bin passed\\n'\n"
                ),
                ".gitignore": ".venv/\n__pycache__/\n",
            },
        )
        (repository / "bin/c9-project-probe").chmod(0o755)
        dependency = repository / ".venv/bin/c9-dependency-probe"
        dependency.parent.mkdir(parents=True)
        dependency.write_text("#!/bin/sh\nprintf 'C9 dependency bin passed\\n'\n")
        dependency.chmod(0o755)
        service = _session_service(service_factory(tmp_path))
        _register(service, provider)
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            check_script = (
                script if operation == "check" else "print('C9 check passed')"
            )
            await service.handle(
                _request(
                    "checks.configure",
                    {
                        "path": str(repository),
                        "config": {
                            "runtime_paths": [".venv"],
                            "checks": {
                                "project": {
                                    "argv": [sys.executable, "-B", "-c", check_script]
                                }
                            },
                        },
                    },
                )
            )
            credentials = await _open(
                service,
                repository,
                provider.model,
                {
                    "PWD": str(repository),
                    "PYTHONPATH": str(repository / "src"),
                    "PATH": os.pathsep.join(
                        (str(repository / "bin"), str(dependency.parent), os.defpath)
                    ),
                },
            )
            task_id = "c9-shared-path-" + operation
            background = await _start(service, repository, credentials, task_id, ("*",))
            await asyncio.wait_for(background, 30)
            with service.store.connection() as con:
                rows = con.execute(
                    "SELECT state,output FROM check_runs WHERE task_id=?", (task_id,)
                ).fetchall()
            assert rows and all(row["state"] == "passed" for row in rows), rows
            events = service.store.list_task_events(task_id, limit=500)
            if operation == "check":
                output = "".join(row["output"] for row in rows)
            else:
                output = "".join(
                    str(event.payload["text"])
                    for event in events
                    if event.event_type == "command.output"
                )
                assert "C9 project bin passed" in output
                assert "C9 dependency bin passed" in output
            assert "C9 snapshot import passed" in output
            assert "Imported value: checkout" not in output
            _assert_completed(service, task_id)
            assert (repository / module).read_text() == proposed
            plan = service.session_environments.plan(provider.model)
            assert {"PWD", "PYTHONPATH", "PATH"} <= set(plan["rewritten_names"])
        finally:
            service.close()

    asyncio.run(scenario())
