"""Runtime-only safe point refresh, frozen review inputs, and crash recovery."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from test_isolated_creation_safety import checkout, manager
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.agent.source_policy import contains_secret_material


def prepare(
    env: BatchEnvironment, tmp_path: Path, *, approved: tuple[str, ...] = ()
) -> tuple[Any, Any, Path]:
    (env.repository / ".gitignore").write_text("*.local\n.env\nnode_modules/\n.venv/\n")
    (env.repository / "a.local").write_text("before a\n")
    (env.repository / "b.local").write_text("before b\n")
    (env.repository / ".env").write_text("MARKER=opening-private-runtime\n")
    isolate = manager(env, tmp_path)
    workspace, _ = isolate.open(
        checkout(env), "runtime-refresh", approved_secrets=approved
    )
    return isolate, workspace, Path(workspace.canonical_path)


def test_refresh_is_stat_cached_and_new_entries_stay_out_of_source_ledger(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.workspace import isolated

    env = batch_env
    isolate, workspace, private = prepare(env, tmp_path)
    assert isolate.runtime.refresh(workspace.workspace_id) == ()
    (env.repository / "a.local").write_text("after a\n")
    (env.repository / "new.local").write_text("new runtime\n")
    with env.store.connection() as con:
        count = con.execute("SELECT count(*) FROM checkout_events").fetchone()[0]
    notes = isolate.runtime.refresh(workspace.workspace_id)
    assert "Runtime refreshed: a.local" in notes
    assert "Runtime refreshed: new.local" in notes
    assert (private / "a.local").read_text() == "after a\n"
    assert (private / "new.local").read_text() == "new runtime\n"
    assert (env.repository / "a.local").read_text() == "after a\n"
    state = isolate.get(workspace.workspace_id)
    assert state and "new.local" in state.runtime_files
    assert "new.local" not in isolated.capture_source(
        private, env.publisher.candidate_dir
    )
    # Unchanged runtime content is never opened again by refresh detection.
    monkeypatch.setattr(
        isolated, "copy_runtime_file", lambda *args: pytest.fail("uncached copy")
    )
    assert isolate.runtime.refresh(workspace.workspace_id) == ()
    with env.store.connection() as con:
        assert (
            con.execute("SELECT count(*) FROM checkout_events").fetchone()[0] == count
        )
        assert con.execute("SELECT count(*) FROM workspace_batches").fetchone()[0] == 0


def test_private_conflict_once_and_unapproved_env_change_are_preserved(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    isolate, workspace, private = prepare(env, tmp_path)
    (private / "a.local").write_text("private edit\n")
    (env.repository / "a.local").write_text("primary edit\n")
    (env.repository / ".env").write_text("MARKER=later-private-runtime\n")
    notes = isolate.runtime.refresh(workspace.workspace_id)
    assert "Runtime conflict; preserved private copy: a.local" in notes
    assert any(".env" in note and "consent" in note for note in notes)
    assert (private / "a.local").read_text() == "private edit\n"
    assert not (private / ".env").exists()
    assert isolate.runtime.refresh(workspace.workspace_id) == ()
    # Restart does not repeat either notice or accidentally grant consent.
    restarted = manager(env, tmp_path)
    assert restarted.runtime.refresh(workspace.workspace_id) == ()
    assert not (private / ".env").exists()


def test_runtime_change_waits_for_running_check_activity(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    isolate, workspace, private = prepare(env, tmp_path)
    started, finish = threading.Event(), threading.Event()

    def running_check() -> None:
        with isolate.runtime.activity(workspace.workspace_id):
            started.set()
            assert finish.wait(5)
            assert (private / "a.local").read_text() == "before a\n"

    thread = threading.Thread(target=running_check)
    thread.start()
    assert started.wait(5)
    try:
        (env.repository / "a.local").write_text("after check\n")
        assert isolate.runtime.refresh(workspace.workspace_id) == ()
        assert (private / "a.local").read_text() == "before a\n"
    finally:
        finish.set()
        thread.join(5)
    assert not thread.is_alive()
    assert isolate.runtime.refresh(workspace.workspace_id) == (
        "Runtime refreshed: a.local",
    )
    assert (private / "a.local").read_text() == "after check\n"


@pytest.mark.parametrize(
    "stage",
    [
        "runtime_staging",
        "runtime_prepared",
        "runtime_removed:a.local",
        "runtime_installed:a.local",
        "runtime_removed:b.local",
        "runtime_installed:b.local",
        "runtime_committed",
        "runtime_cleaned",
    ],
)
def test_crash_each_refresh_step_recovers_one_complete_generation(
    batch_env: BatchEnvironment, tmp_path: Path, stage: str
) -> None:
    env = batch_env
    isolate, workspace, private = prepare(env, tmp_path)
    (env.repository / "a.local").write_text("after a\n")
    (env.repository / "b.local").write_text("after b\n")

    class Crash(BaseException):
        pass

    def fault(observed: str) -> None:
        if observed == stage:
            raise Crash()

    isolate.runtime.fault = fault
    with pytest.raises(Crash):
        isolate.runtime.refresh(workspace.workspace_id)
    restarted = manager(env, tmp_path)
    restarted.runtime.recover(workspace.workspace_id)
    expected = "before" if stage == "runtime_staging" else "after"
    assert (private / "a.local").read_text() == expected + " a\n"
    assert (private / "b.local").read_text() == expected + " b\n"
    restarted.runtime.recover(workspace.workspace_id)
    restarted.runtime.refresh(workspace.workspace_id)
    assert (private / "a.local").read_text() == "after a\n"
    assert not list(private.parent.glob(".runtime-*/refresh_*"))
    with env.store.connection() as con:
        assert (
            con.execute("SELECT count(*) FROM isolated_runtime_journal").fetchone()[0]
            == 0
        )


def test_refresh_detects_nested_dependencies_and_deletion(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    (env.repository / "node_modules/pkg").mkdir(parents=True)
    (env.repository / "node_modules/pkg/module.js").write_text("before\n")
    isolate, workspace, private = prepare(env, tmp_path)
    before = (env.repository / "node_modules").stat().st_mtime_ns
    (env.repository / "node_modules/pkg/module.js").write_text("after\n")
    assert (env.repository / "node_modules").stat().st_mtime_ns == before
    assert isolate.runtime.refresh(workspace.workspace_id) == (
        "Runtime refreshed: node_modules",
    )
    assert (private / "node_modules/pkg/module.js").read_text() == "after\n"
    (env.repository / "b.local").unlink()
    assert isolate.runtime.refresh(workspace.workspace_id) == (
        "Runtime refreshed: b.local",
    )
    assert not (private / "b.local").exists()


def test_recovery_refuses_later_private_write_without_overwriting_it(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    isolate, workspace, private = prepare(env, tmp_path)
    (env.repository / "a.local").write_text("after\n")

    class Crash(BaseException):
        pass

    def fault(stage: str) -> None:
        if stage == "runtime_prepared":
            raise Crash()

    isolate.runtime.fault = fault
    with pytest.raises(Crash):
        isolate.runtime.refresh(workspace.workspace_id)
    (private / "a.local").write_text("late user edit\n")
    with pytest.raises(Exception, match="private input changed"):
        manager(env, tmp_path).runtime.recover(workspace.workspace_id)
    assert (private / "a.local").read_text() == "late user edit\n"


def test_frozen_generation_apply_survives_refresh_and_screens_new_canary(
    tmp_path: Path, repository_factory: Any, service_factory: Any
) -> None:
    from test_isolated_review_workflow import BASE, SECRET
    from test_isolated_review_workflow import prepare as held_prepare
    from test_shared_agent_execution import _request

    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": BASE, ".gitignore": ".env\n"})
        (root / ".env").write_text(SECRET)
        service = service_factory(tmp_path)
        credentials, private = await held_prepare(service, root)
        session = service.store.get_session("isolated-review")
        assert session is not None
        workspace_id = session.workspace_id
        assert workspace_id is not None
        frozen = service.isolated_workspaces.runtime.task_root(workspace_id, "held", 1)
        assert (frozen / ".env").read_text() == SECRET
        canary = "c8-refresh-ordinary-marker-82f149a0"
        assert not contains_secret_material(canary)
        (root / ".env").write_text("MARKER=" + canary + "\n")
        assert service.isolated_workspaces.runtime.refresh(workspace_id) == (
            "Runtime refreshed: .env",
        )
        assert canary in (private / ".env").read_text()
        assert (frozen / ".env").read_text() == SECRET
        # An actual configured check prints the new value through the same
        # runtime protection used by task checks. Its output remains useful.
        from llm_cli.execution.checks import CheckRunner, validate_config

        workflow = service.workflow
        row = workflow.get("held")
        assert row is not None
        config = validate_config(
            {
                "runtime_paths": [".env"],
                "checks": {
                    "print-runtime": {
                        "argv": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; "
                            "print(Path('.env').read_text()); "
                            "print('useful diagnostic')",
                        ]
                    }
                },
            }
        )
        check_row = {**row, "config_json": json.dumps(config)}
        events: list[tuple[str, dict[str, object]]] = []
        runner = CheckRunner(
            workflow,
            check_row,
            root,
            tmp_path / "refresh-checks",
            lambda: (),
            lambda: False,
            lambda kind, payload: events.append((kind, payload)),
            service.workflow.publisher.lock,
            runtime_root=private,
        )
        assert not runner.run("print-runtime").is_error
        with service.store.connection() as con:
            output = con.execute(
                "SELECT output FROM check_runs WHERE name='print-runtime'"
            ).fetchone()[0]
        assert "[private value]" in output and "useful diagnostic" in output
        assert canary not in output and canary not in repr(events)
        service.close()
        service = service_factory(tmp_path)
        service.initialize()
        try:
            result = await service.handle(_request("task.apply", {"task_id": "held"}))
            assert result["state"] == "published"
            assert (private / ".env").read_text() == "MARKER=" + canary + "\n"
            with service.store.connection() as con:
                for table in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ):
                    assert canary not in repr(
                        [
                            tuple(row)
                            for row in con.execute('SELECT * FROM "' + table[0] + '"')
                        ]
                    )
            assert all(
                canary.encode() not in path.read_bytes()
                for path in service.workflow.publisher.candidate_dir.rglob("*")
                if path.is_file()
            )
            for directory in (tmp_path / "state", tmp_path / "data"):
                for path in directory.rglob("*"):
                    if path.is_file() and ("log" in path.name or "sqlite" in path.name):
                        assert canary.encode() not in path.read_bytes()
            await service.handle(_request("session.close", credentials))
            assert not private.exists()
            assert not frozen.parent.exists()
        finally:
            service.close()

    asyncio.run(scenario())


def test_dependency_staging_does_not_hold_publisher_lock_or_block_check_start(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.workspace import runtime

    env = batch_env
    dependency = env.repository / "node_modules/pkg/module.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("before")
    isolate, workspace, private = prepare(env, tmp_path)
    dependency.write_text("after")
    entered, release = threading.Event(), threading.Event()
    original = runtime.copy_dependency_tree

    def blocking(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "copy_dependency_tree", blocking)
    errors: list[BaseException] = []

    def refresh() -> None:
        try:
            isolate.runtime.refresh(workspace.workspace_id)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=refresh)
    thread.start()
    assert entered.wait(5)
    # A different session can publish while this runtime tree is copying.
    assert env.publisher.lock.acquire(timeout=1)
    try:
        with isolate.runtime.activity(workspace.workspace_id):
            release.set()
        # Releasing the publisher lock lets installation recheck activity.
    finally:
        env.publisher.lock.release()
        thread.join(5)
    assert not thread.is_alive() and not errors
    isolate.runtime.refresh(workspace.workspace_id)
    assert (private / "node_modules/pkg/module.js").read_text() == "after"


def test_live_prepare_error_recovers_before_next_command_activity(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    isolate, workspace, private = prepare(env, tmp_path)
    (env.repository / "a.local").write_text("after a\n")
    (env.repository / "b.local").write_text("after b\n")

    def fail(stage: str) -> None:
        if stage == "runtime_installed:a.local":
            raise OSError("simulated interrupted rename")

    isolate.runtime.fault = fail
    assert "deferred" in isolate.runtime.refresh(workspace.workspace_id)[0]
    assert (private / "a.local").read_text() == "after a\n"
    assert (private / "b.local").read_text() == "before b\n"
    isolate.runtime.fault = lambda stage: None
    with isolate.runtime.activity(workspace.workspace_id):
        assert (private / "a.local").read_text() == "after a\n"
        assert (private / "b.local").read_text() == "after b\n"
        isolate.runtime.recover(workspace.workspace_id)
        with env.store.connection() as con:
            assert (
                con.execute("SELECT count(*) FROM isolated_runtime_journal").fetchone()[
                    0
                ]
                == 0
            )


def test_runtime_conflict_notice_persists_when_another_path_refreshes(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    isolate, workspace, private = prepare(env, tmp_path)
    (private / "a.local").write_text("private a\n")
    (env.repository / "a.local").write_text("primary a\n")
    (env.repository / "b.local").write_text("primary b\n")
    assert isolate.runtime.refresh(workspace.workspace_id) == (
        "Runtime conflict; preserved private copy: a.local",
        "Runtime refreshed: b.local",
    )
    assert (private / "a.local").read_text() == "private a\n"
    assert (private / "b.local").read_text() == "primary b\n"
    assert isolate.runtime.refresh(workspace.workspace_id) == ()
    assert manager(env, tmp_path).runtime.refresh(workspace.workspace_id) == ()


@pytest.mark.parametrize("abort", ["busy", "generation"])
def test_runtime_notice_persists_when_staging_is_aborted(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    abort: str,
) -> None:
    from llm_cli.workspace import isolated

    env = batch_env
    isolate, workspace, private = prepare(env, tmp_path)
    (private / "a.local").write_text("private a\n")
    (env.repository / "a.local").write_text("primary a\n")
    (env.repository / "b.local").write_text("primary b\n")
    original = isolated.copy_runtime_file
    activity = isolate.runtime.activity(workspace.workspace_id)

    def copy(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        if abort == "busy":
            activity.__enter__()
        else:
            with env.store.connection() as con:
                con.execute(
                    "UPDATE isolated_runtime_state SET generation=generation+1 "
                    "WHERE workspace_id=?",
                    (workspace.workspace_id,),
                )

    monkeypatch.setattr(isolated, "copy_runtime_file", copy)
    try:
        assert isolate.runtime.refresh(workspace.workspace_id) == (
            "Runtime conflict; preserved private copy: a.local",
        )
    finally:
        if abort == "busy":
            activity.__exit__(None, None, None)
    assert (private / "b.local").read_text() == "before b\n"
    monkeypatch.setattr(isolated, "copy_runtime_file", original)
    assert isolate.runtime.refresh(workspace.workspace_id) == (
        "Runtime refreshed: b.local",
    )
    assert isolate.runtime.refresh(workspace.workspace_id) == ()
