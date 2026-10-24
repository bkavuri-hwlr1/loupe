"""Self-contained Git binary patches from already frozen source candidates."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from llm_cli.coordination.scopes import normalize_changed_path
from llm_cli.git.environment import run_git
from llm_cli.workspace.batches import BatchFile
from llm_cli.workspace.identity import DIRECTORY_MODE, EXECUTABLE_MODE, SYMLINK_MODE


def portable_patch(files: Sequence[BatchFile]) -> bytes:
    """Produce a git-apply compatible patch without touching a user's index."""
    with tempfile.TemporaryDirectory(prefix="loupe-switch-patch-") as temporary:
        root = Path(temporary).resolve()
        for side in ("old", "new"):
            (root / side).mkdir(mode=0o700)
        for item in files:
            relative = normalize_changed_path(item.relative_path)
            if relative != item.relative_path or ".git" in Path(relative).parts:
                raise ValueError("patch path is not canonical ordinary source")
            for side, body, mode in (
                ("old", item.original, item.base.mode),
                ("new", item.content, item.mode),
            ):
                if body is None or mode == DIRECTORY_MODE:
                    continue
                target = root / side / item.relative_path
                for parent in target.parents:
                    if parent == root:
                        break
                    if parent.is_symlink():
                        raise ValueError("patch path crosses a symlink")
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if mode == SYMLINK_MODE:
                    target.symlink_to(os.fsdecode(body))
                else:
                    target.write_bytes(body)
                    target.chmod(0o700 if mode == EXECUTABLE_MODE else 0o600)
        result = run_git(
            root,
            [
                "diff",
                "--no-index",
                "--binary",
                "--no-prefix",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--",
                "old",
                "new",
            ],
            check=False,
            text=False,
            remaining=lambda: 30.0,
        )
        if result.returncode not in {0, 1} or not isinstance(result.stdout, bytes):
            raise ValueError("could not create the exact source patch")
        return result.stdout


def write_export_artifact(
    root: Path,
    name: str,
    body: bytes,
    fault: Callable[[str], None],
) -> Path:
    """Publish fsynced bytes without exposing partial finals or replacing files."""
    import stat

    from llm_cli.ids import new_id

    if Path(name).name != name or name in {".", ".."}:
        raise ValueError("export artifact name must be one safe basename")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = "." + new_id("switch_export") + ".tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    created = os.fstat(descriptor)

    def verify_existing() -> None:
        existing = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        try:
            status = os.fstat(existing)
            if not stat.S_ISREG(status.st_mode) or status.st_mode & 0o077:
                raise ValueError("switch export artifact identity changed")
            if status.st_size != len(body):
                raise ValueError("switch export artifact identity changed")
            chunks, total = [], 0
            while chunk := os.read(existing, min(1024 * 1024, len(body) + 1 - total)):
                chunks.append(chunk)
                total += len(chunk)
                if total > len(body):
                    raise ValueError("switch export artifact identity changed")
            if b"".join(chunks) != body:
                raise ValueError("switch export artifact identity changed")
        finally:
            os.close(existing)

    try:
        with os.fdopen(descriptor, "wb") as handle:
            split = max(1, len(body) // 2)
            handle.write(body[:split])
            handle.flush()
            fault("export_partial")
            handle.write(body[split:])
            handle.flush()
            os.fsync(handle.fileno())
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError:
                verify_existing()
            os.fsync(directory)
        return root / name
    finally:
        try:
            current = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                os.unlink(temporary, dir_fd=directory)
        finally:
            os.close(directory)
