"""Barrier failures retain the evidence needed to separate stalls from errors."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_cli_process_orchestration import _Cluster
from test_optimistic_isolated_sessions import _wait_prepared


@pytest.mark.parametrize("registered", [False, True])
def test_cli_barrier_reports_exited_child_and_persisted_failure(
    tmp_path: Path, registered: bool
) -> None:
    cluster = object.__new__(_Cluster)
    cluster.root = tmp_path
    cluster.signals = tmp_path / "signals"
    cluster.signals.mkdir()
    database = tmp_path / "control.sqlite3"
    with closing(sqlite3.connect(database)) as con:
        con.executescript(
            "CREATE TABLE tasks(task_id,title,state,failure_code,created_at);"
            "INSERT INTO tasks VALUES "
            "('worker-task','label','failed','PROVIDER_ERROR',1);"
            "CREATE TABLE task_events(task_id,sequence,event_type,payload_json);"
            "INSERT INTO task_events VALUES ('worker-task',1,'execution.failed','{}');"
        )
    cluster.paths = SimpleNamespace(control_db=database)
    process = SimpleNamespace(pid=17, poll=lambda: 7)
    cluster.daemon = SimpleNamespace(poll=lambda: None)
    cluster.processes = [process]
    cluster.process_names = {17: "worker"} if registered else {}
    cluster.submissions = {"unready": process} if registered else {}
    (tmp_path / "worker.log").write_text(
        "old log head\n" + "x" * 9_000 + "provider refused"
    )
    before = database.read_bytes()
    with pytest.raises(AssertionError) as failed:
        cluster.ready_all(("unready",), timeout=0)
    message = str(failed.value)
    assert all(
        item in message
        for item in (
            "unready",
            '"exit": 7',
            "provider refused",
            "PROVIDER_ERROR",
            "execution.failed",
        )
    )
    assert "old log head" not in message
    assert database.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "control.sqlite3",
        "signals",
        "worker.log",
    ]


def test_cli_cleanup_releases_submissions_without_ready_markers(tmp_path: Path) -> None:
    cluster = object.__new__(_Cluster)
    cluster.signals = tmp_path
    cluster.submissions = {"not-yet-ready": SimpleNamespace()}
    cluster.processes = [
        SimpleNamespace(poll=lambda: 0, wait=lambda **kwargs: 0, stdin=None)
    ]
    cluster.stack = ExitStack()
    # Controlling-terminal tests bypass submit() and announce their own label.
    (tmp_path / "direct-terminal.ready.json").write_text("{}")
    cluster.close()
    assert (tmp_path / "not-yet-ready.release").is_file()
    assert (tmp_path / "direct-terminal.release").is_file()


def test_preparation_barrier_reports_original_background_error() -> None:
    async def scenario() -> None:
        async def failure() -> None:
            raise RuntimeError("provider refused the request")

        background = asyncio.create_task(failure())
        await asyncio.sleep(0)
        service = SimpleNamespace(
            store=SimpleNamespace(
                get_task=lambda task: {"task_id": task, "state": "failed"},
                list_task_events=lambda task: ["execution.failed: provider refused"],
            )
        )
        with pytest.raises(AssertionError) as failed:
            await _wait_prepared(service, {"worker": threading.Event()}, [background])
        assert "provider refused the request" in str(failed.value)
        assert "execution.failed" in str(failed.value)

    asyncio.run(scenario())
