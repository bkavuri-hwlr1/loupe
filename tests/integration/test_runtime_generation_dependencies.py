"""Retained generations preserve dependency relocation and source link boundaries."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_runtime_refresh import prepare
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.workspace.isolated import copy_runtime_files


def retain(env: BatchEnvironment, isolate: Any, workspace_id: str) -> Path:
    checkout = env.store.get_checkout(env.store.get_workspace(workspace_id).checkout_id)
    env.store.create_task(
        repository_id=checkout.repository_id, task_id="frozen-runtime"
    )
    return isolate.runtime.pin_task(workspace_id, "frozen-runtime", 1)


def test_retained_venv_relocates_into_check_snapshot(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(env.repository / ".venv")],
        check=True,
    )
    launcher = env.repository / ".venv/bin/private-check"
    launcher.write_text(
        "#!"
        + str(env.repository / ".venv/bin/python")
        + "\nprint('frozen dependency works')\n"
    )
    launcher.chmod(0o755)
    isolate, workspace, private = prepare(env, tmp_path)
    assert (private / ".venv").exists()
    frozen = retain(env, isolate, workspace.workspace_id)
    destination = tmp_path / "check"
    destination.mkdir(mode=0o700)
    copy_runtime_files(frozen, destination, (".venv",))
    completed = subprocess.run(
        [str(destination / ".venv/bin/private-check")], capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "frozen dependency works\n"
    assert str(frozen) not in (destination / ".venv/bin/private-check").read_text()


def test_frozen_workspace_link_validates_final_snapshot_source(
    batch_env: BatchEnvironment, tmp_path: Path, repository_factory: Any
) -> None:
    env = batch_env
    package = env.repository / "packages/local"
    package.mkdir(parents=True)
    (package / "entry.py").write_text("answer = 42\n")
    modules = env.repository / "node_modules"
    modules.mkdir()
    (modules / "local").symlink_to("../packages/local")
    isolate, workspace, private = prepare(env, tmp_path)
    frozen = retain(env, isolate, workspace.workspace_id)
    # Later private source edits are independent of the held proposal. A check
    # uses its merged source snapshot, which still contains the original package.
    (private / "packages/local/entry.py").unlink()
    (private / "packages/local").rmdir()
    (private / "packages").rmdir()
    destination = repository_factory(
        tmp_path / "check", {"packages/local/entry.py": "answer = 42\n"}
    )
    copy_runtime_files(frozen, destination, ("node_modules",))
    assert (destination / "node_modules/local/entry.py").read_text() == "answer = 42\n"
    assert (
        destination / "node_modules/local"
    ).resolve() == destination / "packages/local"
    # The final source boundary still refuses a missing or excluded target.
    refused = repository_factory(tmp_path / "missing-source", {"a.py": "pass\n"})
    with pytest.raises(Exception, match="unavailable"):
        copy_runtime_files(frozen, refused, ("node_modules",))


@pytest.mark.parametrize("venv", [False, True])
@pytest.mark.parametrize("added_source", [False, True])
def test_actual_held_apply_checks_frozen_workspace_dependency_against_merged_source(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
    venv: bool,
    added_source: bool,
) -> None:
    import asyncio
    import hashlib

    from test_shared_agent_execution import (
        ScriptedProvider,
        _call,
        _finish,
        _register,
        _request,
        _start,
        _tools,
    )

    async def scenario() -> None:
        root = repository_factory(
            tmp_path,
            {
                "a.py": "value = 1\n",
                (
                    "packages/local/README.md"
                    if added_source
                    else "packages/local/value.py"
                ): ("package placeholder\n" if added_source else "answer = 42\n"),
                ".gitignore": "node_modules/\n.venv/\n",
            },
        )
        modules = root / "node_modules"
        modules.mkdir()
        (modules / "local").symlink_to("../packages/local")
        python = sys.executable
        if venv:
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")],
                check=True,
            )
            python = ".venv/bin/python"
        calls = [
            _call("read_file", path="a.py"),
            _call("write_file", path="a.py", content="value = 2\n"),
        ]
        if added_source:
            calls.append(
                _call(
                    "write_file",
                    path="packages/local/value.py",
                    content="answer = 42\n",
                )
            )
        provider = ScriptedProvider("held-node-runtime", [_tools(*calls), _finish()])
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        creds = {"session_id": provider.model, "resume_secret": "held-runtime-session"}
        try:
            await service.handle(_request("repo.add", {"path": str(root)}))
            await service.handle(
                _request(
                    "checks.configure",
                    {
                        "path": str(root),
                        "config": {
                            "checks": {
                                "linked": {
                                    "argv": [
                                        python,
                                        "-c",
                                        "from pathlib import Path; "
                                        "assert Path('a.py').read_text() "
                                        "== 'value = 2\\n'; "
                                        "assert Path('node_modules/local/value.py')"
                                        ".read_text() == 'answer = 42\\n'; "
                                        "print('held workspace dependency '"
                                        "'check passed')",
                                    ],
                                    "required": True,
                                }
                            }
                        },
                    },
                )
            )
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
                    },
                )
            )
            await service.handle(
                _request(
                    "session.ack", {**creds, "sequence": opened["bootstrap_sequence"]}
                )
            )
            await asyncio.wait_for(
                await _start(service, root, creds, "held-linked", ("*",)), 30
            )
            row = service.workflow.get("held-linked")
            assert row is not None and row["status"] == "awaiting_review", row
            private = Path(opened["workspace"]["canonical_path"])
            frozen = service.isolated_workspaces.runtime.task_root(
                opened["workspace"]["workspace_id"], "held-linked", 1
            )
            assert not (frozen / "packages/local").exists()
            # These later private source deletions never enter the held action.
            for path in (private / "packages/local").iterdir():
                path.unlink()
            (private / "packages/local").rmdir()
            (private / "packages").rmdir()
            # Force a genuine rerun, not reuse of the original task evidence.
            (root / "later_source.py").write_text(
                "unrelated = 'current shared source'\n"
            )
            if added_source:
                assert not (root / "packages/local/value.py").exists()
            service.close()
            service = service_factory(tmp_path)
            service.initialize()
            result = await service.handle(
                _request("task.apply", {"task_id": "held-linked"})
            )
            assert result["state"] == "published"
            assert (root / "a.py").read_text() == "value = 2\n"
            assert (root / "packages/local/value.py").read_text() == "answer = 42\n"
            assert not (private / "packages/local").exists()
            with service.store.connection() as con:
                rows = con.execute(
                    "SELECT state,output FROM check_runs WHERE task_id='held-linked'"
                ).fetchall()
            assert len(rows) >= 2 and all(row[0] == "passed" for row in rows)
            assert all(
                "held workspace dependency check passed" in row[1] for row in rows
            )
        finally:
            service.close()

    asyncio.run(scenario())
