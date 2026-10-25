"""Real isolated daemons exercise managed writers without billed model calls."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from llm_cli.agent.source_policy import contains_secret_material
from llm_cli.errors import LlmCoordError
from llm_cli.execution.sandbox import available_sandbox
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient
from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult
from llm_cli.workspace.authority import process_start_identity
from llm_cli.workspace.identity import identify_path

_SETTLED = {"completed", "failed", "cancelled", "awaiting_review"}
_PYTHON = str(Path(sys.executable).resolve())
_BOOTSTRAP = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('managed_process_fixture', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module._daemon_entry(sys.argv[2])
"""
_HTTP_SERVER = """\
import os, signal, socket, sys
from pathlib import Path
signal.alarm(120)
assert os.environ['C14_PROJECT_MODE'] == 'opening-session'
from response import VALUE
root = Path(os.environ['LOUPE_PROCESS_SOCKET_DIR'])
server = socket.socket(socket.AF_UNIX)
server.bind(str(root / 'server.sock'))
server.listen(5)
print('c14-http-ready', flush=True)
while True:
    connection, _ = server.accept()
    with connection:
        request = connection.recv(4096)
        body = VALUE.encode()
        headers = (b'HTTP/1.0 200 OK\\r\\nContent-Length: '
                   + str(len(body)).encode() + b'\\r\\n\\r\\n')
        connection.sendall(headers + body)
    print('c14-http-served', flush=True)
"""
_HTTP_CLIENT = """\
import os, socket, time
from pathlib import Path
assert os.environ['C14_PROJECT_MODE'] == 'opening-session'
path = str(Path(os.environ['LOUPE_PROCESS_SOCKET_DIR']) / 'server.sock')
deadline = time.monotonic() + 4
while True:
    connection = socket.socket(socket.AF_UNIX)
    try:
        connection.connect(path)
        break
    except (FileNotFoundError, ConnectionRefusedError):
        connection.close()
        if time.monotonic() >= deadline:
            raise
        time.sleep(.02)
connection.sendall(b'GET / HTTP/1.0\\r\\nHost: local\\r\\n\\r\\n')
body = b''
while chunk := connection.recv(4096):
    body += chunk
print(body.decode(), flush=True)
"""
_WRITER = """\
import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.alarm(120)
root = Path('build')
root.mkdir(exist_ok=True)
child = os.fork()
if child == 0:
    signal.alarm(120)
    while True:
        with (root / 'heartbeat').open('ab') as handle:
            handle.write(b'child alive\\n')
        time.sleep(.02)
print('c14-writer-ready', flush=True)
while True:
    time.sleep(.02)
"""


def _wait(predicate: Callable[[], Any], *, timeout: float = 20) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("managed process fixture did not reach its state")


def _identity(pid: int) -> str | None:
    try:
        return process_start_identity(pid)
    except (OSError, ValueError, LlmCoordError):
        return None


def _json_prefix(text: str) -> dict[str, Any]:
    value, _ = json.JSONDecoder().raw_decode(text)
    assert isinstance(value, dict), text
    return value


class _ProcessProvider:
    """Only provider replies are scripted; all tools and storage are production."""

    name = "c14-fixture"

    def __init__(self, model: str) -> None:
        self.model = model
        self.plan: dict[str, Any] = {}
        self.history: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []
        self.phase = 0
        self.process_id = ""

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> _ProcessProvider:
        del system
        self.tools = {str(tool["name"]) for tool in tools}
        if state is not None:
            self.history = list(state.get("messages", []))
        return self

    def snapshot(self) -> Mapping[str, object]:
        return {"messages": self.history}

    def _call(self, name: str, **arguments: Any) -> ModelTurn:
        return ModelTurn(
            text="",
            tool_calls=(
                ToolCallRequest(
                    call_id=f"{self.plan['label']}-{self.phase}-{name}",
                    name=name,
                    arguments=arguments,
                ),
            ),
        )

    def _record(self) -> None:
        root = Path(os.environ["C14_FIXTURE_SIGNALS"])
        path = root / (self.plan["label"] + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"results": self.results, "tools": sorted(self.tools)})
        )
        temporary.replace(path)

    def send_user(self, text: str) -> ModelTurn:
        self.plan, _ = json.JSONDecoder().raw_decode(text.split("C14 ", 1)[1])
        self.history.append({"role": "user", "text": text})
        self.phase, self.results = 0, []
        flow = self.plan["flow"]
        if flow in {"read", "guarded_read", "public_read"}:
            return self._call("read_file", path=self.plan.get("path", "source.txt"))
        if flow == "output":
            return self._call("process_output", process_id=self.plan["process_id"])
        if flow == "command":
            return self._call("run_command", argv=self.plan["argv"], timeout=5)
        if flow == "server_roundtrip" and self.plan.get("edit"):
            return self._call("read_file", path="response.py")
        return self._call("start_process", argv=self.plan["argv"], cwd=".")

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        self.results.extend(
            {
                "call_id": item.call_id,
                "content": item.content,
                "is_error": item.is_error,
            }
            for item in results
        )
        self.history.append({"role": "tool", "results": self.results[-len(results) :]})
        self._record()
        self.phase += 1
        flow = self.plan["flow"]
        if flow == "guarded_read":
            if self.phase == 1:
                assert results[0].is_error, (
                    "private runtime copied into source was readable"
                )
                return self._call(
                    "search_text", query="PRIVATE_EXPORT", path="generated.py"
                )
            if self.phase == 2:
                return self._call(
                    "finish_task",
                    answer="private generated source remains withheld",
                    summary="done",
                )
        if flow != "start_refused":
            assert all(not item.is_error for item in results), results
        if flow == "copy_runtime":
            if self.phase == 1:
                self.process_id = str(_json_prefix(results[0].content)["process_id"])
                return self._call("process_output", process_id=self.process_id)
            if "process_output" in results[0].call_id:
                if "c14-source-copy-ready" not in results[0].content:
                    assert self.phase < 100, "managed runtime copier never became ready"
                    time.sleep(0.02)
                    return self._call("process_output", process_id=self.process_id)
                return self._call("stop_process", process_id=self.process_id)
        offset = 2 if flow == "server_roundtrip" and self.plan.get("edit") else 0
        if offset and self.phase == 1:
            return self._call(
                "write_file", path="response.py", content='VALUE = "c14-http-ok"\n'
            )
        if offset and self.phase == 2:
            return self._call("start_process", argv=self.plan["argv"], cwd=".")
        phase = self.phase - offset
        if phase == 1 and flow in {"start", "server_roundtrip"}:
            self.process_id = str(_json_prefix(results[0].content)["process_id"])
        if flow == "server_roundtrip":
            if phase == 1:
                return self._call(
                    "run_command", argv=[_PYTHON, "-u", "client.py"], timeout=5
                )
            if phase == 2:
                assert "c14-http-ok" in results[0].content, results
                return self._call("process_output", process_id=self.process_id)
            if phase == 3:
                assert "c14-http-served" in results[0].content, results
                if self.plan.get("pause_before_stop"):
                    signals = Path(os.environ["C14_FIXTURE_SIGNALS"])
                    (signals / (self.plan["label"] + ".ready")).touch()
                    _wait(
                        lambda: (signals / (self.plan["label"] + ".release")).exists(),
                        timeout=60,
                    )
                return self._call("stop_process", process_id=self.process_id)
        return self._call(
            "finish_task", answer="managed process fixture complete", summary="done"
        )

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        self.results.extend(
            {
                "call_id": item.call_id,
                "content": item.content,
                "is_error": item.is_error,
            }
            for item in results
        )
        self._record()


