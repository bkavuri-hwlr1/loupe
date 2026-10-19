"""Session setup tolerates queued daemon work without retrying mutations."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from llm_cli.cli import app, session
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient
from llm_cli.protocol.envelopes import Request
from llm_cli.protocol.server import RpcServer


class _SessionServer:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.delays: dict[str, float] = {}
        self.calls: list[tuple[str, str | None]] = []
        self.sessions: dict[str, dict[str, Any]] = {}
        self.installers: list[dict[str, Any]] = []

    async def handle(self, request: Request) -> dict[str, Any]:
        self.calls.append((request.method, request.idempotency_key))
        params = request.params
        if request.method == "session.open":
            # Model the important ambiguous case: durable creation precedes a
            # delayed response. The client must never allocate a second session.
            self.sessions[params["session_id"]] = {
                "provider": params["provider"],
                "model": params["model"],
                "agent_mode": params["mode"],
                "workspace_mode": params.get("workspace") or "shared",
            }
        await asyncio.sleep(self.delays.get(request.method, 0))
        if request.method == "session.open":
            return {
                "session": self.sessions[params["session_id"]],
                "bootstrap_sequence": 1,
            }
        if request.method == "session.resume":
            return {
                "session": self.sessions[params["session_id"]],
                "cursor": {"transport_received_sequence": 1},
            }
        if request.method == "session.ack":
            return {"cursor": {"transport_received_sequence": params["sequence"]}}
        if request.method == "config.show":
            return {"settings": [{"key": "isolated.rebuild", "value": self.installers}]}
        if request.method == "probe.slow":
            return {"ok": True}
        raise AssertionError(request.method)


def _open(
    client: DaemonClient, *, resume: str | None = None, workspace: str | None = None
) -> Any:
    return session.open_or_resume_session(
        client,
        repository=client.paths.state_dir.parent,
        provider="scripted",
        model="scripted",
        resume_session_id=resume,
        workspace_mode=workspace,
    )


@pytest.fixture
def session_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_SessionServer]:
    monkeypatch.setattr(session, "SESSION_SETUP_TIMEOUT_SECONDS", 2.0, raising=False)
    # Keep the actual socket below macOS's 104-byte Unix socket pathname cap.
    with TemporaryDirectory(prefix="lss-", dir="/tmp") as runtime:
        paths = AppPaths(
            "test",
            tmp_path / "config",
            tmp_path / "data",
            tmp_path / "state",
            Path(runtime).resolve(),
        )
        paths.ensure()
        state = _SessionServer(paths)
        ready, stop = threading.Event(), threading.Event()
        failures: list[BaseException] = []

        async def serve() -> None:
            server = RpcServer(paths.socket, paths.profile_id, state.handle)
            try:
                await server.start()
                ready.set()
                while not stop.is_set():
                    await asyncio.sleep(0.005)
            except BaseException as exc:
                failures.append(exc)
                ready.set()
            finally:
                await server.close()

        thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
        thread.start()
        try:
            assert ready.wait(5), "scripted RPC server did not start"
            assert not failures, failures
            yield state
        finally:
            stop.set()
            thread.join(5)
            assert not thread.is_alive(), "scripted RPC server did not stop"
            assert not failures, failures


@pytest.mark.parametrize("delayed", ["session.open", "session.resume", "session.ack"])
def test_session_setup_waits_beyond_the_ordinary_rpc_deadline(
    session_server: _SessionServer, delayed: str
) -> None:
    state = session_server
    # Only these setup responses get the longer budget. Normal RPCs still use
    # this original client and fail promptly below.
    client = DaemonClient(state.paths, timeout_seconds=0.05)
    state.delays[delayed] = 0.15
    credentials = _open(client)
    if delayed == "session.resume":
        assert (
            _open(client, resume=credentials.session_id).session_id
            == credentials.session_id
        )
    assert credentials.cursor == 1
    assert len(state.sessions) == 1
    assert state.calls.count(("session.open", credentials.session_id)) == 1
    assert client.timeout_seconds == 0.05
    state.delays["probe.slow"] = 0.15
    with pytest.raises(LlmCoordError) as failure:
        client.call("probe.slow", autostart=False)
    assert failure.value.code is ErrorCode.DAEMON_UNAVAILABLE


def test_direct_cli_resume_uses_the_setup_deadline(
    session_server: _SessionServer,
) -> None:
    state = session_server
    client = DaemonClient(state.paths, timeout_seconds=0.05)
    credentials = _open(client)
    state.delays["session.resume"] = 0.15
    arguments = app.build_parser().parse_args(
        ["session", "resume", credentials.session_id]
    )
    result = app.dispatch(arguments, client)
    assert result["session"]["provider"] == "scripted"
    assert len(state.sessions) == 1
    assert client.timeout_seconds == 0.05


def test_setup_timeout_keeps_uncertainty_credentials_and_does_not_retry(
    session_server: _SessionServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = session_server
    # Exercise the actual bounded setup timeout without waiting two minutes.
    monkeypatch.setattr(session, "SESSION_SETUP_TIMEOUT_SECONDS", 0.05, raising=False)
    client = DaemonClient(state.paths, timeout_seconds=0.05)
    state.delays["session.open"] = 0.15
    with pytest.raises(LlmCoordError) as failure:
        _open(client)
    assert failure.value.code is ErrorCode.DAEMON_UNAVAILABLE
    assert (
        "session open outcome is unknown; retry with --resume " in failure.value.message
    )
    session_id = failure.value.message.rsplit(" ", 1)[1]
    assert session.session_resume_secret(state.paths, session_id)
    assert list(state.sessions) == [session_id]
    assert state.calls == [("session.open", session_id)]


def test_setup_preserves_an_explicitly_longer_client_budget(
    session_server: _SessionServer,
) -> None:
    client = DaemonClient(session_server.paths, timeout_seconds=300.0)
    assert session._session_setup_client(client).timeout_seconds == 300.0
    assert client.timeout_seconds == 300.0


@pytest.mark.parametrize(
    "workspace,resume,active,local,original,expected,probe",
    [
        ("shared", False, None, False, 5.0, 120.0, None),
        ("isolated", False, [], False, 5.0, 120.0, 120.0),
        (None, True, [], False, 5.0, 120.0, 120.0),
        ("isolated", False, [60, 90], False, 5.0, 270.0, 120.0),
        (None, True, [60], False, 5.0, 180.0, 120.0),
        ("isolated", False, [], True, 5.0, 120.0, 180.0),
        ("isolated", False, None, True, 5.0, 180.0, 180.0),
        ("isolated", False, [90], True, 5.0, 210.0, 180.0),
        ("isolated", False, [60], False, 600.0, 600.0, 600.0),
    ],
)
def test_runtime_budget_composes_setup_and_effective_installer_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: str | None,
    resume: bool,
    active: list[int] | None,
    local: bool,
    original: float,
    expected: float,
    probe: float | None,
) -> None:
    paths = AppPaths("test", tmp_path, tmp_path, tmp_path, tmp_path)
    if local:
        paths.config_file.write_text(
            '[isolated.rebuild]\n".venv" = ["local-installer"]\n'
        )
    probes: list[float] = []

    def show_effective(
        self: DaemonClient, method: str, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        assert method == "config.show"
        probes.append(self.timeout_seconds)
        return {
            "settings": []
            if active is None
            else [
                {
                    "key": "isolated.rebuild",
                    "value": [{"timeout_seconds": seconds} for seconds in active],
                }
            ]
        }

    monkeypatch.setattr(DaemonClient, "call", show_effective)
    client = DaemonClient(paths, timeout_seconds=original)
    runtime = session._runtime_open_client(client, workspace, resume=resume)
    assert runtime.timeout_seconds == expected
    assert probes == ([] if probe is None else [probe])
    assert client.timeout_seconds == original


def test_direct_cli_resume_keeps_the_active_installer_allowance(
    session_server: _SessionServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = session_server
    client = DaemonClient(state.paths, timeout_seconds=0.05)
    credentials = _open(client)
    monkeypatch.setattr(session, "SESSION_SETUP_TIMEOUT_SECONDS", 0.05)
    state.installers = [{"timeout_seconds": 1}]
    state.delays["session.resume"] = 0.15
    arguments = app.build_parser().parse_args(
        ["session", "resume", credentials.session_id]
    )
    result = app.dispatch(arguments, client)
    assert result["session"]["provider"] == "scripted"
    assert len(state.sessions) == 1
    assert client.timeout_seconds == 0.05


def test_bootstrap_ack_keeps_only_the_setup_allowance(
    session_server: _SessionServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = session_server
    state.installers = [{"timeout_seconds": 1}]
    client = DaemonClient(state.paths, timeout_seconds=0.05)
    original_call = DaemonClient.call
    budgets: dict[str, float] = {}

    def record_budget(
        self: DaemonClient, method: str, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        budgets[method] = self.timeout_seconds
        return original_call(self, method, *args, **kwargs)

    monkeypatch.setattr(DaemonClient, "call", record_budget)
    credentials = _open(client, workspace="isolated")
    assert credentials.workspace_mode == "isolated"
    assert budgets["session.open"] == 3.0
    assert budgets["session.ack"] == 2.0
    assert client.timeout_seconds == 0.05
