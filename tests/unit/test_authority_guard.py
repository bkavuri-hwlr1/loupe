"""The suite guard detects authority writes without changing the watched tree."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from authority_guard import AuthorityRootGuard


def test_guard_does_not_create_missing_root(tmp_path: Path) -> None:
    root = tmp_path / "account" / "physical-authority" / "v1"
    guard = AuthorityRootGuard.capture(root)
    guard.assert_unchanged()
    assert not root.exists()
    assert not root.parent.exists()


def test_guard_detects_new_lock_only_record(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    root.mkdir()
    guard = AuthorityRootGuard.capture(root)
    record = root / "checkouts" / "orphan"
    record.mkdir(parents=True)
    (record / "ownership.lock").touch()
    with pytest.raises(AssertionError, match=r"ownership\.lock"):
        guard.assert_unchanged()
    assert (record / "ownership.lock").is_file()


@pytest.mark.parametrize("change", ["content", "mode", "remove"])
def test_guard_detects_changes_to_existing_records(
    tmp_path: Path, change: str
) -> None:
    marker = tmp_path / "authority" / "checkouts" / "one" / "marker.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("before")
    marker.chmod(0o644)
    previous = marker.stat()
    guard = AuthorityRootGuard.capture(marker.parents[2])
    if change == "content":
        marker.write_text("after!")
        os.utime(marker, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    elif change == "mode":
        marker.chmod(0o600)
    else:
        marker.unlink()
    with pytest.raises(AssertionError, match=r"marker\.json"):
        guard.assert_unchanged()


def test_guard_does_not_follow_links(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("private outside content")
    (root / "link").symlink_to(outside)
    guard = AuthorityRootGuard.capture(root)
    outside.write_text("changed outside content")
    guard.assert_unchanged()
