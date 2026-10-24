"""Self-contained Git binary patches from already frozen source candidates."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
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
