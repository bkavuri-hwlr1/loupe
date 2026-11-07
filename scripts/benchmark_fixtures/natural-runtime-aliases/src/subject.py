from __future__ import annotations

import threading
from pathlib import Path

_runtime_lock = threading.RLock()
_runtime_roots = {}

def register_runtime_paths(root: Path, paths: tuple[str, ...]) -> None:
    """Pin hydrated paths independently of mutable repository ignore rules."""
    with _runtime_lock:
        _runtime_roots[root.resolve()] = paths


def runtime_path(root: Path, relative: str) -> bool:
    with _runtime_lock:
        paths = _runtime_roots.get(root.resolve(), ())
    return any(relative == path or relative.startswith(path + "/") for path in paths)
