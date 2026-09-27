"""Owner-only Unix socket RPC server."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from llm_cli import PROTOCOL_VERSION
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.protocol.envelopes import EnvelopeError, Request, Response
from llm_cli.protocol.framing import FrameError, read_frame, write_frame

Handler = Callable[[Request], Awaitable[Any]]

# ``sun_path`` is a fixed-size field in ``struct sockaddr_un`` and is not
# reported by Python.  BSD-derived kernels give it 104 bytes; Linux gives 108.
_SUN_PATH_BYTES = 104 if sys.platform.startswith(("darwin", "freebsd")) else 108


def _assert_bindable(socket_path: Path) -> None:
    """Reject an unbindable socket path with an actionable message.

    A path that exceeds ``sun_path`` fails ``bind`` with a bare ``OSError``,
    which surfaces to the operator only as a daemon that never became ready.
    Deep runtime directories and long temporary directories reach this limit in
    ordinary use, so the diagnosis belongs here rather than in a stack trace.
    """

    encoded = os.fsencode(str(socket_path))
    if len(encoded) + 1 > _SUN_PATH_BYTES:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            f"daemon socket path is {len(encoded)} bytes, but this platform "
            f"allows at most {_SUN_PATH_BYTES - 1}; set LLM_COORD_RUNTIME_DIR "
            f"to a shorter directory",
            {"socket_path": str(socket_path), "limit_bytes": _SUN_PATH_BYTES - 1},
        )


class RpcServer:
    def __init__(self, socket_path: Path, profile_id: str, handler: Handler) -> None:
        self.socket_path = socket_path
        self.profile_id = profile_id
        self.handler = handler
        self._server: asyncio.AbstractServer | None = None
        self._revision = 0

    async def start(self) -> None:
        _assert_bindable(self.socket_path)
        if self.socket_path.exists():
            self.socket_path.unlink()
        old_umask = os.umask(0o077)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_connection, path=self.socket_path
            )
        finally:
            os.umask(old_umask)
        self.socket_path.chmod(0o600)

    async def serve(self, shutdown: asyncio.Event) -> None:
        if self._server is None:
            raise RuntimeError("RPC server has not started")
        async with self._server:
            await shutdown.wait()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request_id = "unknown"
        try:
            raw = await read_frame(reader)
            request = Request.from_dict(raw)
            request_id = request.request_id
            if request.protocol_version != PROTOCOL_VERSION:
                raise LlmCoordError(
                    ErrorCode.PROTOCOL_MISMATCH,
                    f"protocol {request.protocol_version} is not supported",
                    {"supported": PROTOCOL_VERSION},
                )
            if request.profile_id != self.profile_id:
                raise LlmCoordError(
                    ErrorCode.PROTOCOL_MISMATCH,
                    "request profile does not match the daemon profile",
                )
            result = await self.handler(request)
            if isinstance(result, AsyncIterator):
                result = await self._pump(request_id, result, writer)
            self._revision += 1
            response = Response(
                request_id=request_id,
                ok=True,
                result=result,
                daemon_revision=self._revision,
            )
        except LlmCoordError as exc:
            response = Response(
                request_id=request_id,
                ok=False,
                error={
                    "code": exc.code.value,
                    "message": exc.message,
                    "details": exc.details,
                },
                daemon_revision=self._revision,
            )
        except (EnvelopeError, FrameError, asyncio.IncompleteReadError) as exc:
            response = Response(
                request_id=request_id,
                ok=False,
                error={
                    "code": ErrorCode.PROTOCOL_MISMATCH.value,
                    "message": str(exc),
                },
                daemon_revision=self._revision,
            )
        except Exception:
            response = Response(
                request_id=request_id,
                ok=False,
                error={
                    "code": ErrorCode.INTERNAL_RECOVERABLE.value,
                    "message": "the daemon could not complete the request",
                },
                daemon_revision=self._revision,
            )
        try:
            await write_frame(writer, response.to_dict())
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _pump(
        self,
        request_id: str,
        events: AsyncIterator[dict[str, Any]],
        writer: asyncio.StreamWriter,
    ) -> dict[str, Any]:
        """Write one frame per event, then report where the stream stopped.

        Stream frames reuse the same length-prefixed framing as every other
        frame on this socket rather than the NDJSON the design sketched.  One
        framing per connection cannot be misparsed as the other, and the reader
        that already exists handles both directions.

        A client that hangs up mid-stream is an ordinary outcome -- an operator
        pressing Ctrl-C -- so it ends the stream instead of raising.
        """

        last_sequence = 0
        delivered = 0
        try:
            async for event in events:
                sequence = event.get("sequence")
                if isinstance(sequence, int):
                    last_sequence = sequence
                delivered += 1
                await write_frame(
                    writer,
                    {"request_id": request_id, "stream": True, "event": event},
                )
        except (ConnectionError, BrokenPipeError):
            return {"final": True, "detached": True, "last_sequence": last_sequence}
        finally:
            with contextlib.suppress(Exception):
                await events.aclose()  # type: ignore[attr-defined]
        return {
            "final": True,
            "detached": False,
            "delivered": delivered,
            "last_sequence": last_sequence,
        }


__all__ = ["Handler", "RpcServer"]
