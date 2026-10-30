"""Owner-bound private value receipts, separate from source artifacts and SQL."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from llm_cli.agent.source_policy import (
    block_source_private_values,
    contains_private_source,
    has_private_source_values,
    register_source_private_values,
)
from llm_cli.config.models import MAX_HYDRATION_ENTRIES
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.workspace.broker import (
    atomic_delete_file,
    load_candidate_content,
    read_candidate_path,
)
from llm_cli.workspace.hydration import _evidence

if TYPE_CHECKING:
    from llm_cli.workspace.isolated import IsolatedState

_DIRECTORY = "loupe-process-private"
_REQUIRED = "loupe-process-values-required"
_RECEIPT = "values.json"
_LIMIT = 32 * 1024 * 1024
_LOCK = threading.Lock()


def _error() -> LlmCoordError:
    return LlmCoordError(
        ErrorCode.REPOSITORY_UNSAFE,
        "private process value receipt is unavailable or changed; "
        "private source was preserved",
    )


def _binding(root: Path, workspace_id: str, token: str) -> dict[str, object]:
    status = root.lstat()
    if not stat.S_ISDIR(status.st_mode):
        raise _error()
    return {
        "version": 1,
        "workspace_id": workspace_id,
        "token": token,
        "root_identity": [status.st_dev, status.st_ino],
    }


def _read(path: Path) -> dict[str, object]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != os.geteuid()
                or status.st_mode & 0o077
                or status.st_nlink != 1
                or status.st_size > _LIMIT
            ):
                raise _error()
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(_LIMIT + 1)
            if (
                len(raw) > _LIMIT
                or _evidence(os.fstat(descriptor)) != _evidence(status)
                or _evidence(path.lstat()) != _evidence(status)
            ):
                raise _error()
            receipt = json.loads(raw)
            if not isinstance(receipt, dict):
                raise _error()
            return receipt
        finally:
            os.close(descriptor)
    except (OSError, ValueError) as exc:
        raise _error() from exc


def _directory(admin: Path) -> Path:
    directory = admin / _DIRECTORY
    status = directory.lstat()
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_mode & 0o077
    ):
        raise _error()
    return directory


def _load(
    root: Path, admin: Path, binding: dict[str, object]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    required = admin / _REQUIRED
    if not required.exists() and not required.is_symlink():
        if (admin / _DIRECTORY).exists() or (admin / _DIRECTORY).is_symlink():
            raise _error()
        return (), ()
    marker = _read(required)
    retired = marker == {**binding, "retired": True}
    if marker != binding and not retired:
        raise _error()
    if (
        retired
        and not (admin / _DIRECTORY).exists()
        and not (admin / _DIRECTORY).is_symlink()
    ):
        return (), ()
    receipt = _read(_directory(admin) / _RECEIPT)
    if any(receipt.get(key) != value for key, value in binding.items()):
        raise _error()
    values = receipt.get("values")
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value for value in values
    ):
        raise _error()
    public = receipt.get("public_values")
    if not isinstance(public, list) or any(
        not isinstance(value, str) or value not in values for value in public
    ):
        raise _error()
    return tuple(values), tuple(public)


def load_values(
    root: Path, admin: Path, workspace_id: str, token: str
) -> tuple[str, ...]:
    """Load once before source capture/read; a missing required receipt refuses."""
    try:
        values, public = _load(root, admin, _binding(root, workspace_id, token))
    except (LlmCoordError, OSError, ValueError):
        block_source_private_values(root)
        raise _error() from None
    register_source_private_values(root, values, public_values=public)
    return values


def _atomic(path: Path, body: bytes) -> None:
    if len(body) > _LIMIT:
        raise _error()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".private-values-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def remember_values(
    root: Path,
    admin: Path,
    state: IsolatedState,
    candidate_dir: Path,
    new_values: tuple[str, ...],
) -> None:
    """Union durable history; only accepted opening source can prove publicity."""
    binding = _binding(root, state.workspace_id, state.creation_token)
    with _LOCK:
        old_values, old_public = _load(root, admin, binding)
        retained, public = set(old_values), set(old_public)
        candidates = {value for value in new_values if value} - retained
        # Match decoded bytes exactly like source/output guards, including short
        # private values. Current or process-written files never grant publicity.
        for entry in state.source_manifest.values():
            if not candidates:
                break
            digest = entry.get("content_hash")
            if entry.get("screened") or not isinstance(digest, str):
                continue
            body = load_candidate_content(candidate_dir, digest).decode(
                "utf-8", errors="replace"
            )
            found = {value for value in candidates if value in body}
            public.update(found)
            candidates.difference_update(found)
        merged = tuple(sorted(retained | set(new_values) - {""}, key=len, reverse=True))
        required = admin / _REQUIRED
        if not required.exists():
            # Fail closed if interrupted after this durable marker but before
            # the initial receipt. No program has received its GO yet.
            _atomic(required, json.dumps(binding).encode())
            directory = admin / _DIRECTORY
            directory.mkdir(mode=0o700)
            parent = os.open(admin, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        directory = _directory(admin)
        _atomic(
            directory / _RECEIPT,
            json.dumps(
                {**binding, "values": merged, "public_values": sorted(public)}
            ).encode(),
        )
        register_source_private_values(root, merged, public_values=tuple(public))


def scrub_source_copies(
    root: Path, *, guard_root: Path | None = None, max_paths: int = 20_000
) -> set[str]:
    """Remove private-value leaves without following links or retaining bytes."""
    if type(max_paths) is not int or not 1 <= max_paths <= MAX_HYDRATION_ENTRIES:
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            "isolated.hydration_max_entries must be between 1 and 100000",
        )
    protected = root if guard_root is None else guard_root
    if not has_private_source_values(protected) or not root.exists():
        return set()
    removed: set[str] = set()
    count, total = 0, 0
    deadline = time.monotonic() + 5
    for directory, directories, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in list(directories):
            if name in {".git", ".loupe", ".llm-coord", ".codex", ".agents"}:
                directories.remove(name)
            elif (base / name).is_symlink():
                directories.remove(name)
                files.append(name)
        for name in files:
            if name == ".git":
                continue
            count += 1
            if count > max_paths:
                raise LlmCoordError(
                    ErrorCode.REPOSITORY_UNSAFE,
                    "private source cleanup exceeds "
                    f"isolated.hydration_max_entries={max_paths}; "
                    "increase that setting "
                    "within its 100000-entry ceiling or reduce private entries",
                )
            if time.monotonic() > deadline:
                raise _error()
            relative = (base / name).relative_to(root).as_posix()
            identity, body = read_candidate_path(
                base / name, max_bytes=256 * 1024 * 1024
            )
            total += len(body or b"")
            if total > 1024**3:
                raise _error()
            if body is not None and contains_private_source(protected, body):
                atomic_delete_file(root, relative, identity)
                removed.add(relative)
    return removed


def retire_values(root: Path, admin: Path, workspace_id: str, token: str) -> None:
    """Durably record completed scrubbing before deleting the private seed."""
    from llm_cli.workspace.isolated import IsolatedWorkspaceManager

    binding = _binding(root, workspace_id, token)
    marker_path = admin / _REQUIRED
    if not marker_path.exists() and not marker_path.is_symlink():
        if (admin / _DIRECTORY).exists() or (admin / _DIRECTORY).is_symlink():
            raise _error()
        return
    marker = _read(marker_path)
    if marker not in (binding, {**binding, "retired": True}):
        raise _error()
    _atomic(marker_path, json.dumps({**binding, "retired": True}).encode())
    descriptor = os.open(admin, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        IsolatedWorkspaceManager._remove_private_entry(descriptor, _DIRECTORY)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
