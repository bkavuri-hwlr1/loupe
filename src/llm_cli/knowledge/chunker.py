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
        pieces: list[str] = []
        size = 0
        first_line = start + 1
        last_line = first_line
        for offset, line in enumerate(lines[start:end], start + 1):
            encoded = line.encode("utf-8")
            position = 0
            while position < len(encoded):
                available = MAX_TEXT_BYTES - size
                # Decode whole lines in the usual case. A long line needs only
                # one iteration per bounded chunk; an incomplete UTF-8 suffix
                # stays in the next piece rather than splitting a code point.
                fragment = encoded[position : position + available].decode(
                    "utf-8", errors="ignore"
                )
                used = len(fragment.encode("utf-8"))
                if used:
                    if not pieces:
                        first_line = offset
                    pieces.append(fragment)
                    size += used
                    position += used
                    last_line = offset
                if position < len(encoded):
                    _append(
                        result,
                        source_id,
                        path,
                        identity,
                        first_line,
                        last_line,
                        "".join(pieces),
                    )
                    pieces, size = [], 0
        if pieces:
            _append(
                result,
                source_id,
                path,
                identity,
                first_line,
                last_line,
                "".join(pieces),
            )
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
