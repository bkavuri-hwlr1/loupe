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
