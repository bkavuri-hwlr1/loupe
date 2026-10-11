from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol import client as client_module
from llm_cli.protocol.client import DaemonClient


def _client(directory: Path) -> DaemonClient:
    return DaemonClient(
        AppPaths(
            profile_id="test",
            config_dir=directory,
            data_dir=directory,
            state_dir=directory,
            runtime_dir=directory,
        )
    )


@pytest.mark.parametrize("autostart", [False, True])
@pytest.mark.parametrize(
    "interruption",
    [asyncio.IncompleteReadError(b"{", 20), TimeoutError("response timeout")],
)
def test_ambiguous_response_does_not_retry_a_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    autostart: bool,
    interruption: Exception,
) -> None:
    client = _client(tmp_path)
    once = AsyncMock(side_effect=interruption)
    start = Mock()
    monkeypatch.setattr(client, "_call_once", once)
    monkeypatch.setattr(client, "_start_and_wait", start)

    with pytest.raises(LlmCoordError) as failure:
        client.call("task.run", {"task_id": "possibly-accepted"}, autostart=autostart)

    assert failure.value.code is ErrorCode.DAEMON_UNAVAILABLE
    assert "outcome may be unknown" in failure.value.message
    assert once.await_count == 1
    start.assert_not_called()


@pytest.mark.parametrize(
    "interruption",
    [asyncio.IncompleteReadError(b"", 4), TimeoutError("response timeout")],
)
def test_ambiguous_response_after_startup_is_normalized_without_another_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: Exception,
) -> None:
    client = _client(tmp_path)
    once = AsyncMock(side_effect=[FileNotFoundError(), interruption])
    start = Mock()
    monkeypatch.setattr(client, "_call_once", once)
    monkeypatch.setattr(client, "_start_and_wait", start)

    with pytest.raises(LlmCoordError) as failure:
        client.call("task.run", {"task_id": "possibly-accepted"})

    assert failure.value.code is ErrorCode.DAEMON_UNAVAILABLE
    assert "outcome may be unknown" in failure.value.message
    assert once.await_count == 2
    start.assert_called_once_with()
    assert once.await_args_list[0].args[0] is once.await_args_list[1].args[0]


def test_ctrl_c_cancels_a_stream_read_between_loop_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl+C must not raise inside the event loop.

    Raised mid-step, it could leave the loop unable to finish the cancelled
    read, and detaching from a task hung. The interrupt instead cancels the
    read at its next await, then surfaces once the socket is closed.
    """

    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("this process does not receive Ctrl+C as KeyboardInterrupt")
    client = _client(tmp_path)
    settled = []
    writer = Mock(wait_closed=AsyncMock())
    writer.close.side_effect = lambda: settled.append("socket closed")
    monkeypatch.setattr(
        client, "_open_stream", AsyncMock(return_value=(object(), writer))
    )

    loops: list[asyncio.AbstractEventLoop] = []
    finished = threading.Event()

    def watchdog() -> None:
        # Fail instead of hanging if the interrupt strands the event loop.
        while not finished.wait(5):
            for loop in loops:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(loop.stop)

    async def read_forever(reader: object) -> None:
        loops.append(asyncio.get_running_loop())
        os.kill(os.getpid(), signal.SIGINT)
        # Python runs signal handlers between bytecodes, so an interrupt
        # raised here would cut this step short.
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            pass
        settled.append("step finished")
        try:
            await asyncio.Future()
        finally:
            settled.append("read cancelled")

    monkeypatch.setattr(client_module, "read_frame", read_forever)
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        with pytest.raises(KeyboardInterrupt):
            next(client.stream("task.attach", {"task_id": "running"}))
    finally:
        finished.set()
    assert settled == ["step finished", "read cancelled", "socket closed"]
