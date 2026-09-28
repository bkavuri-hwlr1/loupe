from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient
from llm_cli.protocol.framing import read_frame, write_frame


@pytest.mark.parametrize(
    "trailing_bytes", [b"", b"\x00\x00", struct.pack(">I", 128) + b"{"]
)
def test_disconnected_stream_preserves_delivered_output_and_reports_reconnect(
    trailing_bytes: bytes,
) -> None:
    async def scenario(directory: Path) -> None:
        paths = AppPaths(
            profile_id="test",
            config_dir=directory,
            data_dir=directory,
            state_dir=directory,
            runtime_dir=directory,
        )
        expected = {
            "sequence": 7,
            "event_type": "model.text.delta",
            "payload": {"text": "Already visible"},
        }

        async def disconnect(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                request = await read_frame(reader)
                await write_frame(
                    writer,
                    {
                        "request_id": request["request_id"],
                        "stream": True,
                        "event": expected,
                    },
                )
                # Simulate daemon termination at a frame boundary, halfway
                # through a header, or halfway through a declared frame body.
                writer.write(trailing_bytes)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_unix_server(disconnect, path=paths.socket)
        client = DaemonClient(paths)

        def consume() -> None:
            events = client.stream("task.attach", {"task_id": "test-task"})
            assert next(events) == expected
            with pytest.raises(LlmCoordError) as failure:
                next(events)
            assert failure.value.code is ErrorCode.DAEMON_UNAVAILABLE
            assert "attach again" in failure.value.message

        async with server:
            await asyncio.wait_for(asyncio.to_thread(consume), timeout=5)

    # macOS Unix socket names are capped at 104 bytes; pytest's nested paths
    # can exceed that independently of the behavior under test.
    with TemporaryDirectory(prefix="loupe-stream-", dir="/tmp") as directory:
        asyncio.run(scenario(Path(directory)))
