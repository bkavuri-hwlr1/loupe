"""Deterministic bounded line-window chunks with stable content identities."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

VERSION = "lines-v1"
MAX_TEXT_BYTES = 8_192


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: str
    ordinal: int
    start_line: int
    end_line: int
    text: str


def chunks(source_id: str, path: str, identity: str, text: str) -> tuple[Chunk, ...]:
    result: list[Chunk] = []
    lines = text.splitlines(keepends=True)
    start = 0
    while start < len(lines):
        end = min(len(lines), start + 60)
        piece = ""
        size = 0
        first_line = start + 1
        last_line = first_line
        for offset, line in enumerate(lines[start:end], start + 1):
            for char in line:
                encoded = len(char.encode("utf-8"))
                if size + encoded > MAX_TEXT_BYTES:
                    _append(
                        result, source_id, path, identity, first_line, last_line, piece
                    )
                    piece, size, first_line = "", 0, offset
                if not piece:
                    first_line = offset
                piece += char
                size += encoded
                last_line = offset
        if piece:
            _append(result, source_id, path, identity, first_line, last_line, piece)
        if end == len(lines):
            break
        start = max(start + 1, end - 10)
    return tuple(result)


def _append(
    result: list[Chunk],
    source: str,
    path: str,
    identity: str,
    start: int,
    end: int,
    text: str,
) -> None:
    ordinal = len(result)
    key = "\0".join((source, path, identity, VERSION, str(ordinal)))
    result.append(
        Chunk(hashlib.sha256(key.encode()).hexdigest(), ordinal, start, end, text)
    )
