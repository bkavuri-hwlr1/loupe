"""Bounded sync history keeps exact conflict evidence and crash recovery."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from test_isolated_creation_safety import checkout, manager
from test_isolated_sync import Environment
from test_isolated_sync import env as environment_fixture
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.workspace import isolated_sync
from llm_cli.workspace.broker import WorkspaceBrokerError, load_candidate_content
from llm_cli.workspace.isolated_sync import IsolatedSynchronizer

env = environment_fixture


def conflict(env: Environment) -> dict[str, Any]:
    (env.local / "a.txt").write_text("private overlap\nmiddle\nlast\n")
    env.publish("conflict", {"a.txt": b"incoming overlap\nmiddle\nlast\n"})
    env.sync.synchronize(env.workspace_id, refresh_source=False)
    return env.sync._conflicts(env.workspace_id)["a.txt"]


def test_200_growing_syncs_bound_history_and_retain_old_conflict_artifacts(
    env: Environment,
) -> None:
    original = conflict(env)
    for number in range(200):
        (env.primary / f"growth-{number}.txt").write_text(f"source {number}\n")
        assert env.sync.synchronize(env.workspace_id).conflicted == ()
    with env.store.connection() as con:
        rows = con.execute("SELECT * FROM isolated_sync_journal").fetchall()
        assert len(rows) == isolated_sync._APPLIED_JOURNAL_WINDOW
        assert all(row["manifest_json"] == "{}" for row in rows)
        assert all(row["state"] == "applied" for row in rows)
        assert sum(len(row["steps_json"]) for row in rows) < 16_000
        assert (
            con.execute("SELECT count(*) FROM isolated_sync_conflicts").fetchone()[0]
            == 1
        )
    restarted = IsolatedSynchronizer(env.store, env.publisher, env.sync.capture)
    assert restarted._conflicts(env.workspace_id) == {"a.txt": original}
    assert (
        load_candidate_content(
            env.publisher.candidate_dir, original["local_content_hash"]
        )
        == b"private overlap\nmiddle\nlast\n"
    )
    assert (
        load_candidate_content(
            env.publisher.candidate_dir, original["incoming"]["content_hash"]
        )
        == b"incoming overlap\nmiddle\nlast\n"
    )
    with pytest.raises(WorkspaceBrokerError, match="conflict remains unresolved"):
        restarted.publication_files(env.workspace_id, ())
    (env.local / "a.txt").write_text("explicit resolution\n")
    assert (
        restarted.publication_files(env.workspace_id, ())[0].content
        == b"explicit resolution\n"
    )
    # Receiving exactly the local resolution clears the durable divergence.
    env.publish("resolution", {"a.txt": b"explicit resolution\n"})
    restarted.synchronize(env.workspace_id, refresh_source=False)
    assert restarted._conflicts(env.workspace_id) == {}


@pytest.mark.parametrize(
    "failpoint",
    [
        "UPDATE isolated_sync_state SET manifest_json",
        "INSERT INTO isolated_sync_conflicts",
        "DELETE FROM isolated_sync_conflicts",
        "UPDATE isolated_sync_journal SET state='applied'",
        "DELETE FROM isolated_sync_journal",
        "before_commit",
        "after_commit",
    ],
)
def test_confirmation_and_compaction_crashes_never_repeat_a_filesystem_step(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
    failpoint: str,
) -> None:
    # Fill the window so the final transaction really retires an applied row.
    for number in range(8):
        (env.primary / "b.txt").write_text(f"old {number}\n")
        env.sync.synchronize(env.workspace_id)
    (env.local / "a.txt").write_text("private overlap\nmiddle\nlast\n")
    env.publish(
        "incoming",
        {
            "a.txt": b"incoming overlap\nmiddle\nlast\n",
            "b.txt": b"final\n",
        },
    )
    with env.store.connection() as con:
        batch = con.execute(
            "SELECT checkout_event_sequence,request_json FROM workspace_batches "
            "WHERE batch_id='incoming'"
        ).fetchone()
        before = con.execute("SELECT generation FROM isolated_sync_state").fetchone()[0]
    env.sync._prepare(env.workspace_id, batch[0], batch[1])
    original_connection = env.store.connection
    crashed = False
    armed = False

    class Connection:
        def __init__(self, con: sqlite3.Connection) -> None:
            self.con = con

        def __getattr__(self, name: str) -> Any:
            return getattr(self.con, name)

        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            nonlocal crashed, armed
            cursor = self.con.execute(sql, *args)
            armed |= sql.startswith("UPDATE isolated_sync_state SET manifest_json")
            if not crashed and sql.startswith(failpoint):
                crashed = True
                raise RuntimeError("injected confirmation crash")
            return cursor

        def commit(self) -> None:
            nonlocal crashed
            if armed and not crashed and failpoint == "before_commit":
                crashed = True
                raise RuntimeError("injected confirmation crash")
            self.con.commit()
            if armed and not crashed and failpoint == "after_commit":
                crashed = True
                raise RuntimeError("injected confirmation crash")

    @contextmanager
    def connections() -> Iterator[Connection]:
        with original_connection() as con:
            yield Connection(con)

    writes = 0
    original_write = isolated_sync.atomic_replace_regular_file

    def write(**kwargs: Any) -> None:
        nonlocal writes
        writes += 1
        original_write(**kwargs)

    monkeypatch.setattr(env.store, "connection", connections)
    monkeypatch.setattr(isolated_sync, "atomic_replace_regular_file", write)
    with pytest.raises(RuntimeError, match="confirmation crash"):
        env.sync.recover(env.workspace_id)
    monkeypatch.setattr(env.store, "connection", original_connection)
    with env.store.connection() as con:
        pending = con.execute(
            "SELECT state,manifest_json FROM isolated_sync_journal WHERE generation=?",
            (before,),
        ).fetchone()
        assert pending[0] == ("applied" if failpoint == "after_commit" else "prepared")
        assert (pending[1] == "{}") == (failpoint == "after_commit")
    restarted = IsolatedSynchronizer(env.store, env.publisher, env.sync.capture)
    restarted.recover(env.workspace_id)
    restarted.recover(env.workspace_id)
    assert writes == 1
    assert (env.local / "b.txt").read_bytes() == b"final\n"
    assert "a.txt" in restarted._conflicts(env.workspace_id)
    with env.store.connection() as con:
        assert (
            con.execute("SELECT generation FROM isolated_sync_state").fetchone()[0]
            == before + 1
        )
        assert (
            con.execute("SELECT count(*) FROM isolated_sync_journal").fetchone()[0] == 8
        )


def test_legacy_upgrade_compacts_old_conflict_and_recovers_prepared_row(
    env: Environment,
) -> None:
    original = conflict(env)
    for number in range(10):
        (env.primary / "b.txt").write_text(f"source {number}\n")
        env.sync.synchronize(env.workspace_id)
    env.publish("pending", {"b.txt": b"pending incoming\n"})
    with env.store.connection() as con:
        batch = con.execute(
            "SELECT checkout_event_sequence,request_json FROM workspace_batches "
            "WHERE batch_id='pending'"
        ).fetchone()
    env.sync._prepare(env.workspace_id, batch[0], batch[1])
    with env.store.connection() as con:
        # Reconstruct an actual version-30 database whose only retained conflict
        # is older than the diagnostic window. Keep the real prepared transition.
        con.execute("DROP TABLE isolated_sync_conflicts")
        con.execute("DELETE FROM schema_migrations WHERE version=31")
        con.execute(
            "INSERT INTO isolated_sync_journal "
            "VALUES(?,0,0,'{\"old_manifest\":true}',?,'applied')",
            (env.workspace_id, json.dumps([original])),
        )
        prepared = tuple(
            con.execute(
                "SELECT * FROM isolated_sync_journal WHERE state='prepared'"
            ).fetchone()
        )
    env.store.initialize()
    with env.store.connection() as con:
        assert (
            tuple(
                con.execute(
                    "SELECT * FROM isolated_sync_journal WHERE state='prepared'"
                ).fetchone()
            )
            == prepared
        )
    restarted = IsolatedSynchronizer(env.store, env.publisher, env.sync.capture)
    restarted.recover(env.workspace_id)
    assert restarted._conflicts(env.workspace_id) == {"a.txt": original}
    assert (env.local / "b.txt").read_bytes() == b"pending incoming\n"
    with pytest.raises(WorkspaceBrokerError, match="conflict remains unresolved"):
        restarted.publication_files(env.workspace_id, ())


@pytest.mark.parametrize("unresolved", [False, True])
def test_screened_tokens_survive_compaction_until_owned_worktree_retirement(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unresolved: bool,
) -> None:
    from llm_cli.workspace import isolated

    environment = batch_env
    isolate = manager(environment, tmp_path)
    workspace, _ = isolate.open(checkout(environment), "first")
    state = isolate.validate(workspace.workspace_id)
    private = Path(workspace.canonical_path)
    sync = IsolatedSynchronizer(
        environment.store,
        environment.publisher,
        lambda root, protected=(): isolated.capture_source(
            root, environment.publisher.candidate_dir, protected_paths=protected
        ),
        isolate.validate,
    )
    sync.register(
        workspace.workspace_id,
        state.source_manifest,
        state.source_sequence,
        state.runtime_paths,
    )
    original = b"KEY = 'AKIA" + b"A" * 16 + b"'\n"
    incoming = original.replace(b"A" * 16, b"B" * 16)
    (environment.repository / "fixture.py").write_bytes(original)
    sync.synchronize(workspace.workspace_id)
    if unresolved:
        (private / "fixture.py").write_bytes(original.replace(b"A" * 16, b"L" * 16))
    (environment.repository / "fixture.py").write_bytes(incoming)
    original_unlink = isolated_sync.os.unlink

    def crash_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if isinstance(path, str) and len(path) == 32:
            raise RuntimeError("crash after commit before opaque staging unlink")
        original_unlink(path, *args, **kwargs)

    if unresolved:
        sync.synchronize(workspace.workspace_id)
    else:
        monkeypatch.setattr(isolated_sync.os, "unlink", crash_unlink)
        with pytest.raises(RuntimeError, match="after commit"):
            sync.synchronize(workspace.workspace_id)
        monkeypatch.setattr(isolated_sync.os, "unlink", original_unlink)
    with environment.store.connection() as con:
        git_dir = Path(
            con.execute(
                "SELECT git_dir FROM workspaces WHERE workspace_id=?",
                (workspace.workspace_id,),
            ).fetchone()[0]
        )
    staged = [
        path
        for path in (git_dir / "loupe-screened-incoming").iterdir()
        if path.read_bytes() == incoming
    ]
    assert len(staged) == 1
    for number in range(10):
        (environment.repository / "a.txt").write_text(f"ordinary source {number}\n")
        sync.synchronize(workspace.workspace_id)
    restarted = IsolatedSynchronizer(
        environment.store,
        environment.publisher,
        sync.capture,
        isolate.validate,
    )
    retained = restarted._conflicts(workspace.workspace_id)
    if unresolved:
        assert retained["fixture.py"]["private_content_token"] == staged[0].name
        assert (
            restarted._screened_body(
                restarted._state(workspace.workspace_id), retained["fixture.py"]
            )
            == incoming
        )
    else:
        assert retained == {}
    assert staged[0].read_bytes() == incoming
    with environment.store.connection() as con:
        assert (
            con.execute(
                "SELECT count(*) FROM isolated_sync_journal WHERE workspace_id=?",
                (workspace.workspace_id,),
            ).fetchone()[0]
            == 8
        )
    environment.store.close_session("first")
    isolate.cleanup_closed("first")
    assert not private.exists()
    assert not git_dir.exists()
    assert environment.store.get_workspace(workspace.workspace_id).state == "removed"
