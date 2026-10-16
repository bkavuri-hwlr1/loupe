"""Isolated review/apply survives restart and preserves later private work."""

from __future__ import annotations

import asyncio
import hashlib
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

from llm_cli.errors import LlmCoordError

BASE = "value = 1\n\nmiddle = 'old'\n\nname = 'old'\n"
PROPOSAL = BASE.replace("value = 1", "value = 2")
SECRET = "held-proposal-private-runtime"


async def prepare(service: Any, root: Path) -> tuple[dict[str, str], Path]:
    provider = ScriptedProvider(
        "isolated-review",
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
    creds = {"session_id": provider.model, "resume_secret": "fixture-secret"}
    opened = await service.handle(
        _request(
            "session.open",
            {
                "path": str(root),
                "session_id": provider.model,
                "resume_token_hash": hashlib.sha256(
                    creds["resume_secret"].encode()
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
        _request("session.ack", {**creds, "sequence": opened["bootstrap_sequence"]})
    )
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
                                "from pathlib import Path; "
                                "print('CHECK_DETAIL: validating a.py'); "
                                "assert 'value = 2' in Path('a.py').read_text(); "
                                f"assert Path('.env').read_text() == {SECRET!r}; "
                                "print(Path('.env').read_text())",
                            ]
                        }
                    },
                },
            },
        )
    )
    await asyncio.wait_for(await _start(service, root, creds, "held", ("*",)), 30)
    row = service.workflow.get("held")
    assert row is not None and row["status"] == "awaiting_review", row
    assert (root / "a.py").read_text() == BASE
    return creds, Path(opened["workspace"]["canonical_path"])


def test_apply_after_restart_checks_frozen_shared_proposal_and_preserves_private_edits(
    tmp_path: Path, repository_factory: Any, service_factory: Any
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": BASE, ".gitignore": ".env\n"})
        (root / ".env").write_text(SECRET)
        service = service_factory(tmp_path)
        _, private = await prepare(service, root)
        # Work after this held task is not part of its frozen proposal.
        (private / "a.py").write_text(
            BASE.replace("middle = 'old'", "middle = 'private'")
        )
        (private / "later.py").write_text("private later task\n")
        (root / "a.py").write_text(BASE.replace("name = 'old'", "name = 'shared'"))
        (root / ".env").write_text("new shared runtime must not be used")
        service.close()
        service = service_factory(tmp_path)
        service.initialize()
        try:
            result = await service.handle(_request("task.apply", {"task_id": "held"}))
            assert result["state"] == "published"
            expected = PROPOSAL.replace("name = 'old'", "name = 'shared'")
            assert (root / "a.py").read_text() == expected
            assert (private / "a.py").read_text() == expected.replace(
                "middle = 'old'", "middle = 'private'"
            )
            assert not (root / "later.py").exists()
            assert (private / "later.py").read_text() == "private later task\n"
            assert (private / ".env").read_text() == SECRET
            assert (root / ".env").read_text() == "new shared runtime must not be used"
            with service.store.connection() as con:
                results = con.execute(
                    "SELECT state,output FROM check_runs WHERE task_id='held'"
                ).fetchall()
            assert len(results) >= 2 and all(row[0] == "passed" for row in results)
            assert all(SECRET not in row[1] for row in results)
            undo = await service.handle(_request("task.undo", {"task_id": "held"}))
            assert undo["state"] == "published"
            assert (root / "a.py").read_text() == BASE.replace(
                "name = 'old'", "name = 'shared'"
            )
            assert (private / "a.py").read_text() == BASE.replace(
                "name = 'old'", "name = 'shared'"
            ).replace("middle = 'old'", "middle = 'private'")
            assert (private / "later.py").exists()
        finally:
            service.close()

    asyncio.run(scenario())


def test_held_isolated_apply_refuses_overlapping_shared_change(
    tmp_path: Path, repository_factory: Any, service_factory: Any
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": BASE, ".gitignore": ".env\n"})
        (root / ".env").write_text(SECRET)
        service = service_factory(tmp_path)
        try:
            await prepare(service, root)
            remote = BASE.replace("value = 1", "value = 3")
            (root / "a.py").write_text(remote)
            with pytest.raises((ValueError, LlmCoordError), match="Conflict"):
                await service.handle(_request("task.apply", {"task_id": "held"}))
            assert (root / "a.py").read_text() == remote
            row = service.workflow.get("held")
            assert row is not None and row["status"] == "awaiting_review"
        finally:
            service.close()

    asyncio.run(scenario())


def test_closed_held_proposal_applies_cleans_private_runtime_and_can_undo(
    tmp_path: Path, repository_factory: Any, service_factory: Any
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": BASE, ".gitignore": ".env\n"})
        (root / ".env").write_text(SECRET)
        service = service_factory(tmp_path)
        creds, private = await prepare(service, root)
        workspace_id = service.store.get_session(creds["session_id"]).workspace_id
        # A new shared source file invalidates the retained check evidence,
        # exercising the value-aware check path during manual apply too.
        (root / "outside.txt").write_text("new shared source\n")
        await service.handle(_request("session.close", creds))
        assert private.exists() and (private / ".env").read_text() == SECRET
        service.close()
        service = service_factory(tmp_path)
        service.initialize()
        try:
            applied = await service.handle(_request("task.apply", {"task_id": "held"}))
            assert applied["state"] == "published"
            assert (root / "a.py").read_text() == PROPOSAL
            assert not private.exists()
            assert service.store.get_workspace(workspace_id).state == "removed"
            assert not list(service.isolated_workspaces.managed_root.rglob(".env"))
            with service.store.connection() as con:
                outputs = con.execute(
                    "SELECT output FROM check_runs WHERE task_id='held'"
                ).fetchall()
            assert len(outputs) >= 2
            assert all("CHECK_DETAIL: validating a.py" in row[0] for row in outputs)
            assert all(SECRET not in row[0] for row in outputs)
            undone = await service.handle(_request("task.undo", {"task_id": "held"}))
            assert undone["state"] == "published"
            assert (root / "a.py").read_text() == BASE
            assert (root / ".env").read_text() == SECRET
            assert (root / "outside.txt").read_text() == "new shared source\n"
            assert not private.exists()
        finally:
            service.close()

    asyncio.run(scenario())
