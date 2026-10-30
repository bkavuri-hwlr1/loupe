"""Watchers cover bounded read registrations without changing publication bases."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from test_external_changes import _events, _read
from test_external_changes import checkout as checkout

from llm_cli.storage.control import ControlStore
from llm_cli.workspace import external
from llm_cli.workspace.watchers import FILE, Changes, Cursor, _Journal


class WatchedChanges:
    """Controlled, non-consuming evidence from the production watcher journal."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.journal = _Journal()
        self.polling = False

    def changes(self, root: Path, cursor: Cursor | None) -> Changes | None:
        assert root == self.root
        if self.polling:
            return None
        return self.journal.since(cursor, frozenset(), time.monotonic())


def test_watcher_notifies_first_of_five_thousand_reads_for_each_session(
    checkout: tuple[ControlStore, Path, str],
) -> None:
    store, root, _ = checkout
    paths = [f"file{number:05}.py" for number in range(5_000)]
    for path in paths:
        (root / path).write_text("opened source\n")
        _read(store, root, path, "reader")
        _read(store, root, path, "other")
    with store.connection() as con:
        base = tuple(
            con.execute(
                "SELECT * FROM workspace_read_observations "
                "WHERE session_id='reader' AND relative_path=?",
                (paths[0],),
            ).fetchone()
        )
    watch = WatchedChanges(root)
    scanner = external.ExternalWorkspaceScanner(store, watch)  # type: ignore[arg-type]
    assert scanner.scan_session("reader") == scanner.scan_session("other") == 0
    (root / paths[0]).write_text("outside change\n")
    watch.journal.record(paths[0], FILE, False)

    assert scanner.scan_session("reader") == 1
    assert scanner.scan_session("other") == 1

    assert _events(store) == [
        {"path": paths[0], "affected_session_id": "reader"},
        {"path": paths[0], "affected_session_id": "other"},
    ]
    with store.connection() as con:
        assert (
            tuple(
                con.execute(
                    "SELECT * FROM workspace_read_observations "
                    "WHERE session_id='reader' AND relative_path=?",
                    (paths[0],),
                ).fetchone()
            )
            == base
        )


