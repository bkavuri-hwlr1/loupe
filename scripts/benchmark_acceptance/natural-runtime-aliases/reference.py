from __future__ import annotations

import threading
import unicodedata
from collections.abc import Iterable
from pathlib import Path

_runtime_lock = threading.RLock()
_runtime_roots = {}

def register_runtime_paths(root: Path, paths: tuple[str, ...]) -> None:
    """Pin hydrated paths independently of mutable repository ignore rules."""
    with _runtime_lock:
        _runtime_roots[root.resolve()] = paths


def runtime_path_matches(relative: str, paths: Iterable[str]) -> bool:
    """Keep spelling aliases private on every supported filesystem.

    Conservatively reserve case and Unicode-normalization aliases even on a
    case-sensitive volume, without probing or depending on mutable Git config.
    """
    candidate = unicodedata.normalize("NFD", relative).casefold()
    for path in paths:
        protected = unicodedata.normalize("NFD", path).casefold()
        if candidate == protected or candidate.startswith(protected + "/"):
            return True
    return False


def runtime_path(root: Path, relative: str) -> bool:
    with _runtime_lock:
        paths = _runtime_roots.get(root.resolve(), ())
    return runtime_path_matches(relative, paths)
