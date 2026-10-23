"""An authentic 0.1.0a2 profile survives the 0.1.0a3 upgrade and startup."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest

from llm_cli import __version__
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient
from llm_cli.storage.migrations import CONTROL_MIGRATIONS

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "upgrade_0_1_0a2"


def _restore_profile(paths: AppPaths, root: Path) -> None:
    manifest = json.loads((FIXTURE / "manifest.json").read_text())
    original = FIXTURE / "control.sqlite3"
    assert (
        hashlib.sha256(original.read_bytes()).hexdigest() == manifest["database_sha256"]
    )
    paths.ensure()
    shutil.copyfile(original, paths.control_db)
    shutil.copytree(FIXTURE / "candidates", paths.candidate_dir, dirs_exist_ok=True)
    with closing(sqlite3.connect(paths.control_db)) as connection:
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone() == (19,)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name='checkout_regimes'"
        ).fetchone() == (0,)
        # Fixture paths are placeholders; all migration receipts and historical
        # IDs/content remain unchanged. The bundled Git baseline keeps refs valid.
        for (table,) in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        ):
            for column in list(connection.execute(f'PRAGMA table_info("{table}")')):
                if column[2] == "TEXT":
                    name = column[1]
                    connection.execute(
                        f'UPDATE "{table}" SET "{name}" = replace("{name}", ?, ?) '
                        f'WHERE instr("{name}", ?) > 0',
                        (
                            manifest["path_placeholder"],
                            str(root),
                            manifest["path_placeholder"],
                        ),
                    )
        connection.commit()


@pytest.mark.usefixtures("isolated_daemon_autostart")
def test_0_1_0a2_profile_upgrades_without_checkout_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_run: Callable[..., str]
) -> None:
    git_run(
        tmp_path, "clone", "--quiet", str(FIXTURE / "repository.bundle"), "repository"
    )
    repository = tmp_path / "repository"
    git_run(
        repository,
        "remote",
        "set-url",
        "origin",
        "https://example.invalid/loupe-upgrade.git",
    )
    (repository / "a.py").write_text("value = 2\n")
    with tempfile.TemporaryDirectory(prefix="lc-upgrade-", dir="/tmp") as runtime:
        for name, path in {
            "LLM_COORD_CONFIG_HOME": tmp_path / "config",
            "LLM_COORD_DATA_HOME": tmp_path / "data",
            "LLM_COORD_STATE_HOME": tmp_path / "state",
            "LLM_COORD_RUNTIME_DIR": Path(runtime).resolve(),
        }.items():
            monkeypatch.setenv(name, str(path))
        paths = AppPaths.resolve("test")
        _restore_profile(paths, tmp_path)
        before_git = (
            git_run(repository, "rev-parse", "HEAD"),
            git_run(repository, "write-tree"),
        )
        client = DaemonClient(paths, timeout_seconds=30)
        try:
            assert client.call("system.ping")["version"] == __version__
            sessions = client.call("session.list")
            assert {s["session_id"] for s in sessions} == {
                "upgrade-published-session",
                "upgrade-held-session",
            }
            published = client.call("task.show", {"task_id": "upgrade-published"})
            held = client.call("task.show", {"task_id": "upgrade-held"})
            assert published["state"] == "completed"
            assert held["state"] == "awaiting_review"
            proposal = client.call("task.diff", {"task_id": "upgrade-held"})
            assert proposal["status"] == "awaiting_review"
            assert "-value = 2" in proposal["diff"]
            assert "+value = 3" in proposal["diff"]
            for kind in ("published", "held"):
                events = client.call("task.events", {"task_id": f"upgrade-{kind}"})
                assert any(event["event_type"] == "model.finished" for event in events)
                shown = client.call(
                    "session.show", {"session_id": f"upgrade-{kind}-session"}
                )
                assert shown["session"]["checkout_id"] == sessions[0]["checkout_id"]
            with closing(sqlite3.connect(paths.control_db)) as connection:
                applied = connection.execute(
                    "SELECT version,name,checksum FROM schema_migrations "
                    "ORDER BY version"
                ).fetchall()
                assert applied == [
                    (m.version, m.name, m.checksum) for m in CONTROL_MIGRATIONS
                ]
                # This release crosses migrations 20-28. The floor catches removal
                # of the last migration as well as the normal contiguous-set check.
                assert applied[-1][0] >= 28
                checkouts = connection.execute(
                    "SELECT canonical_path FROM checkouts"
                ).fetchall()
                assert checkouts == [(str(repository),)]
                for (path,) in checkouts:
                    status = client.call("workspace.cutover.status", {"path": path})
                    assert status["regime"] == "legacy"
                    assert status["intent_id"] is None
                for table in (
                    "checkout_regimes",
                    "cutover_intents",
                    "workspace_executions",
                ):
                    assert connection.execute(
                        f"SELECT count(*) FROM {table}"
                    ).fetchone() == (0,)
                assert connection.execute(
                    "SELECT count(*) FROM workspace_batches WHERE state='published'"
                ).fetchone() == (1,)
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
                assert connection.execute("PRAGMA integrity_check").fetchone() == (
                    "ok",
                )
            assert (repository / "a.py").read_text() == "value = 2\n"
            assert (
                git_run(repository, "rev-parse", "HEAD"),
                git_run(repository, "write-tree"),
            ) == before_git
        finally:
            if paths.socket.exists():
                client.call("system.shutdown", autostart=False)
                assert client.wait_until_stopped(15)
