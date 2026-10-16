"""Held proposals verify shared source with their own private runtime inputs."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from test_shared_agent_execution import _finish
from test_verified_workflow import await_task, edit_provider, setup

from llm_cli.agent.source_policy import register_runtime_paths
from llm_cli.execution import checks
from llm_cli.workspace.workflow import decode_files


def test_retained_check_uses_private_runtime_and_never_records_its_output(
    tmp_path: Path, repository_factory: Any, service_factory: Any
) -> None:
    async def scenario() -> None:
        root = repository_factory(
            tmp_path, {"a.py": "value = 1\n", ".gitignore": ".env\n"}
        )
        (root / ".env").write_text("shared runtime differs\n")
        private = tmp_path / "private-runtime"
        private.mkdir(mode=0o700)
        secret = "private-check-runtime-fixture"
        (private / ".env").write_text(secret)
        register_runtime_paths(private, (".env",))
        service = service_factory(tmp_path)
        creds = await setup(service, root, edit_provider(_finish()), review=True)
        await await_task(service, root, creds)
        row = service.workflow.get("task")
        assert row is not None and row["status"] == "awaiting_review"
        config = checks.validate_config(
            {
                "runtime_paths": [".env"],
                "checks": {
                    "private": {
                        "argv": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; "
                            "assert Path('a.py').read_text() == 'value = 2\\n'; "
                            f"assert Path('.env').read_text() == {secret!r}; "
                            "print(Path('.env').read_text())",
                        ]
                    }
                },
            }
        )
        # Model this task's frozen configuration; changing the private inputs
        # must invalidate evidence without exposing them in a persisted hash.
        row["config_json"] = json.dumps(config)
        with service.store.connection() as connection:
            connection.execute(
                "UPDATE task_workflows SET config_json=? WHERE task_id='task'",
                (row["config_json"],),
            )
        files = decode_files(row["proposal_json"])
        failure = checks.check_retained_proposal(
            service.workflow,
            row,
            root,
            files,
            runtime_root=private,
            suppress_output=True,
        )
        assert failure is None
        assert checks.verification_current(
            service.store, row, root, files, runtime_root=private
        )
        assert not checks.verification_current(service.store, row, root, files)
        with service.store.connection() as connection:
            output = connection.execute(
                "SELECT output FROM check_runs WHERE task_id='task'"
            ).fetchall()
            events = connection.execute(
                "SELECT payload_json FROM task_events WHERE task_id='task'"
            ).fetchall()
        assert output and all(secret not in entry[0] for entry in output)
        assert all(secret not in entry[0] for entry in events)
        assert (root / "a.py").read_text() == "value = 1\n"
        assert (root / ".env").read_text() == "shared runtime differs\n"
        (private / ".env").write_text("new private runtime\n")
        assert not checks.verification_current(
            service.store, row, root, files, runtime_root=private
        )

    asyncio.run(scenario())
