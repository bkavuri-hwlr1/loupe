"""Isolated sessions keep exact source and local runtime across task boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from test_shared_agent_execution import (
    ScriptedProvider,
    _call,
    _finish,
    _register,
    _request,
    _start,
    _tools,
)
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.workspace.isolated import IsolatedWorkspaceManager


def _manager(env: BatchEnvironment, tmp_path: Path) -> IsolatedWorkspaceManager:
    return IsolatedWorkspaceManager(
        env.store,
        tmp_path / "private",
        env.publisher.candidate_dir,
        env.publisher.lock,
        lambda workspace: None,
    )


def _checkout(env: BatchEnvironment) -> Any:
    workspace = env.store.get_workspace(env.workspace_id)
    assert workspace is not None
    return env.store.get_checkout(workspace.checkout_id)


@pytest.mark.parametrize(
    "phase", ["planned", "registered", "materialized", "hydrated", "ready"]
)
def test_each_creation_crash_recovers_or_cleans_owned_files(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    phase: str,
) -> None:
    env = batch_env
    (env.repository / ".gitignore").write_text(".env\n")
    (env.repository / ".env").write_text("LOCAL_TOKEN=fixture\n")
    manager = _manager(env, tmp_path)

    class Crash(BaseException):
        pass

    def fail(stage: str) -> None:
        if stage == phase:
            raise Crash()

    manager.fault = fail
    with pytest.raises(Crash):
        manager.open(_checkout(env), "crash", approved_secrets=(".env",))
    _manager(env, tmp_path).recover()
    with env.store.connection() as con:
        rows = con.execute(
            "SELECT workspace_id,stage FROM isolated_workspaces"
        ).fetchall()
    if phase == "ready":
        assert len(rows) == 1 and rows[0]["stage"] == "ready"
        _manager(env, tmp_path).validate(rows[0]["workspace_id"])
    else:
        assert not rows
        assert not list((tmp_path / "private").glob("*"))
        # A known interrupted creation can be retried without a manual cleanup.
        manager.fault = lambda stage: None
        manager.open(_checkout(env), "crash", approved_secrets=(".env",))
    assert (env.repository / "a.txt").read_text() == "old a\n"
    assert (env.repository / ".env").read_text() == "LOCAL_TOKEN=fixture\n"


def test_typed_source_is_materialized_without_following_links(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    env = batch_env
    (env.repository / "source-link").symlink_to("a.txt")
    binary = b"\x00\xff\x80image bytes"
    (env.repository / "asset.bin").write_bytes(binary)
    workspace, notices = _manager(env, tmp_path).open(_checkout(env), "typed")
    root = Path(workspace.canonical_path)
    assert os.readlink(root / "source-link") == "a.txt"
    assert (root / "asset.bin").read_bytes() == binary
    assert not any("source-link" in notice for notice in notices)


def test_isolated_task_checks_use_env_without_publishing_or_disclosing_it(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
) -> None:
    async def scenario() -> None:
        root = repository_factory(
            tmp_path, {"a.py": "value = 1\n", ".gitignore": ".env\n"}
        )
        secret = "opaque-test-runtime-value"
        (root / ".env").write_text(secret)
        service = service_factory(tmp_path)
        provider = ScriptedProvider(
            "isolated-checks",
            [
                _tools(
                    _call("read_file", path="a.py"), _call("read_file", path=".env")
                ),
                _tools(_call("write_file", path="a.py", content="value = 2\n")),
                _finish(),
            ],
        )
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        credentials = {"session_id": provider.model, "resume_secret": "isolated-secret"}
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
                    "mode": "normal",
                    "approved_runtime_files": [".env"],
                },
            )
        )
        await service.handle(
            _request(
                "session.ack", {**credentials, "sequence": opened["bootstrap_sequence"]}
            )
        )
        await service.handle(
            _request(
                "checks.configure",
                {
                    "path": str(root),
                    "config": {
                        "checks": {
                            "runtime": {
                                "argv": [
                                    sys.executable,
                                    "-c",
                                    "from pathlib import Path; "
                                "assert Path('a.py').read_text() == 'value = 2\\n'; "
                                "data = Path('.env').read_text(); "
                                "assert data; print(data); "
                                "Path('.env').write_text('changed only in check')",
                                ],
                            }
                        }
                    },
                },
            )
        )
        try:
            work = await _start(service, root, credentials, "isolated-task", ("*",))
            await asyncio.wait_for(work, 30)
            rows = service.workflow.checks("isolated-task")
            assert rows and rows[0]["state"] == "passed", rows
            assert secret not in repr(rows)
            assert secret not in repr(service.store.list_task_events("isolated-task"))
            assert secret not in repr(provider.results)
            assert provider.results[0][1].is_error
            assert (root / "a.py").read_text() == "value = 1\n"
            assert (root / ".env").read_text() == secret
            private = Path(opened["workspace"]["canonical_path"])
            assert (private / ".env").read_text() == secret
            row = service.workflow.get("isolated-task")
            assert row is not None and row["status"] == "awaiting_review"
        finally:
            service.close()

    asyncio.run(scenario())
