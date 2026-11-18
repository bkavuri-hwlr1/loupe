from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def test_upgrade_source_selection_and_tag_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(Path(__file__).parents[1] / "fixtures/upgrade_in_place")
    import releases

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git(
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@example.org",
        "commit",
        "--allow-empty",
        "-qm",
        "release",
    )
    commit = git("rev-parse", "HEAD")
    old = releases.Release("v1", commit, "1")
    new = releases.Release("v2", commit, "2")
    monkeypatch.setattr(releases, "RELEASES", (old, new))
    assert releases.get_release(None) == new
    assert releases.get_release("v1") == old
    with pytest.raises(ValueError, match="unknown release"):
        releases.get_release("v3")
    with pytest.raises(ValueError, match="must resolve"):
        releases.verified_commit(tmp_path, new)
    git("tag", "v2")
    assert releases.verified_commit(tmp_path, new) == commit
    with pytest.raises(ValueError, match="must resolve"):
        releases.verified_commit(tmp_path, releases.Release("v2", "0" * 40, "2"))
