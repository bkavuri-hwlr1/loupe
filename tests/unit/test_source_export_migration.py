"""Source export receipts are an additive upgrade over release schema33."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from test_migrations import _legacy_repository

from llm_cli.storage.connection import connect_control
from llm_cli.storage.migrations import apply_migrations, current_schema_version


def test_export_upgrade_preserves_existing_release_rows(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    _legacy_repository(database, through_version=33)
    with closing(connect_control(database)) as connection:
        assert current_schema_version(connection) == 33
        assert apply_migrations(connection) == 34
        assert (
            connection.execute(
                "SELECT display_name FROM repositories "
                "WHERE repository_id='repo_legacy'"
            ).fetchone()[0]
            == "legacy"
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(export_intents_v2)")
        }
        assert {
            "canonical_request_hash",
            "source_manifest_json",
            "stage_dev",
            "stage_ino",
        } <= columns
        assert not {"body", "content", "patch", "bundle"} & columns
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_export_schema_rejects_request_mutation_without_touching_old_migrations(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.sqlite3"
    _legacy_repository(database, through_version=33)
    with closing(connect_control(database)) as connection:
        before = [
            tuple(row)
            for row in connection.execute(
                "SELECT version,name,checksum FROM schema_migrations"
            )
        ]
        apply_migrations(connection)
        after = [
            tuple(row)
            for row in connection.execute(
                "SELECT version,name,checksum FROM schema_migrations WHERE version<=33"
            )
        ]
        assert before == after
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='source_export_request_immutable'"
        ).fetchone()[0]
        assert "source_manifest_json" in trigger and "destination" in trigger
