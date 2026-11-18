"""Run an upgrade against a pinned release on this machine."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from releases import get_release, verified_commit

FIXTURE = Path(__file__).resolve().parent
PROJECT = FIXTURE.parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", nargs="?", help="source tag (default: latest listed)")
    args = parser.parse_args()
    try:
        release = get_release(args.tag)
        commit = verified_commit(PROJECT, release)
    except ValueError as error:
        parser.error(str(error))
    with tempfile.TemporaryDirectory(prefix="loupe-upgrade-") as temporary:
        base = Path(temporary)
        old = base / "old"
        profile = base / "profile"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(old), commit],
            cwd=PROJECT,
            check=True,
        )
        try:
            for checkout, venv, arguments in (
                (old, old / ".venv", ["build_profile.py", release.tag, old, profile]),
                (PROJECT, base / "current-venv", ["check_upgrade.py", profile]),
            ):
                script, *options = arguments
                subprocess.run(
                    [
                        "uv",
                        "run",
                        "--locked",
                        "--all-groups",
                        "--all-extras",
                        "--python",
                        sys.executable,
                        "python",
                        FIXTURE / script,
                        *options,
                    ],
                    cwd=checkout,
                    env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv)},
                    check=True,
                )
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(old)],
                cwd=PROJECT,
                check=True,
            )


if __name__ == "__main__":
    main()