def _daemon_entry(authority_file: str) -> None:
    spec = importlib.util.spec_from_file_location(
        "c14_authority_helpers", authority_file
    )
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.install_test_authority()
    if os.environ.get("C14_UNVERIFIABLE_PID"):
        from llm_cli.execution import processes

        original_identity = processes.process_start_identity
        unverified_pid = int(os.environ["C14_UNVERIFIABLE_PID"])

        def unavailable_identity(pid: int) -> str:
            if pid == unverified_pid:
                raise OSError("managed fixture identity cannot be read")
            return original_identity(pid)

        processes.process_start_identity = unavailable_identity
    from llm_cli.daemon import main

    original = main.DaemonService
    if os.environ.get("C14_PAUSE_PREPARE"):
        from llm_cli.execution.commands import CommandRunner

        original_prepare = CommandRunner.prepare_process

        def paused_prepare(self: Any, *args: Any, **kwargs: Any) -> Any:
            signals = Path(os.environ["C14_FIXTURE_SIGNALS"])
            (signals / "prepare-ready").touch()
            _wait(lambda: (signals / "prepare-release").exists(), timeout=60)
            return original_prepare(self, *args, **kwargs)

        CommandRunner.prepare_process = paused_prepare

    class Service(original):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            arguments = list(args)
            arguments[1] = replace(
                arguments[1],
                agent_commands="allow",
                agent_command_environment="session",
                agent_explore=False,
                agent_review="off",
            )
            super().__init__(*arguments, **kwargs)
            self.providers.register(
                "c14-fixture", lambda model: _ProcessProvider(model)
            )
            if os.environ.get("C14_GRACE_SECONDS"):
                self.managed_processes.grace_seconds = float(
                    os.environ["C14_GRACE_SECONDS"]
                )

    main.DaemonService = Service
    main.main(["--profile", "test"])


