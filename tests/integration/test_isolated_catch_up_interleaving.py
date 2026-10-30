"""Confirmed publication catch-up releases the barrier between receipt journals."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from test_isolated_publication import open_session
from test_isolated_review_workflow import BASE, PROPOSAL, SECRET, prepare
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

from llm_cli.daemon.service import DaemonService
from llm_cli.workspace.batches import MAX_BATCH_FILES, BatchFile
from llm_cli.workspace.identity import ABSENT, REGULAR_MODE, read_identified_path


@pytest.mark.parametrize("held", [False, True])
def test_confirmed_isolated_catch_up_allows_another_shared_publication(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    monkeypatch: pytest.MonkeyPatch,
    held: bool,
) -> None:
    async def scenario() -> None:
        root = repository_factory(
            tmp_path, {"a.py": BASE, "b.txt": "before\n", ".gitignore": ".env\n"}
        )
        service = service_factory(tmp_path)
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            if held:
                (root / ".env").write_text(SECRET)
                _, private = await prepare(service, root)
            else:
                provider = ScriptedProvider(
                    "isolated",
                    [
                        _tools(
                            _call("read_file", path="a.py"),
                            _call("write_file", path="a.py", content=PROPOSAL),
                        ),
                        _finish(),
                    ],
                )
                _register(service, provider)
                service.initialize()
                await service.handle(_request("repo.add", {"path": str(root)}))
                credentials, private = await open_session(service, root, provider.model)
            peer = {"session_id": "peer", "resume_secret": "fixture-peer-secret"}
            opened = await service.handle(
                _request(
                    "session.open",
                    {
                        "path": str(root),
                        "session_id": peer["session_id"],
                        "resume_token_hash": hashlib.sha256(
                            peer["resume_secret"].encode()
                        ).hexdigest(),
                        "provider": "scripted",
                        "model": "isolated-review" if held else "isolated",
                        "workspace": "shared",
                        "mode": "auto",
                    },
                )
            )
            await service.handle(
                _request(
                    "session.ack", {**peer, "sequence": opened["bootstrap_sequence"]}
                )
            )
            publisher = service.shared_runner.publisher
            publish = publisher.publish
            paths = [f"outside{number:03}.txt" for number in range(3 * MAX_BATCH_FILES)]
            seeded = False
            interleaved = False

            def seed_after_confirmation(*args: Any, **kwargs: Any) -> Any:
                nonlocal seeded
                result = publish(*args, **kwargs)
                if result.state == "published" and not seeded:
                    seeded = True
                    for offset in range(0, len(paths), MAX_BATCH_FILES):
                        files = tuple(
                            BatchFile(path, ABSENT, b"outside source\n", REGULAR_MODE)
                            for path in paths[offset : offset + MAX_BATCH_FILES]
                        )
                        assert (
                            publish(
                                f"seed-{offset}", peer["session_id"], files, ("*",)
                            ).state
                            == "published"
                        )
                return result

            def publish_between_receipts(workspace_id: str) -> None:
                nonlocal interleaved
                received = sum((private / path).exists() for path in paths)
                if not seeded or interleaved or not 0 < received < len(paths):
                    return
                interleaved = True
                assert _published_proposal(service, root)

                def publish_peer() -> None:
                    base, before = read_identified_path(root / "b.txt")
                    assert (
                        publisher.publish(
                            "interleaved-peer",
                            peer["session_id"],
                            (
                                BatchFile(
                                    "b.txt",
                                    base,
                                    b"interleaved\n",
                                    REGULAR_MODE,
                                    before,
                                ),
                            ),
                            ("*",),
                        ).state
                        == "published"
                    )

                pool.submit(publish_peer).result(timeout=3)
                assert received < len(paths)

            monkeypatch.setattr(publisher, "publish", seed_after_confirmation)
            monkeypatch.setattr(
                service.shared_runner.isolated_sync,
                "_between_journals",
                publish_between_receipts,
                raising=False,
            )
            if held:
                result = await service.handle(
                    _request("task.apply", {"task_id": "held"})
                )
                assert result["state"] == "published"
            else:
                await asyncio.wait_for(
                    await _start(service, root, credentials, "isolated-task", ("*",)),
                    30,
                )
                _assert_completed(service, "isolated-task")
            assert seeded and interleaved
            assert (private / "b.txt").read_text() == "interleaved\n"
            assert all(
                (private / path).read_text() == "outside source\n" for path in paths
            )
        finally:
            pool.shutdown(wait=True)
            service.close()

    asyncio.run(scenario())


def _published_proposal(service: DaemonService, root: Path) -> bool:
    assert (root / "a.py").read_text() == PROPOSAL
    with service.store.connection() as con:
        return (
            con.execute(
                "SELECT 1 FROM workspace_batches WHERE state='published' LIMIT 1"
            ).fetchone()
            is not None
        )
