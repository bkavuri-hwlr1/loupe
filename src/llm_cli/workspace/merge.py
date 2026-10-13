"""Three-way text merges for pending edits whose files changed underneath them.

A shared task's edits are private until publication, and each records the
bytes it was based on. When another session publishes, or someone edits a
file outside Loupe, the edits are moved onto the file's current bytes the way
`git rebase` would: a three-way merge from the recorded base. Edits that touch
the same lines conflict, and the result carries diff3 conflict markers with
fixed labels, so a conflict the model has not resolved can be recognized in
the pending content itself.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git

OURS_LABEL = "your pending edits"
BASE_LABEL = "your starting version"
THEIRS_LABEL = "current checkout"
_MARKERS = (
    f"<<<<<<< {OURS_LABEL}",
    f"||||||| {BASE_LABEL}",
    f">>>>>>> {THEIRS_LABEL}",
)


@dataclass(frozen=True, slots=True)
class MergeResult:
    content: bytes
    conflicts: int


def merge_text(base: bytes, ours: bytes, theirs: bytes) -> MergeResult:
    """Apply the change from base to ours onto theirs.

    Every side must be UTF-8 text. ``conflicts`` counts the regions both
    sides changed, which appear in ``content`` between conflict markers.
    """

    for side in (base, ours, theirs):
        side.decode("utf-8")
    with tempfile.TemporaryDirectory(prefix="loupe-merge-") as directory:
        root = Path(directory)
        for name, content in (("ours", ours), ("base", base), ("theirs", theirs)):
            (root / name).write_bytes(content)
        result = run_git(
            root,
            [
                "merge-file",
                "-p",
                "--diff3",
                *("-L", OURS_LABEL, "-L", BASE_LABEL, "-L", THEIRS_LABEL),
                "ours",
                "base",
                "theirs",
            ],
            check=False,
            text=False,
        )
    # The exit status counts conflicts, up to 127; anything else is an error.
    if not 0 <= result.returncode <= 127:
        raise LlmCoordError(ErrorCode.INTERNAL_RECOVERABLE, "git merge-file failed")
    assert isinstance(result.stdout, bytes)
    return MergeResult(result.stdout, result.returncode)


def has_conflict_markers(content: bytes | None) -> bool:
    """Whether content still holds a conflict this module wrote."""

    if not content:
        return False
    lines = content.decode("utf-8", errors="replace").splitlines()
    return any(line in _MARKERS for line in lines)