class _Daemon:
    def __init__(
        self,
        root: Path,
        repository: Path,
        runtime: str,
        options: dict[str, str] | None = None,
    ) -> None:
        self.root, self.repository = root, repository
        self.signals = root / "signals"
        self.signals.mkdir()
        self.env = {
            **os.environ,
            "LLM_COORD_CONFIG_HOME": str(root / "config"),
            "LLM_COORD_DATA_HOME": str(root / "data"),
            "LLM_COORD_STATE_HOME": str(root / "state"),
            "LLM_COORD_RUNTIME_DIR": str(Path(runtime).resolve()),
            "C14_FIXTURE_SIGNALS": str(self.signals),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        self.env.update(options or {})
        self.paths = AppPaths.resolve("test", environ=self.env)
        self.client = DaemonClient(self.paths)
        self.children: list[subprocess.Popen[Any]] = []
        self.logs: list[Any] = []
        self.owned_groups: set[tuple[int, str, int]] = set()
        self.daemon: subprocess.Popen[Any] | None = None
        self.spawn()

    def spawn(self) -> subprocess.Popen[Any]:
        name = f"daemon-{len(self.children)}"
        log = (self.root / (name + ".log")).open("w")
        self.logs.append(log)
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-u",
                "-c",
                _BOOTSTRAP,
                str(Path(__file__).resolve()),
                str(Path(__file__).with_name("authority_helpers.py").resolve()),
            ],
            cwd=self.repository,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.children.append(process)
        self.daemon = process

        def ready() -> bool:
            assert process.poll() is None, (self.root / (name + ".log")).read_text()
            try:
                return bool(self.rpc("system.ping"))
            except LlmCoordError:
                return False

        _wait(ready)
        return process

    def rpc(self, method: str, **params: Any) -> Any:
        return self.client.call(method, params, autostart=False)

    def open(
        self,
        *,
        workspace: str = "isolated",
        approved: list[str] | None = None,
        environment: dict[str, str] | None = None,
        session_id: str = "managed-owner",
    ) -> dict[str, Any]:
        self.rpc("repo.add", path=str(self.repository))
        self.credentials = {
            "session_id": session_id,
            "resume_secret": "c14-local-resume-fixture-" + session_id,
        }
        result = self.rpc(
            "session.open",
            path=str(self.repository),
            session_id=self.credentials["session_id"],
            resume_token_hash=hashlib.sha256(
                self.credentials["resume_secret"].encode()
            ).hexdigest(),
            provider="c14-fixture",
            model="fixture",
            workspace=workspace,
            approved_runtime_files=approved or [],
            command_environment=environment or {"C14_PROJECT_MODE": "opening-session"},
        )
        self.rpc(
            "session.ack", **self.credentials, sequence=result["bootstrap_sequence"]
        )
        secret_file = self.paths.session_secret_file(self.credentials["session_id"])
        secret_file.write_text(self.credentials["resume_secret"])
        secret_file.chmod(0o600)
        return result

    def task(
        self,
        label: str,
        flow: str,
        *,
        expected_states: tuple[str, ...] = ("completed",),
        **plan: Any,
    ) -> dict[str, Any]:
        request = {"label": label, "flow": flow, **plan}
        accepted = self.rpc(
            "task.run",
            **self.credentials,
            path=str(self.repository),
            task_id=label,
            title="C14 " + json.dumps(request),
            scopes=["*"],
        )
        assert accepted["execution"] == "scheduled"
        result = _wait(
            lambda: (
                value
                if (value := self.rpc("task.show", task_id=label))["state"] in _SETTLED
                else None
            )
        )
        evidence_file = self.signals / (label + ".json")
        evidence = (
            json.loads(evidence_file.read_text())
            if evidence_file.exists()
            else {"results": []}
        )
        for item in evidence["results"]:
            if "start_process" in item["call_id"] and not item["is_error"]:
                self._remember(_json_prefix(item["content"]))
        assert result["state"] in expected_states, self.rpc(
            "task.events", task_id=label
        )
        return evidence

    def _remember(self, record: dict[str, Any]) -> None:
        pid, identity, pgid = (
            record.get(key) for key in ("pid", "start_identity", "pgid")
        )
        if isinstance(pid, int) and isinstance(identity, str) and isinstance(pgid, int):
            self.owned_groups.add((pid, identity, pgid))

    def records(self) -> list[dict[str, Any]]:
        value = self.rpc("workspace.process.list", **self.credentials)
        rows = value["processes"] if isinstance(value, dict) else value
        for row in rows:
            self._remember(row)
        return rows

    def cli(self, *arguments: str) -> dict[str, Any]:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("authority_helpers.py").resolve()),
                "--test-cli",
                "--profile",
                "test",
                "--json",
                *arguments,
            ],
            cwd=self.repository,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        return json.loads(result.stdout)

    def close(self) -> None:
        if (
            hasattr(self, "credentials")
            and self.daemon is not None
            and self.daemon.poll() is None
        ):
            with suppress(LlmCoordError):
                self.rpc("session.close", **self.credentials)
        if self.paths.control_db.exists() and hasattr(self, "credentials"):
            with (
                suppress(sqlite3.Error),
                closing(sqlite3.connect(self.paths.control_db)) as con,
            ):
                con.row_factory = sqlite3.Row
                for row in con.execute(
                    "SELECT pid,start_identity,pgid FROM managed_processes "
                    "WHERE session_id=?",
                    (self.credentials["session_id"],),
                ):
                    self._remember(dict(row))
        for process in reversed(self.children):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        # Failing tests must not leave their own managed groups behind. This
        # fallback follows lifecycle assertions and never signals a mismatch.
        for pid, identity, pgid in self.owned_groups:
            if (
                pid > 1
                and pgid == pid
                and pgid != os.getpgrp()
                and _identity(pid) == identity
            ):
                with suppress(ProcessLookupError):
                    if os.getpgid(pid) == pgid:
                        os.killpg(pgid, signal.SIGKILL)
        for log in self.logs:
            log.close()


@contextmanager
def _daemon(tmp_path: Path, repository: Path, **options: str) -> Iterator[_Daemon]:
    if available_sandbox() is None:
        pytest.skip("requires a working OS command sandbox")
    with TemporaryDirectory(prefix="lc-c14-", dir="/tmp") as runtime:
        fixture = _Daemon(tmp_path, repository, runtime, options)
        try:
            yield fixture
        finally:
            fixture.close()


