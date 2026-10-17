"""Shared and isolated sessions exchange checked source batches through the daemon."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
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
from test_shared_catch_up import _publish_after_finishing

from llm_cli.daemon.service import DaemonService


async def open_session(
    service: DaemonService, root: Path, model: str, mode: str = "isolated"
) -> tuple[dict[str, str], Path]:
    credentials = {
        "session_id": model,
        "resume_secret": "fixture-session-secret-" + model,
    }
    opened = await service.handle(
        _request(
            "session.open",
            {
                "path": str(root),
                "session_id": model,
                "resume_token_hash": hashlib.sha256(
                    credentials["resume_secret"].encode()
                ).hexdigest(),
                "provider": "scripted",
                "model": model,
                "workspace": mode,
                "mode": "auto",
            },
        )
    )
    await service.handle(
        _request(
            "session.ack", {**credentials, "sequence": opened["bootstrap_sequence"]}
        )
    )
    return credentials, Path(opened["workspace"]["canonical_path"])


def test_shared_and_isolated_sessions_exchange_source_without_losing_edits(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "first\nmiddle\nlast\n"})
        shared = ScriptedProvider(
            "shared",
            [
                _tools(
                    _call("read_file", path="a.py"),
                    _call("write_file", path="a.py", content="first\nmiddle\nremote\n"),
                ),
                _finish(),
            ],
        )
        isolated = ScriptedProvider(
            "isolated",
            [
                _tools(
                    _call("read_file", path="a.py"),
                    _call("write_file", path="a.py", content="local\nmiddle\nremote\n"),
                ),
                _finish(),
            ],
        )
        service = service_factory(tmp_path)
        _register(service, shared, isolated)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials, private = await open_session(service, root, "isolated")
        shared_creds, _ = await open_session(service, root, "shared", "shared")
        try:
            await asyncio.wait_for(
                await _start(service, root, shared_creds, "shared-task", ("*",)), 30
            )
            _assert_completed(service, "shared-task")
            assert (private / "a.py").read_text() == "first\nmiddle\nlast\n"
            await asyncio.wait_for(
                await _start(service, root, credentials, "isolated-task", ("*",)), 30
            )
            _assert_completed(service, "isolated-task")
            assert (root / "a.py").read_text() == "local\nmiddle\nremote\n"
            assert (private / "a.py").read_text() == "local\nmiddle\nremote\n"
            with service.store.connection() as con:
                batches = con.execute(
                    "SELECT workspace_id,canonical_path FROM workspace_batches "
                    "WHERE state='published'"
                ).fetchall()
            assert len(batches) == 2
            assert len({batch["workspace_id"] for batch in batches}) == 1
            assert all(batch["canonical_path"] == str(root) for batch in batches)
        finally:
            service.close()

    asyncio.run(scenario())


def test_isolated_publication_merges_primary_edit_after_checks_and_rechecks(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        root = repository_factory(
            tmp_path, {"a.py": "value = 1\n\nunrelated = True\n\nname = 'old'\n"}
        )
        provider = ScriptedProvider(
            "isolated",
            [
                _tools(
                    _call("read_file", path="a.py"),
                    _call(
                        "write_file",
                        path="a.py",
                        content="value = 2\n\nunrelated = True\n\nname = 'old'\n",
                    ),
                ),
                _finish(),
            ],
        )
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials, private = await open_session(service, root, provider.model)
        import sys

        await service.handle(
            _request(
                "checks.configure",
                {
                    "path": str(root),
                    "config": {
                        "checks": {
                            "test": {
                                "argv": [
                                    sys.executable,
                                    "-c",
                                    "from pathlib import Path; assert 'value = 2' "
                                    "in Path('a.py').read_text()",
                                ]
                            }
                        }
                    },
                },
            )
        )
        _publish_after_finishing(
            service,
            monkeypatch,
            lambda: (root / "a.py").write_text(
                "value = 1\n\nunrelated = True\n\nname = 'new'\n"
            ),
        )
        try:
            await asyncio.wait_for(
                await _start(service, root, credentials, "task", ("*",)), 30
            )
            _assert_completed(service, "task")
            expected = "value = 2\n\nunrelated = True\n\nname = 'new'\n"
            assert (root / "a.py").read_text() == expected
            assert (private / "a.py").read_text() == expected
            checks = service.workflow.checks("task")
            assert len(checks) == 2 and all(
                check["state"] == "passed" for check in checks
            )
        finally:
            service.close()

    asyncio.run(scenario())


def test_publication_preserves_private_edit_made_after_candidate_freeze(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "original\n"})
        provider = ScriptedProvider(
            "isolated",
            [
                _tools(
                    _call("read_file", path="a.py"),
                    _call("write_file", path="a.py", content="published\n"),
                ),
                _finish(),
            ],
        )
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials, private = await open_session(service, root, provider.model)
        publish = service.shared_runner.publisher.publish

        def after_publish(*args: Any, **kwargs: Any) -> Any:
            result = publish(*args, **kwargs)
            if result.state == "published":
                (private / "a.py").write_text("later private edit\n")
            return result

        monkeypatch.setattr(service.shared_runner.publisher, "publish", after_publish)
        try:
            await asyncio.wait_for(
                await _start(service, root, credentials, "task", ("*",)), 30
            )
            _assert_completed(service, "task")
            assert (root / "a.py").read_text() == "published\n"
            assert (private / "a.py").read_text() == "later private edit\n"
        finally:
            service.close()

    asyncio.run(scenario())


def test_declassified_hydrated_runtime_is_refused_before_source_artifact_capture(
    tmp_path: Path, repository_factory: Any, service_factory: Any
) -> None:
    async def scenario() -> None:
        root = repository_factory(
            tmp_path, {"a.py": "source\n", ".gitignore": "settings.local\n"}
        )
        runtime = b"unknown-private-runtime-value"
        (root / "settings.local").write_bytes(runtime)
        provider = ScriptedProvider("isolated", [])
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials, private = await open_session(service, root, provider.model)
        try:
            session = service.store.get_session(credentials["session_id"])
            assert session is not None and session.workspace_id is not None
            service.shared_runner._prepare_isolated(session.workspace_id)
            (root / ".gitignore").write_text("")
            artifact = (
                service.shared_runner.publisher.candidate_dir
                / hashlib.sha256(runtime).hexdigest()
            )
            assert not artifact.exists()
            report = service.shared_runner.isolated_sync.synchronize(
                session.workspace_id
            )
            assert "settings.local" in report.deferred
            assert not artifact.exists()
            assert (private / "settings.local").read_bytes() == runtime
        finally:
            service.close()

    asyncio.run(scenario())


def test_private_sync_failure_does_not_fail_confirmed_publication_or_recovery(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.workspace.broker import WorkspaceBrokerError

    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "original\n"})
        provider = ScriptedProvider(
            "isolated",
            [
                _tools(
                    _call("read_file", path="a.py"),
                    _call("write_file", path="a.py", content="published\n"),
                ),
                _finish(),
            ],
        )
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials, private = await open_session(service, root, provider.model)
        synchronize = service.shared_runner.isolated_sync.synchronize

        def sync(workspace_id: str, **kwargs: Any) -> Any:
            if (root / "a.py").read_text() == "published\n":
                raise WorkspaceBrokerError("private writer needs recovery")
            return synchronize(workspace_id, **kwargs)

        monkeypatch.setattr(service.shared_runner.isolated_sync, "synchronize", sync)
        try:
            await asyncio.wait_for(
                await _start(service, root, credentials, "task", ("*",)), 30
            )
            _assert_completed(service, "task")
            assert (root / "a.py").read_text() == "published\n"
            assert (private / "a.py").read_text() == "original\n"
            assert any(
                event.event_type == "workspace.isolated_sync_pending"
                for event in service.store.list_task_events("task")
            )
            with service.store.connection() as con:
                con.execute(
                    "UPDATE task_executions SET state='publishing',"
                    "boot_id='prior-boot' WHERE task_id='task'"
                )
            outcomes = service.shared_runner.recover(lambda driver: None)
            assert len(outcomes) == 1 and outcomes[0].resolution == "confirmed"
            _assert_completed(service, "task")
        finally:
            service.close()

    asyncio.run(scenario())


def test_isolated_helper_command_notifies_parent_when_pending_edits_are_reconciled(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from test_shared_explore import Provider

    from llm_cli.agent.tools import ToolBroker
    from llm_cli.execution import commands, shared
    from llm_cli.providers.base import ModelTurn

    async def scenario() -> None:
        root = repository_factory(tmp_path, {"docs/guide.md": "first\nmiddle\nlast\n"})
        provider = Provider(
            "isolated-helper",
            [
                _tools(
                    _call("read_file", path="docs/guide.md"),
                    _call(
                        "write_file",
                        path="docs/guide.md",
                        content="local\nmiddle\nlast\n",
                    ),
                ),
                _tools(_call("explore", task="Inspect command")),
                ModelTurn(text="Done."),
            ],
            [
                _tools(_call("run_command", argv=["cat", "docs/guide.md"])),
                ModelTurn(text="Done."),
            ],
        )
        send = provider.helper.send_user

        def send_after_primary_change(text: str) -> ModelTurn:
            (root / "docs/guide.md").write_text("first\nmiddle\nremote\n")
            return send(text)

        notices: list[str] = []
        notify = ToolBroker.notify_parent

        def collect_notice(broker: ToolBroker, text: str) -> None:
            notices.append(text)
            notify(broker, text)

        monkeypatch.setattr(provider.helper, "send_user", send_after_primary_change)
        monkeypatch.setattr(ToolBroker, "notify_parent", collect_notice)
        monkeypatch.setattr(shared, "available_sandbox", lambda: "test-sandbox")
        monkeypatch.setattr(
            commands, "wrap", lambda kind, policy, argv, **kwargs: list(argv)
        )
        service = service_factory(tmp_path)
        assert service.shared_runner.commands is not None
        service.shared_runner.commands = replace(
            service.shared_runner.commands, approval="allow"
        )
        service.providers.register("scripted", lambda model: provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials, _ = await open_session(service, root, provider.model)
        try:
            await asyncio.wait_for(
                await _start(service, root, credentials, "helper-task", ("docs/",)), 30
            )
            _assert_completed(service, "helper-task")
            assert len(notices) == 1
            assert "docs/guide.md" in notices[0]
            assert "your edits were merged" in notices[0]
            assert "local\nmiddle\nremote" in provider.helper.results[0][0].content
            assert (root / "docs/guide.md").read_text() == "local\nmiddle\nremote\n"
        finally:
            service.close()

    asyncio.run(scenario())


def test_before_model_sync_error_does_not_fail_isolated_task(
    tmp_path: Path, repository_factory: Any, service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.errors import ErrorCode, LlmCoordError

    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "original\n"})
        provider = ScriptedProvider(
            "isolated-save-race",
            [_tools(_call("read_file", path="a.py")), _finish()],
        )
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials, _ = await open_session(service, root, provider.model)
        sync = service.shared_runner.isolated_sync
        original = sync.refresh_capture
        calls = 0

        def raced(path: Path, protected: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise LlmCoordError(
                    ErrorCode.REPOSITORY_UNSAFE,
                    "source changed before isolated snapshot",
                )
            return original(path, protected)

        monkeypatch.setattr(sync, "refresh_capture", raced)
        try:
            await asyncio.wait_for(
                await _start(service, root, credentials, "save-race", ("*",)), 30
            )
            _assert_completed(service, "save-race")
            assert calls > 1
            assert not provider.steps
            assert provider.results and not provider.results[0][0].is_error
        finally:
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("incoming_source", [False, True])
def test_isolated_note_combines_with_quiet_coordination_refresh(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    monkeypatch: pytest.MonkeyPatch,
    incoming_source: bool,
) -> None:
    from llm_cli.agent.driver import CoordinationUpdate
    from llm_cli.workspace.context import SharedCoordinationContext

    async def scenario() -> None:
        original = "original source\n"
        received = "incoming source\n"
        root = repository_factory(tmp_path, {"docs/guide.md": original})
        updates: list[tuple[int | None, CoordinationUpdate]] = []
        other_sequences: list[int] = []
        compile_context = SharedCoordinationContext.__call__

        def capture_update(
            context: SharedCoordinationContext, after: int | None
        ) -> CoordinationUpdate:
            update = compile_context(context, after)
            updates.append((after, update))
            return update

        monkeypatch.setattr(SharedCoordinationContext, "__call__", capture_update)

        def change_after_initial_request(results: Any) -> Any:
            assert not results
            if incoming_source:
                (root / "docs/guide.md").write_text(received)
            with service.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                event = service.store.append_checkout_event(
                    connection,
                    checkout_id=session.checkout_id,
                    event_type="workspace.external_changed",
                    caused_by_session_id="other",
                    payload={
                        "path": "docs/guide.md",
                        "affected_session_id": "other",
                    },
                    now=1,
                )
                connection.commit()
            other_sequences.append(event.sequence)
            return _tools(
                _call(
                    "update_plan",
                    steps=[{"step": "inspect source", "status": "in_progress"}],
                )
            )

        def next_provider_request(results: Any) -> Any:
            assert len(results) == 1 and not results[0].is_error
            if incoming_source:
                assert "Received shared changes: docs/guide.md" in results[0].content
                assert "[Coordination update:" in results[0].content
                assert (private / "docs/guide.md").read_text() == received
            else:
                assert "Received shared changes:" not in results[0].content
                assert "[Coordination update:" not in results[0].content
                assert (private / "docs/guide.md").read_text() == original
            return _finish()

        provider = ScriptedProvider(
            "isolated-quiet-note", [change_after_initial_request, next_provider_request]
        )
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials, private = await open_session(service, root, provider.model)
        session = service.store.get_session(credentials["session_id"])
        assert session is not None
        try:
            await asyncio.wait_for(
                await _start(service, root, credentials, "quiet-note", ("docs/",)), 30
            )
            _assert_completed(service, "quiet-note")
            assert not provider.steps and len(provider.results) == 1
            assert len(other_sequences) == 1
            quiet = [
                update
                for after, update in updates
                if after is not None and update.sequence == other_sequences[0]
            ]
            assert quiet and quiet[0].facts_unchanged and quiet[0].text is None
            notices = [
                event
                for event in service.store.list_task_events("quiet-note")
                if event.event_type == "workspace.isolated_synchronized"
            ]
            assert bool(notices) is incoming_source
        finally:
            service.close()

    asyncio.run(scenario())
