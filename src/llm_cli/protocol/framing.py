"""Bounded four-byte length-prefixed JSON framing for local RPC."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Mapping
from typing import Any

DEFAULT_MAX_FRAME_BYTES = 4 * 1024 * 1024
_HEADER = struct.Struct(">I")


class FrameError(ValueError):
    pass


def encode_frame(
    payload: Mapping[str, Any], *, max_bytes: int = DEFAULT_MAX_FRAME_BYTES
) -> bytes:
    body = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if not body or len(body) > max_bytes:
        raise FrameError(f"frame body must be between 1 and {max_bytes} bytes")
    return _HEADER.pack(len(body)) + body


def decode_frame(
    data: bytes, *, max_bytes: int = DEFAULT_MAX_FRAME_BYTES
) -> dict[str, Any]:
    if len(data) < _HEADER.size:
        raise FrameError("frame is missing its length header")
    (length,) = _HEADER.unpack(data[: _HEADER.size])
    if length < 1 or length > max_bytes:
        raise FrameError("frame length is outside the configured bounds")
    body = data[_HEADER.size :]
    if len(body) != length:
        raise FrameError("frame body length does not match its header")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameError("frame body is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise FrameError("frame body must be a JSON object")
    return value


async def read_frame(
    reader: asyncio.StreamReader, *, max_bytes: int = DEFAULT_MAX_FRAME_BYTES
) -> dict[str, Any]:
    header = await reader.readexactly(_HEADER.size)
    (length,) = _HEADER.unpack(header)
    if length < 1 or length > max_bytes:
        raise FrameError("frame length is outside the configured bounds")
    body = await reader.readexactly(length)
    return decode_frame(header + body, max_bytes=max_bytes)


async def write_frame(
    writer: asyncio.StreamWriter,
    payload: Mapping[str, Any],
    *,
    max_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> None:
    writer.write(encode_frame(payload, max_bytes=max_bytes))
    await writer.drain()


__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "FrameError",
    "decode_frame",
    "encode_frame",
    "read_frame",
    "write_frame",
]
