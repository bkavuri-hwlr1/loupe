"""Verified profile snapshots through the running daemon and public CLI."""

from __future__ import annotations

import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from llm_cli.cli.app import build_parser, dispatch
from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient


def test_live_daemon_backup_keeps_latest_task_and_private_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository_factory: Any,
) -> None:
    repository = repository_factory(tmp_path, {"README.md": "base\n"})
    with tempfile.TemporaryDirectory(prefix="lc-backup-", dir="/tmp") as runtime:
        for name, path in {
            "LLM_COORD_CONFIG_HOME": tmp_path / "config",
            "LLM_COORD_DATA_HOME": tmp_path / "data",
            "LLM_COORD_STATE_HOME": tmp_path / "state",
            "LLM_COORD_RUNTIME_DIR": Path(runtime).resolve(),
        }.items():
            monkeypatch.setenv(name, str(path))
        client = DaemonClient(AppPaths.resolve("backup-test"), timeout_seconds=30)
        try:
            client.call("repo.add", {"path": str(repository)})
            task = client.call(
                "task.run",
                {
                    "path": str(repository),
                    "title": "latest backup task",
                    "scopes": ["README.md"],
                    "claim_only": True,
                },
            )["task"]
            arguments = build_parser().parse_args(
                [
                    "db",
                    "backup",
                    "--out",
                    str(tmp_path / "backups"),
                ]
            )
            result = dispatch(arguments, client)
            directory = Path(result["directory"])
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            assert len(result["databases"]) == 1
            assert result["omitted"] == ["knowledge.sqlite3", "vectors.sqlite3"]
            assert not client.paths.knowledge_db.exists()
            assert not client.paths.vectors_db.exists()
            for database in result["databases"]:
                path = Path(database["path"])
                assert path.parent == directory
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
                assert database["integrity_check"] == "ok"
                with closing(sqlite3.connect(path)) as connection:
                    assert connection.execute("PRAGMA integrity_check").fetchone() == (
                        "ok",
                    )
                    if path.name == "control.sqlite3":
                        assert connection.execute(
                            "SELECT title FROM tasks WHERE task_id = ?",
                            (task["task_id"],),
                        ).fetchone() == ("latest backup task",)
            second = client.call("db.backup")
            assert Path(second["directory"]).parent == client.paths.data_dir / "backups"
            assert second["directory"] != result["directory"]
            # A chosen directory may pass through a symlink, as /tmp does on
            # macOS; the backup lands in the directory it points to.
            linked = tmp_path / "linked"
            linked.symlink_to(tmp_path / "backups", target_is_directory=True)
            third = client.call("db.backup", {"out": str(linked / "child")})
            assert Path(third["directory"]).parent == tmp_path / "backups" / "child"
            assert stat.S_IMODE(Path(third["directory"]).stat().st_mode) == 0o700
            blocked = tmp_path / "not-a-directory"
            blocked.write_text("")
            with pytest.raises(LlmCoordError, match=r"could not complete: .*exists"):
                client.call("db.backup", {"out": str(blocked)})
            assert client.call("system.ping", autostart=False)
        finally:
            client.call("system.shutdown", autostart=False)
            assert client.wait_until_stopped(15)
