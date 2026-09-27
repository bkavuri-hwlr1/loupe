"""Small structured logger that never accepts prompt/source payload fields."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from llm_cli.paths import make_private_file

_FORBIDDEN_KEYS = {
    "prompt",
    "source",
    "source_text",
    "diff",
    "patch",
    "token",
    "secret",
    "embedding",
    "provider_body",
}


class JsonLogger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, event: str, **fields: Any) -> None:
        safe: dict[str, Any] = {}
        for key, value in fields.items():
            if key.casefold() in _FORBIDDEN_KEYS:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe[key] = value
        record = {
            "timestamp_ms": time.time_ns() // 1_000_000,
            "event": event,
            **safe,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            make_private_file(self.path)
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


__all__ = ["JsonLogger"]
