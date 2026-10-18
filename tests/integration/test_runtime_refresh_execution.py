"""Runtime adoption is exercised through a scripted model and actual checks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
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

from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult
from llm_cli.workspace.runtime_refresh import RuntimeRefresher


@pytest.mark.parametrize("install_after_safe_point", [False, True])
def test_next_check_adopts_changed_and_newly_hydrated_runtime_files(
    tmp_path: Path,
    repository_factory: Any,
    service_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    install_after_safe_point: bool,
) -> None:
    async def scenario() -> None:
        root = repository_factory(
            tmp_path,
            {"a.py": "value = 1\n", ".gitignore": "runtime-*.cfg\n"},
        )
        (root / "runtime-current.cfg").write_text("before refresh\n")
        original_activity = RuntimeRefresher.activity
        raced = False

        @contextmanager
        def install_before_activity(
            runtime: RuntimeRefresher, workspace_id: str
        ) -> Iterator[None]:
            nonlocal raced
            if not raced:
                raced = True
                (root / "runtime-added.cfg").write_text("new runtime\n")
                # Another helper installs after safe_point read its path list.
                runtime.refresh(workspace_id)
            with original_activity(runtime, workspace_id):
                yield

        if install_after_safe_point:
            monkeypatch.setattr(RuntimeRefresher, "activity", install_before_activity)

        def update_runtime(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert all(not result.is_error for result in results), results
            # Simulate the user changing runtime inputs while the model thinks.
            (root / "runtime-current.cfg").write_text("after refresh\n")
            if not install_after_safe_point:
                (root / "runtime-added.cfg").write_text("new runtime\n")
            return _tools(
                ToolCallRequest("runtime-check", "run_check", {"name": "runtime"})
            )

        def finish(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert all(not result.is_error for result in results), results
            assert any("passed" in result.content.lower() for result in results)
            return _finish()

        provider = ScriptedProvider(
            "runtime-refresh-execution",
            [
                _tools(
                    _call("read_file", path="a.py"),
                    _call("write_file", path="a.py", content="value = 2\n"),
                ),
                update_runtime,
                finish,
            ],
        )
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        credentials = {
            "session_id": provider.model,
            "resume_secret": "runtime-refresh-session",
        }
        try:
            await service.handle(_request("repo.add", {"path": str(root)}))
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
                                        "assert Path('a.py').read_text() "
                                        "== 'value = 2\\n'; "
                                        "assert Path('runtime-current.cfg')"
                                        ".read_text() "
                                        "== 'after refresh\\n'; "
                                        "assert Path('runtime-added.cfg').read_text() "
                                        "== 'new runtime\\n'; "
                                        "print('Runtime adoption passed')",
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
                            credentials["resume_secret"].encode()
                        ).hexdigest(),
                        "provider": "scripted",
                        "model": provider.model,
                        "workspace": "isolated",
                        "mode": "auto",
                    },
                )
            )
            await service.handle(
                _request(
                    "session.ack",
                    {**credentials, "sequence": opened["bootstrap_sequence"]},
                )
            )
            task = await _start(service, root, credentials, "runtime-adoption", ("*",))
            await asyncio.wait_for(task, 30)
            _assert_completed(service, "runtime-adoption")
            row = service.workflow.get("runtime-adoption")
            assert row is not None
            config = json.loads(row["config_json"])
            assert set(config["runtime_paths"]) == {
                "runtime-current.cfg",
                "runtime-added.cfg",
            }
            assert (root / "a.py").read_text() == "value = 2\n"
            with service.store.connection() as con:
                checks = con.execute(
                    "SELECT state,output FROM check_runs WHERE task_id=?",
                    ("runtime-adoption",),
                ).fetchall()
            assert checks and all(check["state"] == "passed" for check in checks)
            assert any("Runtime adoption passed" in check["output"] for check in checks)
        finally:
            service.close()

    asyncio.run(scenario())
