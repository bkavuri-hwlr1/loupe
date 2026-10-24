"""Migration 31 preserves conflict replay and refuses unreadable evidence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from llm_cli.storage.connection import connect_control
from llm_cli.storage.migrations import CONTROL_MIGRATIONS, apply_migrations
from llm_cli.workspace.broker import MAX_SHARED_TEXT_BYTES
from llm_cli.workspace.identity import ABSENT
from llm_cli.workspace.isolated_sync import _entry


def legacy(path: Path) -> sqlite3.Connection:
    con = connect_control(path)
    apply_migrations(con, CONTROL_MIGRATIONS[:30])
    con.execute(
        "INSERT INTO repositories(repository_id,repo_key,profile_id,display_name,"
        "git_common_dir,main_worktree_path,target_ref,created_at,updated_at,"
        "last_seen_at) VALUES('repo',?,'test','test','/tmp/r/.git','/tmp/r',"
        "'refs/heads/main',0,0,0)",
        ("a" * 64,),
    )
    con.execute(
        "INSERT INTO repository_heads(repo_key,last_effective_time,created_at,"
        "updated_at) "
        "VALUES(?,0,0,0)",
        ("a" * 64,),
    )
    con.execute(
        "INSERT INTO checkouts(checkout_id,repository_id,repo_key,canonical_path,"
        "git_common_dir,path_case_insensitive,created_at,updated_at) "
        "VALUES('checkout','repo',?,'/tmp/r','/tmp/r/.git',0,0,0)",
        ("a" * 64,),
    )
    for workspace in ("one", "two"):
        con.execute(
            "INSERT INTO workspaces(workspace_id,checkout_id,kind,mode,state,"
            "canonical_path,created_at,updated_at) "
            "VALUES(?,'checkout','linked_worktree','isolated','active',?,0,0)",
            (workspace, "/tmp/" + workspace),
        )
        con.execute(
            "INSERT INTO isolated_sync_state VALUES(?,'{}',0,'[]',12)",
            (workspace,),
        )
    return con


def step(path: str, disposition: str = "divergence_preserved") -> dict[str, object]:
    identity = _entry(ABSENT, None)
    return {
        "path": path,
        "disposition": disposition,
        "before": {
            key: value for key, value in identity.items() if key != "content_hash"
        },
        "after": {
            key: value for key, value in identity.items() if key != "content_hash"
        },
        "incoming": identity,
        "content_hash": "a" * 64,
        "local_content_hash": "b" * 64,
        "private_content_token": "c" * 32,
    }


def journals(con: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [tuple(row) for row in con.execute("SELECT * FROM isolated_sync_journal")]


def test_legacy_conflicts_replay_exactly_and_prepared_rows_are_unchanged(
    tmp_path: Path,
) -> None:
    con = legacy(tmp_path / "legacy.sqlite3")
    expected = {}
    for generation in range(12):
        steps = [step("retained"), step("cleared")]
        if generation == 11:
            steps += [step("cleared", "already_current"), step("duplicate")]
            steps += [step("duplicate", "merged"), step("last")]
        for item in steps:
            if item["disposition"] == "divergence_preserved":
                expected[item["path"]] = item
            else:
                expected.pop(item["path"], None)
        con.execute(
            "INSERT INTO isolated_sync_journal VALUES('one',?,?,?,?,'applied')",
            (generation, generation, '{"large":"manifest"}', json.dumps(steps)),
        )
    con.execute(
        "INSERT INTO isolated_sync_journal VALUES('two',0,0,'{}',?,'applied')",
        (json.dumps([step("independent")]),),
    )
    con.execute(
        "INSERT INTO isolated_sync_journal "
        "VALUES('one',12,12,'{ \"pending\" : true }',?,'prepared')",
        (json.dumps([step("never applied")]),),
    )
    prepared = tuple(
        con.execute(
            "SELECT * FROM isolated_sync_journal WHERE state='prepared'"
        ).fetchone()
    )
    assert apply_migrations(con, CONTROL_MIGRATIONS[:31]) == 31
    actual = {
        row["path"]: json.loads(row["step_json"])
        for row in con.execute(
            "SELECT * FROM isolated_sync_conflicts WHERE workspace_id='one'"
        )
    }
    assert actual == expected
    assert (
        tuple(
            con.execute(
                "SELECT * FROM isolated_sync_journal WHERE state='prepared'"
            ).fetchone()
        )
        == prepared
    )
    assert (
        con.execute(
            "SELECT count(*) FROM isolated_sync_journal WHERE workspace_id='one' "
            "AND state='applied'"
        ).fetchone()[0]
        == 8
    )
    assert all(
        row[0] == "{}"
        for row in con.execute(
            "SELECT manifest_json FROM isolated_sync_journal WHERE state='applied'"
        )
    )
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


@pytest.mark.parametrize(
    "corrupt",
    [
        "not json",
        "{}",
        "[null]",
        "[{}]",
        json.dumps([step("bad") | {"disposition": "unknown"}]),
        json.dumps([step("bad") | {"after": {"kind": "unknown"}}]),
        json.dumps([step("bad") | {"path": None}]),
        json.dumps(
            [
                step("bad")
                | {
                    "after": {
                        "kind": "regular",
                        "mode": "100644",
                        "digest": "a" * 64,
                        "size": MAX_SHARED_TEXT_BYTES + 1,
                    }
                }
            ]
        ),
        json.dumps([step("bad") | {"after": dict(step("bad")["after"], unknown=True)}]),
        json.dumps([step("bad")]).replace(
            '"path": "bad"', '"path": "bad", "path": "other"'
        ),
    ],
)
def test_corrupt_applied_evidence_aborts_the_whole_migration(
    tmp_path: Path,
    corrupt: str,
) -> None:
    con = legacy(tmp_path / "legacy.sqlite3")
    con.execute(
        "INSERT INTO isolated_sync_journal "
        "VALUES('one',0,0,'{\"evidence\":true}',?,'applied')",
        (corrupt,),
    )
    before = journals(con)
    with pytest.raises(sqlite3.DatabaseError):
        apply_migrations(con)
    assert journals(con) == before
    assert con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 30
    assert (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='isolated_sync_conflicts'"
        ).fetchone()
        is None
    )
    con.close()


@pytest.mark.parametrize("boundary", range(10))
def test_interrupted_migration_rolls_back_every_compaction_statement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: int,
) -> None:
    from llm_cli.storage import migrations

    con = legacy(tmp_path / "legacy.sqlite3")
    for generation in range(12):
        con.execute(
            "INSERT INTO isolated_sync_journal VALUES('one',?,?,?,?,'applied')",
            (generation, generation, '{"evidence":true}', json.dumps([step("old")])),
        )
    before = journals(con)
    original = migrations._iter_statements

    def interrupted(sql: str) -> tuple[str, ...]:
        if sql != CONTROL_MIGRATIONS[30].sql:
            return original(sql)
        statements = original(sql)
        # Raise through a connection-local SQL function at each statement boundary.
        return (
            *statements[:boundary],
            "SELECT c16_injected_crash();",
            *statements[boundary:],
        )

    def crash() -> None:
        raise RuntimeError("interrupted migration")

    con.create_function("c16_injected_crash", 0, crash)
    monkeypatch.setattr(migrations, "_iter_statements", interrupted)
    with pytest.raises(sqlite3.DatabaseError):
        apply_migrations(con)
    assert journals(con) == before
    assert con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 30
    assert (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='isolated_sync_conflicts'"
        ).fetchone()
        is None
    )
    con.close()
