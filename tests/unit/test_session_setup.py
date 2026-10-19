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
                "workspace_mode": "shared",
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
        if request.method == "probe.slow":
            return {"ok": True}
        raise AssertionError(request.method)


def _open(client: DaemonClient, *, resume: str | None = None) -> Any:
    return session.open_or_resume_session(
        client,
        repository=client.paths.state_dir.parent,
        provider="scripted",
        model="scripted",
        resume_session_id=resume,
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
