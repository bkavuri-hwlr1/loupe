from __future__ import annotations

import asyncio
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


def test_interrupt_cancels_stream_read_before_closing_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    settled = []
    writer = Mock(wait_closed=AsyncMock())
    writer.close.side_effect = lambda: settled.append("socket closed")
    monkeypatch.setattr(
        client, "_open_stream", AsyncMock(return_value=(object(), writer))
    )

    def interrupt() -> None:
        raise KeyboardInterrupt

    async def read_forever(reader: object) -> None:
        asyncio.get_running_loop().call_soon(interrupt)
        try:
            await asyncio.Future()
        finally:
            settled.append("read cancelled")

    monkeypatch.setattr(client_module, "read_frame", read_forever)
    with pytest.raises(KeyboardInterrupt):
        next(client.stream("task.attach", {"task_id": "running"}))
    assert settled == ["read cancelled", "socket closed"]
