"""Identify the exact Loupe source code a process is running."""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent


@functools.cache
def code_identity() -> dict[str, str]:
    """Fingerprint this package's Python sources, once per process.

    The daemon computes this at startup, so it describes the code it loaded.
    A client compares it with its own to notice a daemon that is still running
    older code, or code from another installation sharing the same profile.
    """

    digest = hashlib.sha256()
    for path in sorted(_PACKAGE.rglob("*.py")):
        digest.update(path.relative_to(_PACKAGE).as_posix().encode() + b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"\0unreadable")
        digest.update(b"\0")
    return {"fingerprint": digest.hexdigest(), "path": str(_PACKAGE)}


__all__ = ["code_identity"]