def test_agent_http_server_output_and_stop_use_real_sandbox_and_cli(
    tmp_path: Path,
    repository_factory: Any,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    root = repository_factory(
        tmp_path,
        {
            "server.py": _HTTP_SERVER,
            "client.py": _HTTP_CLIENT,
            "response.py": 'VALUE = "c14-http-old"\n',
        },
    )
    with _daemon(tmp_path, root) as daemon:
        daemon.open()
        daemon.rpc(
            "checks.configure",
            path=str(root),
            config={
                "checks": {
                    "proposal-response": {
                        "argv": [
                            _PYTHON,
                            "-c",
                            "from response import VALUE; "
                            "assert VALUE == 'c14-http-ok'; "
                            "print('c14-proposal-check-passed')",
                        ],
                        "required": True,
                        "timeout": 5,
                    }
                }
            },
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(
                daemon.task,
                "http-server",
                "server_roundtrip",
                argv=[_PYTHON, "-u", "server.py"],
                edit=True,
                pause_before_stop=True,
            )

            def reached_server_barrier() -> bool:
                if running.done():
                    running.result()
                    raise AssertionError(
                        "HTTP task ended before its running-server barrier"
                    )
                return (daemon.signals / "http-server.ready").exists()

            try:
                _wait(reached_server_barrier)
                listed = daemon.cli(
                    "workspace",
                    "process",
                    "list",
                    "--session",
                    daemon.credentials["session_id"],
                )
                values = listed["processes"] if isinstance(listed, dict) else listed
                assert len(values) == 1 and values[0]["state"] == "running"
                record = values[0]
                assert record["pid"] and record["start_identity"] and record["pgid"]
            finally:
                (daemon.signals / "http-server.release").touch()
            evidence = running.result(timeout=30)
        offered = set(evidence["tools"])
        assert {
            "start_process",
            "process_output",
            "stop_process",
            "run_command",
        } <= offered
        output = "\n".join(row["content"] for row in evidence["results"])
        assert "c14-http-ok" in output and "c14-http-served" in output
        with closing(sqlite3.connect(daemon.paths.control_db)) as con:
            assert con.execute(
                "SELECT state FROM managed_processes WHERE process_id=?",
                (record["process_id"],),
            ).fetchone()[0] in {"stopped", "exited"}
        assert _identity(record["pid"]) is None
        checks = daemon.rpc("task.diff", task_id="http-server")["checks"]
        assert checks and all(
            check["state"] == "passed" and check["name"] == "proposal-response"
            for check in checks
        ), checks
        assert (root / "response.py").read_text() == 'VALUE = "c14-http-ok"\n'


def test_shared_background_process_is_refused_before_spawn(
    tmp_path: Path,
    repository_factory: Any,
) -> None:
    root = repository_factory(tmp_path, {"wait.py": "import time\ntime.sleep(60)\n"})
    with _daemon(tmp_path, root) as daemon:
        daemon.open(workspace="shared")
        evidence = daemon.task(
            "shared-refusal", "start_refused", argv=[_PYTHON, "wait.py"]
        )
        start = evidence["results"][0]
        assert start["is_error"] and "isolated" in start["content"]
        assert not daemon.records()


def _only_process(daemon: _Daemon) -> dict[str, Any]:
    records = daemon.records()
    assert len(records) == 1, records
    assert "--profile test" in records[0]["stop_command"]
    return records[0]


def _stored_process(daemon: _Daemon) -> dict[str, Any]:
    with closing(sqlite3.connect(daemon.paths.control_db)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT *,screened_output AS output FROM managed_processes "
            "WHERE session_id=?",
            (daemon.credentials["session_id"],),
        ).fetchall()
    assert len(rows) == 1
    return dict(rows[0])


def _started_process(evidence: dict[str, Any]) -> dict[str, Any]:
    row = next(row for row in evidence["results"] if "start_process" in row["call_id"])
    assert not row["is_error"], row
    return _json_prefix(row["content"])


def _heartbeat_stops(path: Path) -> None:
    before = path.stat().st_size
    time.sleep(0.15)
    assert path.stat().st_size == before, "forked writer survived process-group stop"


def test_cli_stop_kills_term_ignoring_process_group_without_blocking_other_rpcs(
    tmp_path: Path,
    repository_factory: Any,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    root = repository_factory(
        tmp_path, {"writer.py": _WRITER, ".gitignore": "build/\n"}
    )
    with _daemon(tmp_path, root, C14_GRACE_SECONDS="2") as daemon:
        opened = daemon.open()
        private = Path(opened["workspace"]["canonical_path"])
        daemon.task("writer-start", "start", argv=[_PYTHON, "-u", "writer.py"])
        record = _only_process(daemon)
        heartbeat = private / "build" / "heartbeat"
        _wait(lambda: heartbeat.exists() and heartbeat.stat().st_size > 30)
        assert record["pid"] and record["start_identity"] and record["pgid"]
        assert record["pid"] == record["pgid"]
        with ThreadPoolExecutor(max_workers=1) as executor:
            stop_arguments = shlex.split(record["stop_command"])
            assert stop_arguments[0] == "loupe"
            stopping = executor.submit(daemon.cli, *stop_arguments[1:])

            # Wait for the durable stop transition rather than an arbitrary
            # delay, then prove the unrelated daemon barrier is available.
            def entered_stop() -> bool:
                with closing(sqlite3.connect(daemon.paths.control_db)) as con, con:
                    return (
                        con.execute(
                            "SELECT state FROM managed_processes WHERE process_id=?",
                            (record["process_id"],),
                        ).fetchone()[0]
                        == "stopping"
                    )

            _wait(entered_stop)
            started = time.monotonic()
            assert daemon.rpc("repo.list")
            assert time.monotonic() - started < 1.0
            assert not stopping.done(), "stop finished before the unrelated RPC"
            result = stopping.result(timeout=10)
        assert result["state"] in {"stopped", "exited"}
        _heartbeat_stops(heartbeat)
        assert _identity(record["pid"]) is None


def test_session_close_stops_the_group_before_private_worktree_cleanup(
    tmp_path: Path,
    repository_factory: Any,
) -> None:
    root = repository_factory(
        tmp_path, {"writer.py": _WRITER, ".gitignore": "build/\n"}
    )
    with _daemon(tmp_path, root) as daemon:
        opened = daemon.open()
        private = Path(opened["workspace"]["canonical_path"])
        daemon.task("close-start", "start", argv=[_PYTHON, "-u", "writer.py"])
        record = _only_process(daemon)
        _wait(lambda: (private / "build" / "heartbeat").exists())
        result = daemon.rpc("session.close", **daemon.credentials)
        assert result["session"]["state"] == "closed"
        _wait(lambda: _identity(record["pid"]) is None)
        _wait(lambda: not private.exists())
        with closing(sqlite3.connect(daemon.paths.control_db)) as con, con:
            assert con.execute(
                "SELECT state FROM managed_processes WHERE process_id=?",
                (record["process_id"],),
            ).fetchone()[0] in {"stopped", "exited"}


def test_daemon_death_restart_stops_matching_managed_group(
    tmp_path: Path,
    repository_factory: Any,
) -> None:
    root = repository_factory(
        tmp_path, {"writer.py": _WRITER, ".gitignore": "build/\n"}
    )
    with _daemon(tmp_path, root) as daemon:
        opened = daemon.open()
        private = Path(opened["workspace"]["canonical_path"])
        daemon.task("restart-start", "start", argv=[_PYTHON, "-u", "writer.py"])
        record = _only_process(daemon)
        heartbeat = private / "build" / "heartbeat"
        _wait(lambda: heartbeat.exists() and heartbeat.stat().st_size > 30)
        assert daemon.daemon is not None
        daemon.daemon.kill()
        daemon.daemon.wait(timeout=10)
        daemon.spawn()
        _wait(lambda: _identity(record["pid"]) is None)
        _heartbeat_stops(heartbeat)
        with closing(sqlite3.connect(daemon.paths.control_db)) as con, con:
            assert con.execute(
                "SELECT state FROM managed_processes WHERE process_id=?",
                (record["process_id"],),
            ).fetchone()[0] in {"stopped", "exited"}


@pytest.mark.parametrize("identity_case", ["reused", "unverifiable"])
def test_restart_does_not_signal_a_reused_pid_or_unverifiable_identity(
    tmp_path: Path,
    repository_factory: Any,
    identity_case: str,
) -> None:
    root = repository_factory(
        tmp_path,
        {"wait.py": "import time\nprint('ready', flush=True)\ntime.sleep(120)\n"},
    )
    with _daemon(tmp_path, root) as daemon:
        daemon.open()
        daemon.task("reuse-start", "start", argv=[_PYTHON, "-u", "wait.py"])
        original = _only_process(daemon)
        assert daemon.daemon is not None
        daemon.daemon.kill()
        daemon.daemon.wait(timeout=10)
        # This unrelated, test-owned group stands in for a numeric PID reuse.
        # Its actual identity is deliberately different from the durable one.
        victim = subprocess.Popen(
            [_PYTHON, "-c", "import time; time.sleep(120)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            actual_identity = _wait(lambda: _identity(victim.pid))
            stored_identity = (
                "different-process-start-fixture"
                if identity_case == "reused"
                else actual_identity
            )
            if identity_case == "unverifiable":
                daemon.env["C14_UNVERIFIABLE_PID"] = str(victim.pid)
            with closing(sqlite3.connect(daemon.paths.control_db)) as con, con:
                con.execute(
                    "UPDATE managed_processes SET pid=?,pgid=?,start_identity=?,"
                    "state='running' "
                    "WHERE process_id=?",
                    (
                        victim.pid,
                        victim.pid,
                        stored_identity,
                        original["process_id"],
                    ),
                )
            daemon.spawn()
            with closing(sqlite3.connect(daemon.paths.control_db)) as con, con:
                state = con.execute(
                    "SELECT state FROM managed_processes WHERE process_id=?",
                    (original["process_id"],),
                ).fetchone()[0]
            assert state == "attention_required"
            assert victim.poll() is None, "recovery signalled an unrelated reused PID"
            listed = _only_process(daemon)
            assert listed["process_id"] == original["process_id"]
            assert listed["stop_command"]
            assert original["process_id"] in listed["stop_command"]
            try:
                refused = daemon.rpc(
                    "workspace.process.stop",
                    **daemon.credentials,
                    process_id=original["process_id"],
                )
                assert refused["state"] == "attention_required"
            except LlmCoordError as error:
                assert original["process_id"] in str(error)
            assert victim.poll() is None, "stop signalled an unrelated reused PID"
        finally:
            victim.terminate()
            victim.wait(timeout=5)
            if (
                _identity(original["pid"]) == original["start_identity"]
                and os.getpgid(original["pid"]) == original["pgid"]
            ):
                os.killpg(original["pgid"], signal.SIGKILL)


def test_output_flood_is_bounded_and_private_stdout_never_reaches_durable_sinks(
    tmp_path: Path,
    repository_factory: Any,
) -> None:
    canary = "c14-ordinary-private-output-marker-8c2da1f7"
    environment_canary = "c14-session-private-output-marker-7b83ad61"
    assert not contains_secret_material(canary)
    assert not contains_secret_material(environment_canary)
    program = """\
import os, sys, time
from pathlib import Path
value = Path('.env').read_text().strip().split('=', 1)[1]
environment_value = os.environ['C14_PROCESS_PASSWORD']
print(value, flush=True)
print(environment_value, flush=True)
sys.stdout.write('f' * (2 * 1024 * 1024) + '\\nc14-flood-tail\\n')
sys.stdout.flush()
sys.stdout.write(value[:6])
sys.stdout.flush()
time.sleep(.05)
print(value[6:], flush=True)
print(environment_value, flush=True)
time.sleep(120)
"""
    root = repository_factory(tmp_path, {"flood.py": program, ".gitignore": ".env\n"})
    (root / ".env").write_text("MARKER=" + canary + "\n")
    with _daemon(tmp_path, root) as daemon:
        daemon.open(
            approved=[".env"],
            environment={
                "C14_PROJECT_MODE": "opening-session",
                "C14_PROCESS_PASSWORD": environment_canary,
            },
        )
        daemon.task("flood-start", "start", argv=[_PYTHON, "-u", "flood.py"])
        record = _wait(
            lambda: (
                value
                if (value := _stored_process(daemon))["truncated"]
                and "c14-flood-tail" in value["output"]
                and (canary in value["output"] or "[private value]" in value["output"])
                else None
            )
        )
        assert len(record["output"].encode()) <= 64 * 1024
        for secret in (canary, environment_canary):
            assert secret not in json.dumps(record)
        assert "[private value]" in record["output"]
        evidence = daemon.task(
            "flood-output", "output", process_id=record["process_id"]
        )
        for secret in (canary, environment_canary):
            assert secret not in json.dumps(evidence)
        output = _json_prefix(evidence["results"][0]["content"])
        assert output["ring_limit_bytes"] == 64 * 1024
        assert output["ring_bytes"] <= output["ring_limit_bytes"]
        assert len(output["output"].encode()) <= 8 * 1024
        assert output["truncated"]
        daemon.rpc(
            "workspace.process.stop",
            **daemon.credentials,
            process_id=record["process_id"],
        )
        with closing(sqlite3.connect(daemon.paths.control_db)) as con, con:
            dumped = "\n".join(con.iterdump())
            for secret in (canary, environment_canary):
                assert secret not in dumped
            assert (
                con.execute(
                    "SELECT length(screened_output) FROM managed_processes "
                    "WHERE process_id=?",
                    (record["process_id"],),
                ).fetchone()[0]
                <= 64 * 1024
            )
        for path in daemon.paths.data_dir.rglob("*"):
            if path.is_file() and not path.is_relative_to(
                daemon.paths.data_dir / "isolated-worktrees"
            ):
                for secret in (canary, environment_canary):
                    assert secret.encode() not in path.read_bytes(), path
        for path in [
            *daemon.root.glob("*.log"),
            *daemon.paths.state_dir.rglob("*.jsonl"),
        ]:
            for secret in (canary, environment_canary):
                assert secret.encode() not in path.read_bytes(), path


def test_live_process_defers_sync_refresh_cleanup_and_switch_names_stop_command(
    tmp_path: Path,
    repository_factory: Any,
) -> None:
    root = repository_factory(
        tmp_path,
        {
            "wait.py": "import time\nprint('ready', flush=True)\ntime.sleep(120)\n",
            "source.txt": "opening source\n",
            ".gitignore": ".env\n",
        },
    )
    (root / ".env").write_text("MARKER=opening-private-value\n")
    with _daemon(tmp_path, root) as daemon:
        opened = daemon.open(approved=[".env"])
        private = Path(opened["workspace"]["canonical_path"])
        daemon.task("defer-start", "start", argv=[_PYTHON, "-u", "wait.py"])
        record = _only_process(daemon)
        (root / "source.txt").write_text("incoming source\n")
        (root / ".env").write_text("MARKER=incoming-private-value\n")
        evidence = daemon.task("defer-read", "read")
        assert (private / "source.txt").read_text() == "opening source\n"
        assert (private / ".env").read_text() == "MARKER=opening-private-value\n"
        assert private.exists()
        notices = json.dumps(evidence)
        assert record["process_id"] in notices
        assert record["stop_command"] in notices
        with pytest.raises(LlmCoordError) as refused:
            daemon.rpc(
                "workspace.switch_mode",
                **daemon.credentials,
                switch_intent_id="blocked-by-process",
                workspace="shared",
                disposition="discard",
            )
        assert record["process_id"] in str(refused.value)
        assert record["stop_command"] in str(refused.value)
        daemon.rpc(
            "workspace.process.stop",
            **daemon.credentials,
            process_id=record["process_id"],
        )
        daemon.task("adopt-after-stop", "read")
        assert (private / "source.txt").read_text() == "incoming source\n"
        assert (private / ".env").read_text() == "MARKER=incoming-private-value\n"


def test_other_session_cannot_read_live_workspace_private_ipc_files(
    tmp_path: Path,
    repository_factory: Any,
) -> None:
    program = """\
import os, time
from pathlib import Path
root = Path(os.environ['LOUPE_PROCESS_SOCKET_DIR'])
(root / 'private.txt').write_text('c14-innocuous-ipc-sentinel')
print('IPC_PATH=' + str(root), flush=True)
time.sleep(120)
"""
    probe = """\
from pathlib import Path
import sys
try:
    Path(sys.argv[1]).read_bytes()
except OSError:
    print('c14-ipc-denied')
else:
    print('c14-unexpected-ipc-read')
"""
    root = repository_factory(tmp_path, {"ipc_writer.py": program})
    with _daemon(tmp_path, root) as daemon:
        daemon.open(session_id="ipc-first")
        first_credentials = dict(daemon.credentials)
        daemon.task("ipc-first-start", "start", argv=[_PYTHON, "-u", "ipc_writer.py"])
        first = _only_process(daemon)
        row = _wait(
            lambda: (
                value
                if "IPC_PATH=" in (value := _stored_process(daemon))["output"]
                else None
            )
        )
        directory = Path(row["output"].split("IPC_PATH=", 1)[1].splitlines()[0])
        target = directory / "private.txt"
        assert target.read_text() == "c14-innocuous-ipc-sentinel"
        daemon.open(session_id="ipc-second")
        evidence = daemon.task(
            "ipc-second-read", "command", argv=[_PYTHON, "-c", probe, str(target)]
        )
        text = json.dumps(evidence)
        assert "c14-ipc-denied" in text
        assert "c14-unexpected-ipc-read" not in text
        assert _identity(first["pid"]) == first["start_identity"]
        daemon.rpc(
            "workspace.process.stop",
            **first_credentials,
            process_id=first["process_id"],
        )
        daemon.rpc("session.close", **first_credentials)
        daemon.rpc("session.close", **daemon.credentials)


def test_slow_managed_start_keeps_daemon_and_publication_barriers_available(
    tmp_path: Path,
    repository_factory: Any,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    root = repository_factory(
        tmp_path,
        {
            "wait.py": "import time\nprint('ready', flush=True)\ntime.sleep(120)\n",
            "source.txt": "peer source\n",
        },
    )
    with _daemon(tmp_path, root, C14_PAUSE_PREPARE="1") as daemon:
        daemon.open(session_id="starting-owner")
        owner = dict(daemon.credentials)
        daemon.open(session_id="unrelated-reader", workspace="shared")
        peer = dict(daemon.credentials)
        daemon.credentials = owner
        with ThreadPoolExecutor(max_workers=1) as executor:
            starting = executor.submit(
                daemon.task, "slow-start", "start", argv=[_PYTHON, "-u", "wait.py"]
            )
            try:
                _wait(lambda: (daemon.signals / "prepare-ready").exists())
                started = time.monotonic()
                assert daemon.rpc("repo.list")
                read = daemon.rpc(
                    "workspace.read_file", **peer, relative_path="source.txt"
                )
                assert read["content"] == "peer source\n"
                assert time.monotonic() - started < 1.0
                assert not starting.done(), "start skipped its controlled barrier"
            finally:
                (daemon.signals / "prepare-release").touch()
            starting.result(timeout=30)
        record = _only_process(daemon)
        daemon.rpc("workspace.process.stop", **owner, process_id=record["process_id"])
        daemon.rpc("session.close", **peer)
        daemon.rpc("session.close", **owner)


def test_managed_and_snapshot_commands_cannot_read_unapproved_primary_env(
    tmp_path: Path,
    repository_factory: Any,
) -> None:
    probe = """\
from pathlib import Path
import sys, time
try:
    Path(sys.argv[1]).read_bytes()
except OSError:
    print('c14-primary-env-denied', flush=True)
else:
    print('c14-unexpected-primary-env-read', flush=True)
if '--keepalive' in sys.argv:
    time.sleep(120)
"""
    canary = "c14-unapproved-primary-marker-5f3c9a12"
    assert not contains_secret_material(canary)
    root = repository_factory(
        tmp_path, {"primary_probe.py": probe, ".gitignore": ".env\n"}
    )
    (root / ".env").write_text("MARKER=" + canary + "\n")
    with _daemon(tmp_path, root) as daemon:
        opened = daemon.open()
        assert not (Path(opened["workspace"]["canonical_path"]) / ".env").exists()
        daemon.task(
            "managed-primary-probe",
            "start",
            argv=[_PYTHON, "-u", "primary_probe.py", str(root / ".env"), "--keepalive"],
        )
        row = _wait(
            lambda: (
                value
                if "c14-primary-env-denied"
                in (value := _stored_process(daemon))["output"]
                or "c14-unexpected-primary-env-read" in value["output"]
                else None
            )
        )
        assert "c14-primary-env-denied" in row["output"]
        assert "c14-unexpected-primary-env-read" not in row["output"]
        evidence = daemon.task(
            "snapshot-primary-probe",
            "command",
            argv=[_PYTHON, "-u", "primary_probe.py", str(root / ".env")],
        )
        text = json.dumps(evidence)
        assert "c14-primary-env-denied" in text
        assert "c14-unexpected-primary-env-read" not in text
        assert canary not in text
        daemon.rpc(
            "workspace.process.stop", **daemon.credentials, process_id=row["process_id"]
        )


@pytest.mark.parametrize(
    ("private_value", "runtime_disposition"),
    [
        ("c14-runtime-source-opaque-marker-6b3f90d1", "delete"),
        ("zQ7!m2", "overwrite"),
        ("c14-runtime-source-rotated-marker-84e9ac20", "overwrite"),
    ],
    ids=[
        "opaque-value-deleted-env",
        "short-value-overwritten-env",
        "opaque-value-overwritten-env",
    ],
)
def test_managed_private_runtime_cannot_be_read_or_published_as_source_after_restart(
    tmp_path: Path,
    repository_factory: Any,
    private_value: str,
    runtime_disposition: str,
) -> None:
    assert not contains_secret_material(private_value)
    program = """\
from pathlib import Path
import json, sys, time
value = Path('.env').read_text().partition('=')[2].strip()
Path('generated.py').write_text('PRIVATE_EXPORT = ' + repr(value) + '\\n')
Path('runtime-cache.json').write_text(json.dumps({'public': value}))
if sys.argv[1] == 'delete':
    Path('.env').unlink()
else:
    Path('.env').write_text('MARKER=replaced-runtime-input\\n')
print('c14-source-copy-ready', flush=True)
time.sleep(120)
"""
    root = repository_factory(
        tmp_path,
        {
            "copy_runtime.py": program,
            ".gitignore": ".env\nruntime-cache.json\n",
        },
    )
    (root / ".env").write_text("MARKER=" + private_value + "\n")
    (root / "runtime-cache.json").write_text('{"public":"initial"}')
    with _daemon(tmp_path, root) as daemon:
        opened = daemon.open(approved=[".env"])
        private = Path(opened["workspace"]["canonical_path"])
        absent = identify_path(private / "generated.py")
        assert absent.absent
        absent_base = asdict(absent)
        daemon.task(
            "copy-private-runtime",
            "copy_runtime",
            argv=[_PYTHON, "-u", "copy_runtime.py", runtime_disposition],
            expected_states=("completed", "failed"),
        )
        assert private_value in (private / "generated.py").read_text()
        if runtime_disposition == "delete":
            assert not (private / ".env").exists()
        else:
            assert private_value not in (private / ".env").read_text()
        assert not (root / "generated.py").exists()

        def assert_rpc_source_is_withheld() -> None:
            for read_only in (False, True):
                with pytest.raises(
                    LlmCoordError, match=r"requires a shared workspace|private runtime"
                ):
                    daemon.rpc(
                        "workspace.read_file",
                        **daemon.credentials,
                        relative_path="generated.py",
                        read_only=read_only,
                    )
            with pytest.raises(LlmCoordError, match="private runtime"):
                daemon.rpc(
                    "workspace.stage_file",
                    **daemon.credentials,
                    candidate_id="must-not-store-runtime-value",
                    relative_path="generated.py",
                    base=absent_base,
                    content=(private / "generated.py").read_text(),
                )

        assert_rpc_source_is_withheld()
        evidence = daemon.task(
            "guarded-source-read",
            "guarded_read",
            path="generated.py",
            expected_states=("completed", "failed"),
        )
        assert evidence["results"][0]["is_error"]
        assert private_value not in json.dumps(evidence)
        search = next(
            row for row in evidence["results"] if "search_text" in row["call_id"]
        )
        assert "PRIVATE_EXPORT =" not in search["content"]

        def assert_retained_output_is_screened(label: str) -> None:
            if runtime_disposition == "delete":
                return
            output = daemon.task(
                label,
                "command",
                argv=[
                    _PYTHON,
                    "-c",
                    "from pathlib import Path; "
                    "print(Path('runtime-cache.json').read_text())",
                ],
            )
            content = json.dumps(output)
            assert private_value not in content
            assert "[private value]" in content

        assert_retained_output_is_screened("rotated-private-output")
        assert daemon.daemon is not None
        daemon.daemon.kill()
        daemon.daemon.wait(timeout=10)
        daemon.spawn()
        daemon.rpc(
            "session.resume",
            **daemon.credentials,
            command_environment={"C14_PROJECT_MODE": "opening-session"},
        )
        assert private_value in (private / "generated.py").read_text()
        if runtime_disposition == "delete":
            assert not (private / ".env").exists()
        else:
            assert private_value not in (private / ".env").read_text()
        assert_rpc_source_is_withheld()
        restarted = daemon.task(
            "guarded-source-after-restart",
            "guarded_read",
            path="generated.py",
            expected_states=("completed", "failed"),
        )
        assert restarted["results"][0]["is_error"]
        assert private_value not in json.dumps(restarted)
        assert_retained_output_is_screened("rotated-private-output-after-restart")
        assert not (root / "generated.py").exists()
        with closing(sqlite3.connect(daemon.paths.control_db)) as con:
            assert private_value not in "\n".join(con.iterdump())
        for path in daemon.paths.data_dir.rglob("*"):
            if path.is_file() and not path.is_relative_to(
                daemon.paths.data_dir / "isolated-worktrees"
            ):
                assert private_value.encode() not in path.read_bytes(), path
        for path in [
            *daemon.root.glob("*.log"),
            *daemon.paths.state_dir.rglob("*.jsonl"),
        ]:
            assert private_value.encode() not in path.read_bytes(), path


def test_private_runtime_value_already_in_opening_public_source_remains_readable(
    tmp_path: Path,
    repository_factory: Any,
) -> None:
    public_value = "development"
    root = repository_factory(
        tmp_path,
        {
            "public.py": 'MODE = "development"\n',
            "copy_runtime.py": (
                "import time\nprint('ready', flush=True)\ntime.sleep(120)\n"
            ),
            ".gitignore": ".env\n",
        },
    )
    (root / ".env").write_text("MARKER=" + public_value + "\n")
    with _daemon(tmp_path, root) as daemon:
        daemon.open(approved=[".env"])
        evidence = daemon.task(
            "public-value-start", "start", argv=[_PYTHON, "-u", "copy_runtime.py"]
        )
        process = _started_process(evidence)
        daemon.rpc(
            "workspace.process.stop",
            **daemon.credentials,
            process_id=process["process_id"],
        )
        read = daemon.task("public-value-read", "public_read", path="public.py")
        assert public_value in read["results"][0]["content"]
        assert not read["results"][0]["is_error"]
