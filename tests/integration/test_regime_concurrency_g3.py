"""Mode changes and physical authority keep one compatible lock order."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from llm_cli.git.inspect import inspect_repository
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.authority import PhysicalAuthority


@pytest.mark.parametrize("operation", ["pin", "marker"])
def test_session_mode_and_authority_operation_complete_without_sqlite_timeout(
    operation: str,
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = repository_factory(tmp_path, {"README.md": "base\n"})
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    store = ControlStore(profile / "control.sqlite3", timeout_seconds=0.5)
    store.initialize()
    info = inspect_repository(repository, profile_id="test")
    registered = store.register_repository(
        repo_key=info.repo_key,
        display_name="regime concurrency",
        git_common_dir=str(info.common_git_dir),
        main_worktree_path=str(info.main_worktree),
        target_ref=info.target_ref,
        profile_id="test",
    )
    checkout = store.ensure_checkout(
        repository=registered,
        canonical_path=str(repository.resolve()),
        git_common_dir=str(info.common_git_dir),
    )
    workspace = store.ensure_shared_workspace(checkout)
    authority = PhysicalAuthority(
        store,
        "test",
        "regime-concurrency-boot",
        tmp_path / "profile" / "socket",
        root=tmp_path / "physical-authority" / "v1",
    )
    store.authority = authority
    retained = authority.acquire(repository)
    _, _, sequence = store.open_session(
        session_id="regime-concurrency-session",
        checkout_id=checkout.checkout_id,
        resume_token_hash=hashlib.sha256(b"regime-concurrency-fixture").hexdigest(),
        provider="scripted",
        model="fixture",
        workspace_id=workspace.workspace_id,
    )
    store.acknowledge_session("regime-concurrency-session", sequence=sequence)

    writer_at_regime = threading.Event()
    authority_at_sql = threading.Event()
    writer_entering_regime = threading.Event()
    events: list[tuple[str, str]] = []
    results: dict[str, Any] = {}
    record_lock = threading.Lock()
    writer_name = "g3-session-mode-writer"
    acquirer_name = "g3-authority-" + operation
    actual_connect = store.connect
    actual_regime = authority.regime

    def record(event: str) -> None:
        with record_lock:
            events.append((threading.current_thread().name, event))

    def traced_connect() -> sqlite3.Connection:
        connection = actual_connect()

        def trace(statement: str) -> None:
            sql = " ".join(statement.upper().split())
            if sql.startswith(("BEGIN", "COMMIT", "ROLLBACK")):
                record(sql)
            if threading.current_thread().name == acquirer_name and (
                (operation == "pin" and sql == "BEGIN IMMEDIATE")
                or (
                    operation == "marker"
                    and sql.startswith(
                        "UPDATE AUTHORITY_CHECKOUTS SET MARKER_GENERATION="
                    )
                )
            ):
                # The pin's installation validation begins a real write
                # transaction. A marker's real UPDATE is its competing write.
                # Both traces run while their authority operation owns _lock,
                # immediately before SQLite tries to acquire its writer lock.
                record("authority-write-entered")
                authority_at_sql.set()

        connection.set_trace_callback(trace)
        return connection

    def gated_regime(root: Path) -> dict[str, Any]:
        if threading.current_thread().name == writer_name:
            record("writer-at-regime")
            writer_at_regime.set()
            if not authority_at_sql.wait(5):
                raise AssertionError("authority operation did not reach its SQL write")
            record("writer-entering-regime")
            writer_entering_regime.set()
        return actual_regime(root)

    monkeypatch.setattr(store, "connect", traced_connect)
    monkeypatch.setattr(authority, "regime", gated_regime)

    def change_mode() -> None:
        try:
            session = store.set_session_mode("regime-concurrency-session", "normal")
            results["writer"] = session.agent_mode
        except BaseException as error:
            results["writer"] = error

    def use_authority() -> None:
        try:
            if operation == "pin":
                with authority.pin(repository):
                    pass
            else:
                authority._write_marker(retained.owner, "clean")
            results["authority"] = "completed"
        except BaseException as error:
            results["authority"] = error

    writer = threading.Thread(target=change_mode, name=writer_name, daemon=True)
    acquirer = threading.Thread(target=use_authority, name=acquirer_name, daemon=True)
    threads = (writer, acquirer)
    try:
        writer.start()
        assert writer_at_regime.wait(5), events
        acquirer.start()
        assert authority_at_sql.wait(5), events
        assert writer_entering_regime.wait(5), events
        deadline = time.monotonic() + 8
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        assert not any(thread.is_alive() for thread in threads), events
        assert results == {"writer": "normal", "authority": "completed"}, (
            results,
            events,
        )
        assert store.get_session("regime-concurrency-session").agent_mode == "normal"
    finally:
        # A failed ordering assertion still releases our gate and waits for
        # the real threads before closing retained ownership or temporary DBs.
        authority_at_sql.set()
        deadline = time.monotonic() + 8
        for thread in threads:
            if thread.ident is not None:
                thread.join(max(0, deadline - time.monotonic()))
        live = [thread.name for thread in threads if thread.is_alive()]
        if not live:
            retained.close()
            authority.close()
        assert not live, ("concurrency workers did not terminate", live, events)
