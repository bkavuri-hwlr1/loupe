"""Actual CLI/daemon runtime exports keep bytes out of durable authority sinks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from test_cli_process_orchestration import _Cluster, _wait
from test_cli_process_orchestration import cluster as cluster
from test_shared_agent_execution import ScriptedProvider, _register, _request

from llm_cli.agent.source_policy import contains_secret_material
from llm_cli.errors import LlmCoordError
from llm_cli.workspace import runtime_export
from llm_cli.workspace.isolated import capture_source


def _paused_runtime_export_daemon_entry() -> None:
    from test_cli_process_orchestration import _daemon_entry

    original = runtime_export._rename_new

    def paused(*args: Any, **kwargs: Any) -> None:
        signals = Path(os.environ["ORCHESTRATION_SIGNALS"])
        (signals / "export-stage-ready").touch()
        while not (signals / "export-stage-release").exists():
            time.sleep(0.01)
        original(*args, **kwargs)

    runtime_export._rename_new = paused
    _daemon_entry()


class _PausedExportCluster(_Cluster):
    def _spawn(self, name: str, args: list[str]) -> Any:
        if name == "daemon":
            args = [
                "-c",
                "from test_runtime_export_cli import "
                "_paused_runtime_export_daemon_entry; "
                "_paused_runtime_export_daemon_entry()",
            ]
        return super()._spawn(name, args)


def _cli(cluster: _Cluster, name: str, arguments: list[str]) -> dict[str, Any]:
    process = cluster._spawn(
        name, ["-m", "llm_cli", "--profile", "test", "--json", *arguments]
    )
    assert process.wait(timeout=30) == 0, (cluster.root / (name + ".log")).read_text()
    return json.loads((cluster.root / (name + ".log")).read_text())


def test_actual_daemon_cli_changed_env_requires_both_flags_and_persists_only_metadata(
    cluster: _Cluster,
) -> None:
    # _Cluster starts the real daemon through install_test_authority; the
    # installed editable package also works when its CLI child uses Python -I.
    root = cluster.repository
    (root / ".gitignore").write_text(".env\nbuild/\n")
    (root / ".env").write_text("MARKER=opening-runtime\n")
    (root / "build").mkdir()
    (root / "build" / "output").write_bytes(b"initial build")
    session_id, secret = "runtime-export-process", "local-resume-secret"
    cluster.rpc("repo.add", path=str(root))
    opened = cluster.rpc(
        "session.open",
        path=str(root),
        session_id=session_id,
        resume_token_hash=hashlib.sha256(secret.encode()).hexdigest(),
        provider="process-test",
        model="export-only-no-model-calls",
        workspace="isolated",
        approved_runtime_files=[".env"],
    )
    credentials = {"session_id": session_id, "resume_secret": secret}
    cluster.rpc("session.ack", **credentials, sequence=opened["bootstrap_sequence"])
    cluster.paths.session_secret_file(session_id).write_text(secret)
    cluster.paths.session_secret_file(session_id).chmod(0o600)
    private = Path(opened["workspace"]["canonical_path"])
    canary = "c15-ordinary-nonsignature-runtime-marker-a56e2d91"
    assert not contains_secret_material(canary)
    payload = "MARKER=" + canary + "\n"
    (private / ".env").write_text(payload)
    (private / "build" / "output").write_bytes(b"regenerated build\x00\xff")
    destination = cluster.root / "kept-runtime-env"
    args = [
        "workspace",
        "runtime",
        "export",
        ".env",
        "--session",
        session_id,
        "--to",
        str(destination),
    ]
    for index, flags in enumerate(([], ["--yes"], ["--allow-secret"])):
        preview = _cli(cluster, f"preview-{index}", [*args, *flags])
        assert preview["state"] == "confirmation_required"
        assert preview["sensitivity"] == "likely_secret"
        assert not destination.exists()
        assert canary not in repr(preview)
    copied = _cli(cluster, "export-env", [*args, "--yes", "--allow-secret"])
    assert copied["state"] == "exported" and copied["bytes"] == len(payload)
    assert destination.read_text() == payload
    assert (root / ".env").read_text() == "MARKER=opening-runtime\n"
    built = cluster.root / "kept-build"
    result = _cli(
        cluster,
        "export-build",
        [
            "workspace",
            "runtime",
            "export",
            "build",
            "--session",
            session_id,
            "--to",
            str(built),
            "--yes",
        ],
    )
    assert result["state"] == "exported"
    assert (built / "output").read_bytes() == b"regenerated build\x00\xff"
    signed_canary = "AKIA" + "Q" * 16
    signed_path = private / "build" / "credentials-data.json"
    signed_path.write_text(" " * (2 * 1024 * 1024) + signed_canary + "\n")
    signed_destination = cluster.root / "kept-signature-runtime"
    signed_args = [
        "workspace",
        "runtime",
        "export",
        "build/credentials-data.json",
        "--session",
        session_id,
        "--to",
        str(signed_destination),
        "--yes",
    ]
    signature_preview = _cli(cluster, "signature-preview", signed_args)
    assert signature_preview["sensitivity"] == "likely_secret"
    assert signature_preview["requires_allow_secret"]
    assert not signed_destination.exists()
    assert (
        _cli(cluster, "signature-export", [*signed_args, "--allow-secret"])["state"]
        == "exported"
    )
    assert signed_destination.read_bytes() == signed_path.read_bytes()
    events = cluster.rpc("session.events", **credentials, after=0, limit=500)
    exports = [
        event for event in events if event["event_type"] == "session.runtime_exported"
    ]
    assert len(exports) == 3
    for event in exports:
        assert set(event["payload"]) == {
            "kind",
            "bytes",
            "entries",
            "sensitivity",
            "state",
        }
        assert canary not in repr(event)
        assert str(destination) not in repr(event)
        assert ".env" not in repr(event)
    with closing(sqlite3.connect(cluster.paths.control_db)) as connection:
        dump = "\n".join(connection.iterdump())
        assert canary not in dump and signed_canary not in dump
        assert (
            connection.execute("SELECT count(*) FROM workspace_batches").fetchone()[0]
            == 0
        )
    for path in cluster.paths.data_dir.rglob("*"):
        # Runtime worktrees and intentional export destinations are local byte
        # storage. SQLite, artifacts, candidates and logs must contain no value.
        if path.is_file() and not any(
            path.is_relative_to(cluster.paths.data_dir / name)
            for name in ("worktrees", "isolated-worktrees")
        ):
            assert canary.encode() not in path.read_bytes(), path
            assert signed_canary.encode() not in path.read_bytes(), path
    for path in [
        *cluster.root.glob("*.log"),
        *cluster.paths.state_dir.rglob("*.jsonl"),
    ]:
        assert canary not in path.read_text(), path
        assert signed_canary not in path.read_text(), path
    # Export audit events must not invalidate the next production coordination
    # packet or project runtime disposition metadata into model context.
    chat = cluster.chat("after-export-chat", resume=session_id)
    cluster.submit(chat, "after-export", {"docs/a.md": ""}, read_only=True)
    task = cluster.settled("after-export")
    assert task["state"] == "completed", cluster.diagnostics()
    evidence_path = cluster.signals / "after-export.observed.json"
    _wait(evidence_path.exists)
    evidence = evidence_path.read_text()
    for private_value in (
        canary,
        signed_canary,
        ".env",
        str(destination),
        "runtime_exported",
        "likely_secret",
    ):
        assert private_value not in evidence


def test_private_staging_survives_kill_until_owned_restart_close_cleanup(
    tmp_path: Path,
    repository_factory: Any,
) -> None:
    root = repository_factory(
        tmp_path,
        {
            "docs/a.md": "ordinary source\n",
            ".gitignore": ".env\nkept.env\n",
        },
    )
    (root / ".env").write_text("MARKER=opening-runtime\n")
    with TemporaryDirectory(prefix="c15-stage-", dir="/tmp") as runtime:
        group = _PausedExportCluster(tmp_path, root, runtime)
        try:
            session_id, secret = "paused-export", "paused-export-secret"
            credentials = {"session_id": session_id, "resume_secret": secret}
            group.rpc("repo.add", path=str(root))
            opened = group.rpc(
                "session.open",
                path=str(root),
                session_id=session_id,
                resume_token_hash=hashlib.sha256(secret.encode()).hexdigest(),
                provider="process-test",
                model="paused-export-model",
                workspace="isolated",
                approved_runtime_files=[".env"],
            )
            group.rpc(
                "session.ack", **credentials, sequence=opened["bootstrap_sequence"]
            )
            secret_file = group.paths.session_secret_file(session_id)
            secret_file.write_text(secret)
            secret_file.chmod(0o600)
            private = Path(opened["workspace"]["canonical_path"])
            canary = "c15-staged-nonsignature-private-marker-fb761a04"
            payload = "MARKER=" + canary + "\n"
            (private / ".env").write_text(payload)
            destination = root / "kept.env"
            exporting = group._spawn(
                "paused-export-cli",
                [
                    "-m",
                    "llm_cli",
                    "--profile",
                    "test",
                    "--json",
                    "workspace",
                    "runtime",
                    "export",
                    ".env",
                    "--session",
                    session_id,
                    "--to",
                    str(destination),
                    "--yes",
                    "--allow-secret",
                    "--into-checkout",
                ],
            )
            _wait((group.signals / "export-stage-ready").exists)
            with closing(sqlite3.connect(group.paths.control_db)) as connection:
                admin = Path(
                    connection.execute(
                        "SELECT git_dir FROM workspaces WHERE workspace_id=?",
                        (opened["workspace"]["workspace_id"],),
                    ).fetchone()[0]
                )
            staged = list(admin.glob("loupe-runtime-export-*/*"))
            assert len(staged) == 1 and staged[0].read_text() == payload
            assert staged[0].stat().st_mode & 0o777 == 0o600
            assert not destination.exists()
            assert not list(root.glob(".loupe-runtime-export-*"))
            captured = tmp_path / "capture-during-stage"
            captured.mkdir(mode=0o700)
            manifest = capture_source(root, captured)
            assert set(manifest) == {".gitignore", "docs/a.md"}
            assert all(
                canary.encode() not in path.read_bytes() for path in captured.iterdir()
            )
            chat = group.chat("while-export-chat", resume=session_id)
            group.submit(chat, "while-export", {"docs/a.md": ""}, read_only=True)
            assert group.settled("while-export")["state"] == "completed", (
                group.diagnostics()
            )
            model_evidence = group.signals / "while-export.observed.json"
            _wait(model_evidence.exists)
            assert canary not in model_evidence.read_text()
            group.daemon.kill()
            group.daemon.wait(timeout=10)
            assert exporting.wait(timeout=10) != 0
            assert staged[0].read_text() == payload
            assert not destination.exists()
            group.daemon = group._spawn(
                "restarted-daemon",
                [
                    "-c",
                    "from test_cli_process_orchestration import _daemon_entry; "
                    "_daemon_entry()",
                ],
            )

            def ready() -> bool:
                try:
                    group.rpc("system.ping")
                    return True
                except LlmCoordError:
                    return False

            _wait(ready)
            group.rpc("session.close", **credentials)
            assert not private.exists()
            assert not admin.exists()
            assert not destination.exists()
            with closing(sqlite3.connect(group.paths.control_db)) as connection:
                assert canary not in "\n".join(connection.iterdump())
            for path in group.paths.data_dir.rglob("*"):
                if path.is_file():
                    assert canary.encode() not in path.read_bytes(), path
            for path in [
                *group.root.glob("*.log"),
                *group.paths.state_dir.rglob("*.jsonl"),
            ]:
                assert canary not in path.read_text(), path
        finally:
            group.close()


def test_export_activity_defers_close_cleanup_and_does_not_hold_daemon_barrier(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.txt": "source", ".gitignore": ".env\n"})
        (root / ".env").write_text("MARKER=close-export-canary-764219\n")
        service = service_factory(tmp_path)
        provider = ScriptedProvider("export-only", [])
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials = {"session_id": provider.model, "resume_secret": "test-export"}
        opened = await service.handle(
            _request(
                "session.open",
                {
                    "path": str(root),
                    "session_id": provider.model,
                    "resume_token_hash": hashlib.sha256(
                        credentials["resume_secret"].encode()
                    ).hexdigest(),
                    "provider": "scripted",
                    "model": provider.model,
                    "workspace": "isolated",
                    "approved_runtime_files": [".env"],
                },
            )
        )
        await service.handle(
            _request(
                "session.ack", {**credentials, "sequence": opened["bootstrap_sequence"]}
            )
        )
        private = Path(opened["workspace"]["canonical_path"])
        entered, release = threading.Event(), threading.Event()
        original = runtime_export._copy_file

        def paused(*args: Any) -> None:
            entered.set()
            assert release.wait(10)
            original(*args)

        monkeypatch.setattr(runtime_export, "_copy_file", paused)
        task = asyncio.create_task(
            service.handle(
                _request(
                    "workspace.runtime.export",
                    {
                        **credentials,
                        "relative_path": ".env",
                        "destination": str(tmp_path / "exported"),
                        "yes": True,
                        "allow_secret": True,
                    },
                )
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            await asyncio.wait_for(service.handle(_request("system.ping", {})), 2)
            await asyncio.wait_for(
                service.handle(_request("session.close", credentials)), 2
            )
            assert private.is_dir()
            assert service.isolated_workspaces.runtime.busy(
                opened["workspace"]["workspace_id"]
            )
        finally:
            release.set()
        assert (await task)["state"] == "exported"
        assert not provider.results
        assert not service.isolated_workspaces.runtime.busy(
            opened["workspace"]["workspace_id"]
        )
        service.close()

    asyncio.run(scenario())


def test_export_rejects_shared_sessions_and_pending_switches(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
) -> None:
    async def scenario() -> None:
        root = repository_factory(
            tmp_path, {"a.txt": "source", ".gitignore": "build/\n"}
        )
        service = service_factory(tmp_path)
        provider = ScriptedProvider("export-only", [])
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials = {"session_id": provider.model, "resume_secret": "test-export"}
        opened = await service.handle(
            _request(
                "session.open",
                {
                    "path": str(root),
                    "session_id": provider.model,
                    "resume_token_hash": hashlib.sha256(
                        credentials["resume_secret"].encode()
                    ).hexdigest(),
                    "provider": "scripted",
                    "model": provider.model,
                    "workspace": "isolated",
                },
            )
        )
        await service.handle(
            _request(
                "session.ack", {**credentials, "sequence": opened["bootstrap_sequence"]}
            )
        )
        with service.store.connection() as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(mode_switch_intents)")
            }
            if "session_id" in columns:
                connection.execute(
                    "INSERT INTO mode_switch_intents("
                    "switch_intent_id,session_id,source_workspace_id,source_mode,"
                    "target_mode,source_binding_generation,source_scope_json,"
                    "request_json,classification,files_json,held_json,state,"
                    "created_at,updated_at) VALUES "
                    "('export-block',?,?,'isolated','shared',1,'{}','{}','clean',"
                    "'[]','[]','requested',0,0)",
                    (provider.model, opened["workspace"]["workspace_id"]),
                )
            else:
                connection.execute(
                    "CREATE TABLE mode_switch_intents("
                    "source_workspace_id TEXT, state TEXT)"
                )
                connection.execute(
                    "INSERT INTO mode_switch_intents VALUES (?, 'requested')",
                    (opened["workspace"]["workspace_id"],),
                )
        try:
            with pytest.raises(LlmCoordError, match="pending mode switch"):
                await service.handle(
                    _request(
                        "workspace.runtime.export",
                        {
                            **credentials,
                            "relative_path": "build",
                            "destination": str(tmp_path / "exported"),
                        },
                    )
                )
        finally:
            service.close()

    asyncio.run(scenario())
