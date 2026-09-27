"""Local environment and database health probes."""

from __future__ import annotations

import shutil
import sqlite3
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from llm_cli.paths import AppPaths


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    warning: bool = False


def run_doctor(paths: AppPaths) -> list[dict[str, object]]:
    checks = [
        _python_check(),
        _git_check(),
        _sqlite_check(),
        _fts5_check(),
        _directory_check(paths.data_dir),
        _directory_check(paths.state_dir),
        _directory_check(paths.runtime_dir),
    ]
    for database in (paths.control_db, paths.knowledge_db, paths.vectors_db):
        if database.exists():
            checks.extend((_file_mode_check(database), _integrity_check(database)))
    return [asdict(check) for check in checks]


def _python_check() -> Check:
    supported = (3, 12) <= sys.version_info[:2] < (3, 15)
    return Check(
        "python",
        supported,
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )


def _git_check() -> Check:
    executable = shutil.which("git")
    if not executable:
        return Check("git", False, "git is not installed")
    result = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    return Check("git", result.returncode == 0, result.stdout.strip()[:200])


def _sqlite_check() -> Check:
    return Check(
        "sqlite", sqlite3.sqlite_version_info >= (3, 35), sqlite3.sqlite_version
    )


def _fts5_check() -> Check:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
    except sqlite3.Error as exc:
        return Check("sqlite_fts5", False, str(exc)[:200])
    finally:
        connection.close()
    return Check("sqlite_fts5", True, "available")


def _directory_check(path: Path) -> Check:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return Check(f"directory:{path.name}", False, str(exc)[:200])
    return Check(
        f"directory:{path.name}",
        path.is_dir() and mode & 0o077 == 0,
        f"mode={mode:04o}",
    )


def _file_mode_check(path: Path) -> Check:
    mode = stat.S_IMODE(path.stat().st_mode)
    return Check(f"permissions:{path.name}", mode & 0o077 == 0, f"mode={mode:04o}")


def _integrity_check(path: Path) -> Check:
    try:
        uri = f"{path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
        detail = str(result[0]) if result else "no result"
        return Check(f"integrity:{path.name}", detail == "ok", detail[:200])
    except sqlite3.Error as exc:
        return Check(f"integrity:{path.name}", False, str(exc)[:200])


__all__ = ["Check", "run_doctor"]
