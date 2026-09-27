"""In-place schema upgrades over a database that already holds rows."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from llm_cli.storage.connection import connect_control
from llm_cli.storage.migrations import (
    CONTROL_MIGRATIONS,
    Migration,
    MigrationError,
    apply_migrations,
    current_schema_version,
)

_LATEST = CONTROL_MIGRATIONS[-1].version


def _legacy_repository(path: Path, through_version: int) -> None:
    """Build a database at an older schema, holding one registered repository."""

    older = tuple(
        item for item in CONTROL_MIGRATIONS if item.version <= through_version
    )
    with closing(connect_control(path)) as connection:
        apply_migrations(connection, older)
        connection.execute(
            """
            INSERT INTO repositories(
                repository_id, repo_key, profile_id, display_name,
                git_common_dir, main_worktree_path, target_ref,
                created_at, updated_at, last_seen_at
            ) VALUES ('repo_legacy', ?, 'default', 'legacy', '/tmp/x/.git',
                      '/tmp/x', 'refs/heads/main', 1, 1, 1)
            """,
            ("f" * 64,),
        )
        connection.commit()


def test_upgrading_preserves_rows_and_defaults_them_safely(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    _legacy_repository(database, through_version=5)

    with closing(connect_control(database)) as connection:
        applied = apply_migrations(connection)

        assert applied == _LATEST
        row = connection.execute(
            "SELECT * FROM repositories WHERE repository_id = 'repo_legacy'"
        ).fetchone()
        assert row is not None
        assert row["display_name"] == "legacy"
        # A row that predates the probe was never asked whether its working
        # tree is case-insensitive. Contending is the direction that only costs
        # parallelism, so that is where an unprobed row lands.
        assert row["path_case_insensitive"] == 1
        assert row["coordinate_by_remote"] == 1
        assert "boot_id" in {
            column["name"]
            for column in connection.execute("PRAGMA table_info(task_executions)")
        }
        tables = {
            str(item["name"])
            for item in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        assert {
            "workspace_read_observations",
            "workspace_candidates",
            "workspace_publications",
            "workspace_divergences",
        } <= tables


def test_upgrading_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    _legacy_repository(database, through_version=6)

    with closing(connect_control(database)) as connection:
        assert apply_migrations(connection) == _LATEST
        assert apply_migrations(connection) == _LATEST
        assert current_schema_version(connection) == _LATEST


def test_effort_upgrade_preserves_existing_conversation_and_validates_new_values(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.sqlite3"
    _legacy_repository(database, through_version=14)
    with closing(connect_control(database)) as connection:
        connection.execute(
            """INSERT INTO repository_heads(
                repo_key, last_effective_time, created_at, updated_at
            ) VALUES (?, 1, 1, 1)""",
            ("f" * 64,),
        )
        connection.execute(
            """INSERT INTO checkouts(
                checkout_id, repository_id, repo_key, canonical_path,
                git_common_dir, path_case_insensitive, created_at, updated_at
            ) VALUES ('checkout_old', 'repo_legacy', ?, '/tmp/x', '/tmp/x/.git',
                      0, 1, 1)""",
            ("f" * 64,),
        )
        connection.execute(
            """INSERT INTO sessions(
                session_id, checkout_id, resume_token_hash, provider, model,
                state, conversation_json, opened_at, last_heartbeat_at, updated_at
            ) VALUES ('session_old', 'checkout_old', ?, 'openai', 'gpt-5.3-codex',
                      'disconnected', '{"input": []}', 1, 1, 1)""",
            ("a" * 64,),
        )
        connection.commit()
        apply_migrations(connection)
        row = connection.execute("SELECT * FROM sessions").fetchone()
        assert row["effort"] is None
        assert row["conversation_json"] == '{"input": []}'
        assert row["resume_token_hash"] == "a" * 64
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE sessions SET effort = 'invented'")
        connection.execute("UPDATE sessions SET effort = 'high'")
        assert connection.execute("SELECT effort FROM sessions").fetchone()[0] == "high"


def test_a_rewritten_migration_is_refused_before_anything_newer_runs(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.sqlite3"
    _legacy_repository(database, through_version=_LATEST)
    tampered = (
        Migration(1, CONTROL_MIGRATIONS[0].name, "SELECT 1;"),
        *CONTROL_MIGRATIONS[1:],
    )

    with (
        closing(connect_control(database)) as connection,
        pytest.raises(MigrationError),
    ):
        apply_migrations(connection, tampered)


def _legacy_execution(path: Path, through_version: int) -> None:
    """Add a full task/claim/execution chain at an older schema version."""

    _legacy_repository(path, through_version)
    with closing(connect_control(path)) as connection:
        key = "f" * 64
        connection.execute(
            """
            INSERT INTO repository_heads(
                repo_key, last_effective_time, created_at, updated_at
            ) VALUES (?, 1, 1, 1)
            """,
            (key,),
        )
        connection.execute(
            """
            INSERT INTO tasks(task_id, repository_id, repo_key, created_at, updated_at)
            VALUES ('task_legacy', 'repo_legacy', ?, 1, 1)
            """,
            (key,),
        )
        connection.execute(
            """
            INSERT INTO claims(
                claim_id, task_id, task_attempt, repo_key, state, queue_sequence,
                fencing_token, lease_expires_at, created_at, updated_at
            ) VALUES ('claim_legacy', 'task_legacy', 1, ?, 'active_work', 1,
                      1, 9999999999999, 1, 1)
            """,
            (key,),
        )
        connection.execute(
            """
            INSERT INTO task_executions(
                execution_id, task_id, task_attempt, claim_id, driver, state,
                worktree_path, base_oid, created_at, updated_at
            ) VALUES ('exec_legacy', 'task_legacy', 1, 'claim_legacy',
                      'fixture_write', 'running', '/tmp/wt', 'a' * 40, 1, 1)
            """
        )
        connection.commit()


def test_rebuilding_the_executions_table_preserves_its_rows(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    _legacy_execution(database, through_version=7)

    with closing(connect_control(database)) as connection:
        assert apply_migrations(connection) == _LATEST

        row = connection.execute(
            "SELECT * FROM task_executions WHERE execution_id = 'exec_legacy'"
        ).fetchone()
        assert row is not None
        assert row["state"] == "running"
        assert row["driver"] == "fixture_write"
        assert row["worktree_path"] == "/tmp/wt"
        # New columns take their defaults rather than nulling the row out.
        assert row["tool_calls"] == 0
        assert row["summary"] is None
        # A driver name the old CHECK constraint would have rejected now fits.
        connection.execute(
            "UPDATE task_executions SET driver = 'coding_agent' "
            "WHERE execution_id = 'exec_legacy'"
        )
        indexes = {
            item["name"]
            for item in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'index' "
                "AND tbl_name = 'task_executions'"
            )
        }
        assert "task_executions_boot_idx" in indexes
        assert "task_executions_claim_idx" in indexes


def test_answer_upgrade_preserves_legacy_finished_checkpoint(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    _legacy_execution(database, through_version=17)
    checkpoint = '{"phase":"finished","final_summary":"Legacy summary"}'
    with closing(connect_control(database)) as connection:
        connection.execute(
            """INSERT INTO execution_checkpoints(
                execution_id, driver, checkpoint_json, revision, created_at, updated_at
            ) VALUES ('exec_legacy', 'fixture_write', ?, 3, 1, 2)""",
            (checkpoint,),
        )
        connection.commit()
        assert apply_migrations(connection) == _LATEST
        row = connection.execute("SELECT * FROM execution_checkpoints").fetchone()
        assert row["checkpoint_json"] == checkpoint
        assert row["revision"] == 3
        assert connection.execute("SELECT * FROM execution_answers").fetchall() == []
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_optimistic_migration_keeps_old_claims_exclusive(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    _legacy_execution(database, through_version=13)

    with closing(connect_control(database)) as connection:
        assert apply_migrations(connection) == _LATEST
        claim = connection.execute(
            "SELECT * FROM claims WHERE claim_id = 'claim_legacy'"
        ).fetchone()
        assert claim is not None
        assert claim["state"] == "active_work"
        assert claim["scheduling_mode"] == "exclusive"
        assert claim["workspace_id"] is None
        assert claim["fencing_token"] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                "UPDATE claims SET scheduling_mode = 'optimistic' "
                "WHERE claim_id = 'claim_legacy'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                "UPDATE claims SET scheduling_mode = 'unknown' "
                "WHERE claim_id = 'claim_legacy'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "UPDATE claims SET scheduling_mode = 'optimistic', "
                "workspace_id = 'missing-workspace' WHERE claim_id = 'claim_legacy'"
            )


@pytest.mark.parametrize(("publish", "mode"), [("auto", "auto"), ("review", "normal")])
def test_agent_modes_upgrade_preserves_existing_policy_and_task_snapshot(
    tmp_path: Path, publish: str, mode: str
) -> None:
    database = tmp_path / "control.sqlite3"
    _legacy_execution(database, through_version=16)
    with closing(connect_control(database)) as connection:
        connection.execute(
            """INSERT INTO checkouts(
                checkout_id,repository_id,repo_key,canonical_path,git_common_dir,
                path_case_insensitive,created_at,updated_at
            ) VALUES ('checkout_old','repo_legacy',?,'/tmp/x','/tmp/x/.git',0,1,1)""",
            ("f" * 64,),
        )
        connection.execute(
            """INSERT INTO sessions(
                session_id,checkout_id,resume_token_hash,provider,model,state,
                conversation_json,opened_at,last_heartbeat_at,updated_at
            ) VALUES ('session_old','checkout_old',?,'scripted','mode','disconnected',
                '{"messages": []}',1,1,1)""",
            ("a" * 64,),
        )
        connection.execute(
            "INSERT INTO session_publication_policy(session_id,mode) VALUES (?,?)",
            ("session_old", publish),
        )
        connection.execute(
            """INSERT INTO task_workflows(task_id,attempt,publish_mode,proposal_json,
                created_at) VALUES ('task_legacy',1,?,'[]',1)""",
            (publish,),
        )
        connection.commit()
        assert apply_migrations(connection) == _LATEST
        assert apply_migrations(connection) == _LATEST
        session = connection.execute("SELECT * FROM sessions").fetchone()
        task = connection.execute("SELECT * FROM task_workflows").fetchone()
        assert session["agent_mode"] == mode
        assert session["conversation_json"] == '{"messages": []}'
        assert session["resume_token_hash"] == "a" * 64
        assert task["agent_mode"] == mode
        assert task["publish_mode"] == publish and task["proposal_json"] == "[]"
        for table in ("sessions", "task_workflows"):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                connection.execute(f"UPDATE {table} SET agent_mode='unknown'")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