def test_watcher_count_budget_prioritizes_changes_and_keeps_retry_cursor(
    checkout: tuple[ControlStore, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, _ = checkout
    monkeypatch.setattr(external, "MAX_PATHS", 2)
    for number in range(6):
        path = f"file{number}"
        (root / path).write_text("opened\n")
        _read(store, root, path)
    watch = WatchedChanges(root)
    scanner = external.ExternalWorkspaceScanner(store, watch)  # type: ignore[arg-type]
    scanner.scan_session("reader")
    for number in range(3):
        path = f"file{number}"
        (root / path).write_text("changed\n")
        watch.journal.record(path, FILE, False)

    assert scanner.scan_session("reader") == 2
    assert scanner.scan_session("reader") == 1
    assert {item["path"] for item in _events(store)} == {"file0", "file1", "file2"}


@pytest.mark.parametrize("polling", [False, True])
def test_fallback_keeps_latest_read_window_and_reports_coverage_once(
    checkout: tuple[ControlStore, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    polling: bool,
) -> None:
    import json

    from llm_cli.workspace.context import SharedCoordinationContext

    store, root, _ = checkout
    monkeypatch.setattr(external, "MAX_PATHS", 2)
    for number in range(4):
        path = f"file{number}"
        (root / path).write_text("opened\n")
        _read(store, root, path)
        with store.connection() as con:
            con.execute(
                "UPDATE workspace_read_observations SET observed_at=? "
                "WHERE session_id='reader' AND relative_path=?",
                (number, path),
            )
    watch = WatchedChanges(root)
    scanner = external.ExternalWorkspaceScanner(store, watch)  # type: ignore[arg-type]
    while True:
        scanner.scan_session("reader")
        if not scanner._watched["reader"].retry:
            break
    watch.polling = polling
    if not polling:
        watch.journal.collapse("forced overflow")
    (root / "file0").write_text("outside old\n")
    (root / "file3").write_text("outside recent\n")
    context = SharedCoordinationContext(store, "reader", ("*",), scanner=scanner)

    update = context(None)

    assert [event["path"] for event in _events(store)] == ["file3"]
    notice = json.loads(update.text or "{}")
    assert "latest 2 read paths" in notice["external_coverage"]
    assert "external_changes" not in notice
    later = context(update.sequence)
    if polling:
        assert later.text is None
    if not polling:
        # The next healthy cursor must revisit older registered reads whose
        # events were lost; the full-scan warning cannot simply disappear.
        for _ in range(3):
            scanner.scan_session("reader")
        assert {event["path"] for event in _events(store)} == {"file0", "file3"}


def _registration_cap(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    from llm_cli.workspace import external_reads

    monkeypatch.setattr(external_reads, "MAX_READ_REGISTRATIONS", count)
    monkeypatch.setattr(external, "MAX_READ_REGISTRATIONS", count)


def _registered(store: ControlStore) -> set[str]:
    with store.connection() as con:
        return {
            str(row[0])
            for row in con.execute(
                "SELECT relative_path FROM workspace_external_observations "
                "WHERE session_id='reader'"
            )
        }


def test_advisory_registration_cap_never_retires_canonical_publication_bases(
    checkout: tuple[ControlStore, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, _ = checkout
    _registration_cap(monkeypatch, 3)
    for path in ("a", "b", "c", "d"):
        (root / path).write_text("opened\n")
    for path in ("a", "b", "c"):
        _read(store, root, path)
    with store.connection() as con:
        canonical = {
            str(row["relative_path"]): tuple(row)
            for row in con.execute("SELECT * FROM workspace_read_observations")
        }
    for _ in range(5):
        _read(store, root, "c")
    assert _registered(store) == {"a", "b", "c"}

    _read(store, root, "d")

    assert _registered(store) == {"b", "c", "d"}
    with store.connection() as con:
        assert (
            tuple(
                con.execute(
                    "SELECT * FROM workspace_read_observations WHERE relative_path='a'"
                ).fetchone()
            )
            == canonical["a"]
        )
        assert (
            con.execute("SELECT count(*) FROM workspace_read_observations").fetchone()[
                0
            ]
            == 4
        )
    _read(store, root, "a")
    assert _registered(store) == {"a", "c", "d"}


def test_registration_overflow_uses_only_targeted_delete_and_existing_selects(
    checkout: tuple[ControlStore, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, _ = checkout
    _registration_cap(monkeypatch, 3)
    for path in ("a", "b", "c", "d"):
        (root / path).write_text("opened\n")
    for path in ("a", "b", "c"):
        _read(store, root, path)
    statements: list[str] = []
    connect = store.connect

    def trace() -> Any:
        connection = connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, "connect", trace)

    _read(store, root, "d")

    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 3  # Session, workspace, and original metadata binding.
    deletes = [sql for sql in statements if sql.lstrip().upper().startswith("DELETE")]
    assert len(deletes) == 1
    assert "session_id='reader' AND relative_path='a'" in deletes[0]
    assert not any(
        "COUNT(" in sql.upper() or "ORDER BY" in sql.upper() for sql in selects
    )


def test_failed_registration_does_not_advance_cache_or_retire_a_base(
    checkout: tuple[ControlStore, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, _ = checkout
    _registration_cap(monkeypatch, 3)
    for path in ("a", "b", "c", "d", "e"):
        (root / path).write_text("opened\n")
    for path in ("a", "b", "c"):
        _read(store, root, path)
    register = external.register_read

    def fail(*args: Any, **kwargs: Any) -> None:
        register(*args, **kwargs)
        raise RuntimeError("registration rolled back")

    monkeypatch.setattr(external, "register_read", fail)
    with pytest.raises(RuntimeError, match="registration rolled back"):
        _read(store, root, "d")
    assert _registered(store) == {"a", "b", "c"}
    monkeypatch.setattr(external, "register_read", register)
    _read(store, root, "e")
    assert _registered(store) == {"b", "c", "e"}
    with store.connection() as con:
        assert (
            con.execute(
                "SELECT 1 FROM workspace_read_observations WHERE relative_path='d'"
            ).fetchone()
            is None
        )


def test_shared_store_instances_keep_registration_bound_under_concurrent_reads(
    checkout: tuple[ControlStore, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    store, root, _ = checkout
    _registration_cap(monkeypatch, 5)
    other = ControlStore(store.path)
    other.initialize()
    paths = [f"file{number}" for number in range(20)]
    for path in paths:
        (root / path).write_text("opened\n")
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(_read, store if number % 2 else other, root, path)
            for number, path in enumerate(paths)
        ]
        for future in futures:
            future.result(timeout=10)
    assert len(_registered(store)) == 5
    with store.connection() as con:
        assert (
            con.execute("SELECT count(*) FROM workspace_read_observations").fetchone()[
                0
            ]
            == 20
        )


def test_restart_trims_legacy_advisory_rows_and_preserves_all_bases(
    checkout: tuple[ControlStore, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import sys

    store, root, _ = checkout
    _registration_cap(monkeypatch, 3)
    for number in range(8):
        path = f"file{number}"
        (root / path).write_text("opened\n")
        _read(store, root, path)
    with store.connection() as con:
        before = [
            tuple(row)
            for row in con.execute(
                "SELECT * FROM workspace_read_observations ORDER BY relative_path"
            )
        ]
        con.execute(
            "INSERT OR IGNORE INTO workspace_external_observations "
            "SELECT session_id,relative_path,1,NULL,NULL,0 "
            "FROM workspace_read_observations"
        )
    assert len(_registered(store)) == 8
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from llm_cli.workspace import external_reads; "
            "external_reads.MAX_READ_REGISTRATIONS=3; "
            "from llm_cli.storage.control import ControlStore; "
            "ControlStore(sys.argv[1]).initialize()",
            str(store.path),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    assert len(_registered(store)) == 3
    with store.connection() as con:
        assert [
            tuple(row)
            for row in con.execute(
                "SELECT * FROM workspace_read_observations ORDER BY relative_path"
            )
        ] == before


def test_closed_session_releases_only_its_advisory_memory_cache(
    checkout: tuple[ControlStore, Path, str],
) -> None:
    store, root, _ = checkout
    (root / "a").write_text("opened\n")
    _read(store, root, "a", "reader")
    _read(store, root, "a", "other")
    registry = store._external_reads
    assert registry is not None
    assert set(registry.sessions) == {"reader", "other"}

    store.close_session("reader")

    assert set(registry.sessions) == {"other"}
    with store.connection() as con:
        assert (
            con.execute(
                "SELECT 1 FROM workspace_read_observations "
                "WHERE session_id='reader' AND relative_path='a'"
            ).fetchone()
            is not None
        )


def test_workspace_binding_generation_resets_only_advisory_scope(
    checkout: tuple[ControlStore, Path, str],
) -> None:
    store, root, _ = checkout
    for path in ("a", "b"):
        (root / path).write_text("opened\n")
    _read(store, root, "a", "reader")
    _read(store, root, "a", "other")
    with store.connection() as con:
        # A round trip through C13 clears the old model read registrations,
        # while shared mode can return to the same shared workspace ID.
        con.execute("DELETE FROM workspace_read_observations WHERE session_id='reader'")
        con.execute(
            "UPDATE sessions SET workspace_binding_generation="
            "workspace_binding_generation+1 WHERE session_id='reader'"
        )
        other = tuple(
            con.execute(
                "SELECT * FROM workspace_read_observations WHERE session_id='other'"
            ).fetchone()
        )

    _read(store, root, "b", "reader")

    assert _registered(store) == {"b"}
    with store.connection() as con:
        assert (
            tuple(
                con.execute(
                    "SELECT * FROM workspace_read_observations WHERE session_id='other'"
                ).fetchone()
            )
            == other
        )


def test_native_registration_cap_notice_is_advisory_and_deduplicated(
    checkout: tuple[ControlStore, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from llm_cli.workspace.context import SharedCoordinationContext

    store, root, _ = checkout
    _registration_cap(monkeypatch, 3)
    for number in range(4):
        path = f"file{number}"
        (root / path).write_text("opened\n")
        _read(store, root, path)
    watch = WatchedChanges(root)
    scanner = external.ExternalWorkspaceScanner(store, watch)  # type: ignore[arg-type]
    context = SharedCoordinationContext(store, "reader", ("*",), scanner=scanner)

    first = context(None)

    packet = json.loads(first.text or "{}")
    assert "at most 3 read paths" in packet["external_coverage"]
    assert "external_changes" not in packet
    assert context(first.sequence).text is None


def test_native_read_query_never_walks_unbounded_canonical_history(
    checkout: tuple[ControlStore, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, workspace_id = checkout
    _registration_cap(monkeypatch, 3)
    (root / "kept").write_text("opened\n")
    _read(store, root, "kept")
    with store.connection() as connection:
        # Canonical C5 publication bases deliberately outlive the advisory LRU.
        connection.execute(
            "WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL "
            "SELECT n+1 FROM numbers WHERE n<20000) "
            "INSERT INTO workspace_read_observations "
            "SELECT session_id,workspace_id,'historical/'||n,observed_kind,"
            "observed_mode,observed_digest,observed_at "
            "FROM workspace_read_observations,"
            "numbers WHERE session_id='reader' AND relative_path='kept'"
        )
    statements: list[str] = []
    connect = store.connect

    def trace() -> Any:
        connection = connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, "connect", trace)
    scanner = external.ExternalWorkspaceScanner(
        store,
        WatchedChanges(root),  # type: ignore[arg-type]
    )
    scanner.scan_session("reader")
    queries = [sql for sql in statements if sql.startswith("SELECT r.*, x.generation")]
    assert len(queries) == 1
    with store.connection() as connection:
        plan = [
            row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + queries[0])
        ]
        assert plan[0].startswith("SEARCH x "), plan
        assert "session_id=?" in plan[0]
        assert plan[1].startswith("SEARCH r "), plan
        assert "session_id=? AND relative_path=?" in plan[1]
        assert (
            connection.execute(
                "SELECT count(*) FROM workspace_read_observations "
                "WHERE session_id='reader' AND workspace_id=?",
                (workspace_id,),
            ).fetchone()[0]
            == 20_001
        )
    assert _registered(store) == {"kept"}
