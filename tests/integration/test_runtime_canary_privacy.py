"""The C6 canary exercises private-value filtering without a live model."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

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

from llm_cli.agent.source_policy import contains_secret_material
from llm_cli.cli.app import _watch
from llm_cli.cli.render import EventRenderer
from llm_cli.observability.logging import JsonLogger

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _script(name: str) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_scripted_c6_runtime_check_redacts_canary_before_storage_and_logs(
    tmp_path: Path, service_factory: Any
) -> None:
    benchmark = _script("live_benchmark")
    sessions = _script("live_sessions")

    async def scenario() -> None:
        _, experiments = sessions.load_experiments()
        experiment = next(e for e in experiments if e.id == "toolkit-runtime")
        repository = benchmark._fixture(tmp_path / "runtime", experiment.fixture)
        canary = sessions.prepare_runtime(repository, experiment)
        assert canary is not None and len(canary) >= 8
        assert not contains_secret_material(canary)
        assert not contains_secret_material((repository / ".env").read_bytes())
        value = canary.decode()
        source = "src/toolkit/__init__.py"
        changed = (repository / source).read_text() + "\n# Scripted privacy check.\n"
        provider = ScriptedProvider(
            "c6-runtime-privacy",
            [
                _tools(
                    _call("read_file", path=source),
                    _call("write_file", path=source, content=changed),
                ),
                _finish(),
            ],
        )
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        logger = JsonLogger(service.paths.log_file)
        logger.write("daemon.ready", provider="scripted")
        credentials = {
            "session_id": provider.model,
            "resume_secret": "scripted-c6-session",
        }
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            config = tomllib.loads(
                "runtime_paths = "
                + json.dumps(list(experiment.runtime_paths))
                + "\n"
                + benchmark.checks_toml(experiment.checks, sys.executable)
            )
            assert config["checks"]["runtime"].get("required", True)
            await service.handle(
                _request(
                    "checks.configure", {"path": str(repository), "config": config}
                )
            )
            opened = await service.handle(
                _request(
                    "session.open",
                    {
                        "path": str(repository),
                        "session_id": provider.model,
                        "resume_token_hash": hashlib.sha256(
                            credentials["resume_secret"].encode()
                        ).hexdigest(),
                        "provider": "scripted",
                        "model": provider.model,
                        "workspace": "isolated",
                        "mode": experiment.mode,
                        "approved_runtime_files": list(
                            experiment.approved_runtime_files
                        ),
                    },
                )
            )
            await service.handle(
                _request(
                    "session.ack",
                    {**credentials, "sequence": opened["bootstrap_sequence"]},
                )
            )
            # Autostart redirects the daemon's stdout and stderr to this sink.
            # Exercise that sink as well as structured lifecycle logging.
            with (
                service.paths.log_file.open("a") as daemon_log,
                redirect_stdout(daemon_log),
                redirect_stderr(daemon_log),
            ):
                work = await _start(
                    service, repository, credentials, "c6-canary-task", ("*",)
                )
                await asyncio.wait_for(work, 60)
            _assert_completed(service, "c6-canary-task")
            with service.store.connection() as connection:
                stored = connection.execute(
                    "SELECT run_id,name,state,output FROM check_runs WHERE task_id=?",
                    ("c6-canary-task",),
                ).fetchall()
                dump = "\n".join(connection.iterdump())
            assert {row["name"] for row in stored} == {"test", "runtime"}
            assert all(row["state"] == "passed" for row in stored)
            events = [
                asdict(event)
                for event in service.store.list_task_events("c6-canary-task")
            ]
            # Both the fixture's pytest check and the separate frozen runtime
            # check print once. Their successful output must reach each durable
            # sink with a replacement, rather than being withheld wholesale.
            for row in stored:
                output = row["output"]
                assert output.count("[private value]") == 1, output
                assert "C6 runtime read passed" in output
                assert "withheld" not in output
                emitted = "".join(
                    str(event["payload"]["text"])
                    for event in events
                    if event["event_type"] == "check.output"
                    and event["payload"]["run_id"] == row["run_id"]
                )
                assert emitted.count("[private value]") == 1, emitted
                assert "C6 runtime read passed" in emitted
                assert value not in emitted
            assert value not in dump
            assert value not in repr(events)
            assert value not in repr(provider.results)
            assert not sessions._database_contains_canary(
                service.paths.control_db, canary
            )

            chat_log = repository.parent / "chat.log"
            with chat_log.open("w") as stream:
                renderer = EventRenderer(stream, plain=True)
                for event in events:
                    renderer.render(event)
                renderer.finish()
            assert "Checks passed" in chat_log.read_text()
            # Task-watch JSON is an actual CLI output path that includes check
            # output; human chat intentionally hides successful check details.
            watch_log = repository.parent / "task-watch.log"
            client = SimpleNamespace(
                stream=lambda *args: iter(events),
                call=lambda *args: {"state": "completed"},
            )
            with watch_log.open("w") as stream, redirect_stdout(stream):
                _watch(client, "c6-canary-task", 0, as_json=True)
            assert "[private value]" in watch_log.read_text()
            logger.write("daemon.stopped", provider="scripted")
            for log in (service.paths.log_file, chat_log, watch_log):
                assert log.stat().st_size > 0
                assert not sessions._contains_canary(log, canary)
            assert (repository / ".env").read_bytes() == (
                b"LOUPE_C6_CANARY=" + canary + b"\n"
            )
        finally:
            service.close()

    asyncio.run(scenario())
