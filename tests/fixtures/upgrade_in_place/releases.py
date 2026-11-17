"""Pinned source releases, oldest first. Keep the most recent two tags."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Release:
    tag: str
    commit: str
    version: str


RELEASES = (Release("v0.1.0a3", "4b9615160262c677d07f4136a40857dbb0942d1d", "0.1.0a3"),)


def get_release(tag: str | None) -> Release:
    if tag is None:
        return RELEASES[-1]
    for release in RELEASES:
        if tag == release.tag:
            return release
    raise ValueError(f"unknown release {tag!r}; expected {[r.tag for r in RELEASES]}")


def verified_commit(repository: Path, release: Release) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/tags/{release.tag}^{{commit}}"],
        cwd=repository,
        capture_output=True,
        text=True,
    )
    if result.returncode or result.stdout.strip() != release.commit:
        raise ValueError(f"tag {release.tag} must resolve to {release.commit}")
    return release.commit
