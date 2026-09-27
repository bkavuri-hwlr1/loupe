"""Opaque, time-sortable identifiers without a Python 3.14 dependency."""

from __future__ import annotations

import secrets
import time


def new_id(prefix: str) -> str:
    """Return a compact opaque ID that sorts approximately by creation time."""

    if not prefix or not prefix.isascii() or not prefix.replace("_", "").isalnum():
        raise ValueError("ID prefix must be non-empty ASCII alphanumeric text")
    milliseconds = time.time_ns() // 1_000_000
    return f"{prefix}_{milliseconds:012x}{secrets.token_hex(10)}"


__all__ = ["new_id"]
