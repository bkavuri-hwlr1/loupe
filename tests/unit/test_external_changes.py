"""External scans observe net changes without granting publication authority."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.errors import LlmCoordError
from llm_cli.storage.control import ControlStore
from llm_cli.workspace import external
from llm_cli.workspace.batches import BatchFile, SharedBatchPublisher
from llm_cli.workspace.context import SharedCoordinationContext
from llm_cli.workspace.identity import REGULAR_MODE, FileIdentity, read_identified_path


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[ControlStore, Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", "--template="], cwd=root, check=True)
    store = ControlStore(tmp_path / "db")
    store.initialize()
    repo = store.register_repository(
        repo_key="a" * 64,
        display_name="fixture",
        git_common_dir=str(root / ".git"),
        main_worktree_path=str(root),
        target_ref="refs/heads/main",
    )
    check = store.ensure_checkout(
        repository=repo, canonical_path=str(root), git_common_dir=repo.git_common_dir
    )
    workspace = store.ensure_shared_workspace(check)
    for session in ("reader", "other"):
        _, _, opened = store.open_session(
            session_id=session,
            checkout_id=check.checkout_id,
            workspace_id=workspace.workspace_id,
            resume_token_hash=hashlib.sha256(session.encode()).hexdigest(),
            provider="fixture",
            model="fixture",
        )
        store.acknowledge_session(session, sequence=opened)
    return store, root, workspace.workspace_id


def _read(store: ControlStore, root: Path, path: str, session: str = "reader") -> None:
    identity, _ = read_identified_path(root / path)
    store.record_workspace_read(
        session_id=session, relative_path=path, identity=identity
    )


def _events(store: ControlStore) -> list[dict[str, Any]]:
    with store.connection() as conn:
        return [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload_json FROM checkout_events WHERE event_type=?",
                (external.EVENT_TYPE,),
            )
        ]


def test_next_boundary_detects_edit_and_leaves_publication_base_unchanged(
    checkout: tuple[ControlStore, Path, str],
) -> None:
    store, root, _ = checkout
    (root / "a.py").write_text("old")
    _read(store, root, "a.py")
    context = SharedCoordinationContext(store, "reader", ("a.py",))
    first = context(None)
    with store.connection() as conn:
        before = tuple(
            conn.execute("SELECT * FROM workspace_read_observations").fetchone()
        )
    (root / "a.py").write_text("new")
    update = context(first.sequence)
    assert update.text
    assert json.loads(update.text)["changes"][0]["paths"] == [
        {"path": "a.py", "overlaps_scope": True}
    ]
    assert "new" not in update.text
    assert _events(store) == [{"path": "a.py", "affected_session_id": "reader"}]
    with store.connection() as conn:
        assert (
            tuple(conn.execute("SELECT * FROM workspace_read_observations").fetchone())
            == before
        )


def test_unread_session_is_not_interrupted(
    checkout: tuple[ControlStore, Path, str],
) -> None:
    store, root, _ = checkout
    (root / "a.py").write_text("old")
    _read(store, root, "a.py")
    context = SharedCoordinationContext(store, "other", ("*",))
    first = context(None)
    (root / "a.py").write_text("new")
    assert external.ExternalWorkspaceScanner(store).scan_active() == 1
    assert context(first.sequence).text is None


@pytest.mark.parametrize("path", [".env", "cache/runtime.txt", "ordinary.txt"])
def test_opaque_changes_never_persist_bytes_or_new_hashes(
    checkout: tuple[ControlStore, Path, str], monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    store, root, _ = checkout
    (root / ".gitignore").write_text("cache/\n")
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("before")
    _read(store, root, path)
    scanner = external.ExternalWorkspaceScanner(store)
    scanner.scan_session("reader")
    secret = "sk-" + "Q" * 40
    target.write_text(secret)
    if path != "ordinary.txt":
        monkeypatch.setattr(
            external, "_source", lambda *args: pytest.fail("excluded bytes opened")
        )
    assert scanner.scan_session("reader") == 1
    assert _events(store) == [{"path": path, "affected_session_id": "reader"}]
    with store.connection() as conn:
        row = conn.execute(
            "SELECT identity_json FROM workspace_external_observations"
        ).fetchone()
        assert row[0] == "opaque"
    assert secret not in json.dumps(_events(store))


def test_unchanged_metadata_skips_content_and_restarts_deduplicate(
    checkout: tuple[ControlStore, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, _ = checkout
    (root / "a").write_text("old")
    _read(store, root, "a")
    scanner = external.ExternalWorkspaceScanner(store)
    scanner.scan_session("reader")
    (root / "a").write_text("new")
    assert scanner.scan_session("reader") == 1
    monkeypatch.setattr(
        external, "_source", lambda *args: pytest.fail("unchanged file read")
    )
    assert external.ExternalWorkspaceScanner(store).scan_session("reader") == 0
    assert len(_events(store)) == 1


def test_metadata_touch_with_identical_content_has_no_event(
    checkout: tuple[ControlStore, Path, str],
) -> None:
    store, root, _ = checkout
    (root / "a").write_text("same")
    _read(store, root, "a")
    scanner = external.ExternalWorkspaceScanner(store)
    scanner.scan_session("reader")
    (root / "a").write_text("same")
    assert scanner.scan_session("reader") == 0


@pytest.mark.parametrize(
    "kind", ["delete", "replace", "chmod", "symlink", "fifo", "parent_symlink"]
)
def test_filesystem_changes_and_no_follow(
    checkout: tuple[ControlStore, Path, str], kind: str
) -> None:
    store, root, _ = checkout
    target = root / "dir/a"
    target.parent.mkdir()
    target.write_text("old")
    _read(store, root, "dir/a")
    scanner = external.ExternalWorkspaceScanner(store)
    scanner.scan_session("reader")
    if kind == "chmod":
        target.chmod(0o755)
    elif kind == "parent_symlink":
        target.parent.rename(root / "elsewhere")
        target.parent.symlink_to(root / "elsewhere", target_is_directory=True)
    else:
        target.unlink()
        if kind == "replace":
            target.write_text("new")
        elif kind == "symlink":
            target.symlink_to("/etc/passwd")
        elif kind == "fifo":
            os.mkfifo(target)
    assert scanner.scan_session("reader") == 1
    assert _events(store) == [{"path": "dir/a", "affected_session_id": "reader"}]


def test_offline_edit_and_new_read_do_not_duplicate_notices(
    checkout: tuple[ControlStore, Path, str],
) -> None:
    store, root, _ = checkout
    (root / "a").write_text("old")
    _read(store, root, "a")
    (root / "a").write_text("new")
    assert external.ExternalWorkspaceScanner(store).scan_session("reader") == 1
    _read(store, root, "a")
    assert external.ExternalWorkspaceScanner(store).scan_session("reader") == 0


def test_budget_failure_does_not_advance_cursor_or_record_partial_scan(
    checkout: tuple[ControlStore, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, _ = checkout
    (root / "a").write_text("old")
    _read(store, root, "a")
    context = SharedCoordinationContext(store, "reader", ("*",))
    initial = context(None)
    (root / "a").write_text("new")
    monkeypatch.setattr(external, "MAX_BYTES", 1)
    with pytest.raises(LlmCoordError, match="byte budget"):
        context(initial.sequence)
    assert not _events(store)
    monkeypatch.setattr(external, "MAX_BYTES", 1_000)
    assert context(initial.sequence).text


def test_actual_batch_publication_is_not_reported_again_as_external(
    checkout: tuple[ControlStore, Path, str], tmp_path: Path
) -> None:
    store, root, _ = checkout
    (root / "a").write_text("old")
    _read(store, root, "a")
    scanner = external.ExternalWorkspaceScanner(store)
    scanner.scan_session("reader")
    base, _ = read_identified_path(root / "a")
    publisher = SharedBatchPublisher(store, tmp_path / "private", threading.RLock())
    publisher.publish(
        session_id="other",
        batch_id="batch-test",
        files=(BatchFile("a", base, b"new", REGULAR_MODE, original=b"old"),),
        scopes=("a",),
    )
    assert scanner.scan_session("reader") == 0
    assert not _events(store)
    (root / "a").write_text("outside")
    assert scanner.scan_session("reader") == 1
    (root / "a").write_text("new")
    assert scanner.scan_session("reader") == 1


def _journal(
    store: ControlStore, root: Path, workspace: str, requests: list[str]
) -> None:
    with store.connection() as conn:
        checkout = conn.execute(
            "SELECT checkout_id FROM workspaces WHERE workspace_id=?", (workspace,)
        ).fetchone()[0]
        head = conn.execute(
            "SELECT next_event_sequence FROM checkout_heads WHERE checkout_id=?",
            (checkout,),
        ).fetchone()[0]
        conn.executemany(
            "INSERT INTO "
            "workspace_batches(batch_id,session_id,workspace_id,checkout_id,"
            "canonical_path,git_common_dir,request_json,state,workspace_revision,"
            "checkout_event_sequence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,'published',1,?,1,1)",
            [
                (
                    f"history-{i}",
                    "other",
                    workspace,
                    checkout,
                    str(root),
                    str(root / ".git"),
                    request,
                    head + i + 1,
                )
                for i, request in enumerate(requests)
            ],
        )


@pytest.mark.parametrize("many", [False, True])
def test_history_limits_apply_before_unbounded_fetch_or_parse(
    checkout: tuple[ControlStore, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    many: bool,
) -> None:
    store, root, workspace = checkout
    (root / "a").write_text("old")
    _read(store, root, "a")
    identity, _ = read_identified_path(root / "a")
    if many:
        request = json.dumps(
            {
                "files": [
                    {
                        "relative_path": "unrelated",
                        "result": {
                            "kind": "regular",
                            "mode": REGULAR_MODE,
                            "digest": identity.digest,
                        },
                    }
                ]
            }
        )
        _journal(store, root, workspace, [request] * 1_001)
    else:
        _journal(
            store, root, workspace, ['{"unused":"' + "X" * (2 * 1024 * 1024) + '"}']
        )
    loads = json.loads

    def bounded(value: str) -> Any:
        assert len(value) <= 1_048_576, "oversized publication body fetched"
        return loads(value)

    monkeypatch.setattr(external.json, "loads", bounded)
    with pytest.raises(LlmCoordError, match="history exceeds"):
        external.ExternalWorkspaceScanner(store).scan_session("reader")
    assert not _events(store)
    with store.connection() as conn:
        assert (
            conn.execute(
                "SELECT identity_json FROM workspace_external_observations"
            ).fetchone()[0]
            is None
        )


def test_concurrent_real_read_wins_scan_generation_race(
    checkout: tuple[ControlStore, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, _ = checkout
    (root / "a").write_text("old")
    _read(store, root, "a")
    scanner = external.ExternalWorkspaceScanner(store)
    scanner.scan_session("reader")
    (root / "a").write_text("new")
    source = external._source

    def raced(*args: Any) -> Any:
        result = source(*args)
        _read(store, root, "a")
        return result

    monkeypatch.setattr(external, "_source", raced)
    assert scanner.scan_session("reader") == 0
    monkeypatch.setattr(external, "_source", source)
    assert scanner.scan_session("reader") == 0
    assert not _events(store)


def test_pending_publication_during_scan_never_records_external_change(
    checkout: tuple[ControlStore, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, workspace = checkout
    (root / "a").write_text("old")
    _read(store, root, "a")
    scanner = external.ExternalWorkspaceScanner(store)
    scanner.scan_session("reader")
    (root / "a").write_text("new")
    source = external._source

    def raced(*args: Any) -> Any:
        result = source(*args)
        _journal(store, root, workspace, ["{}"])
        with store.connection() as conn:
            conn.execute("UPDATE workspace_batches SET state='applying'")
        return result

    monkeypatch.setattr(external, "_source", raced)
    with pytest.raises(LlmCoordError, match="publication changed"):
        scanner.scan_session("reader")
    assert not _events(store)


def test_own_publication_uses_checkout_case_rules(
    checkout: tuple[ControlStore, Path, str], tmp_path: Path
) -> None:
    store, root, _ = checkout
    (root / "lower.py").write_text("old")
    if not (root / "LOWER.py").exists():
        pytest.skip("requires a case-insensitive filesystem")
    with store.connection() as conn:
        conn.execute("UPDATE checkouts SET path_case_insensitive=1")
    _read(store, root, "lower.py")
    scanner = external.ExternalWorkspaceScanner(store)
    scanner.scan_session("reader")
    base, _ = read_identified_path(root / "LOWER.py")
    publisher = SharedBatchPublisher(store, tmp_path / "private", threading.RLock())
    publisher.publish(
        session_id="other",
        batch_id="case-publication",
        files=(BatchFile("LOWER.py", base, b"new", REGULAR_MODE, original=b"old"),),
        scopes=("LOWER.py",),
        case_insensitive_filesystem=True,
    )
    assert scanner.scan_session("reader") == 0
    assert not _events(store)


def _tracked_broker(store: ControlStore, root: Path) -> SharedToolBroker:
    def record(path: str, identity: FileIdentity) -> None:
        store.record_workspace_read(
            session_id="reader", relative_path=path, identity=identity
        )

    return SharedToolBroker(root, ("*",), on_read=record)


def test_private_helper_replays_do_not_reset_publication_dedup(
    checkout: tuple[ControlStore, Path, str], tmp_path: Path
) -> None:
    store, root, _ = checkout
    (root / "a").write_text("old")
    broker = _tracked_broker(store, root)
    assert not broker.invoke("read_file", {"path": "a"}).is_error
    assert not broker.invoke("write_file", {"path": "a", "content": "pending"}).is_error
    helper = broker.read_only_view(broker.limits)
    scanner = external.ExternalWorkspaceScanner(store)
    assert scanner.scan_session("reader") == 0
    base, _ = read_identified_path(root / "a")
    publisher = SharedBatchPublisher(store, tmp_path / "private", threading.RLock())
    publisher.publish(
        session_id="other",
        batch_id="private-view-publication",
        files=(BatchFile("a", base, b"peer", REGULAR_MODE, original=b"old"),),
        scopes=("a",),
    )
    assert scanner.scan_session("reader") == 0
    for _ in range(2):
        assert helper.invoke("read_file", {"path": "a"}).content == "pending"
        assert not helper.invoke("read_diff", {}).is_error
        assert scanner.scan_session("reader") == 0
    assert not _events(store)


def test_private_helper_replays_do_not_duplicate_a_real_outside_edit(
    checkout: tuple[ControlStore, Path, str],
) -> None:
    store, root, _ = checkout
    (root / "a").write_text("old")
    broker = _tracked_broker(store, root)
    assert not broker.invoke("read_file", {"path": "a"}).is_error
    assert not broker.invoke("write_file", {"path": "a", "content": "pending"}).is_error
    helper = broker.read_only_view(broker.limits)
    scanner = external.ExternalWorkspaceScanner(store)
    assert scanner.scan_session("reader") == 0
    (root / "a").write_text("outside")
    assert scanner.scan_session("reader") == 1
    for _ in range(2):
        assert helper.invoke("read_file", {"path": "a"}).content == "pending"
        assert scanner.scan_session("reader") == 0
    assert len(_events(store)) == 1


def test_helper_absent_disk_read_observes_later_outside_creation(
    checkout: tuple[ControlStore, Path, str],
) -> None:
    store, root, _ = checkout
    broker = _tracked_broker(store, root)
    helper = broker.read_only_view(broker.limits)
    assert helper.invoke("read_file", {"path": "missing.py"}).is_error
    scanner = external.ExternalWorkspaceScanner(store)
    assert scanner.scan_session("reader") == 0
    (root / "missing.py").write_text("created outside")
    assert scanner.scan_session("reader") == 1
    assert _events(store) == [{"path": "missing.py", "affected_session_id": "reader"}]


def test_pinned_batch_cannot_hide_outside_changes(
    checkout: tuple[ControlStore, Path, str],
) -> None:
    store, root, _ = checkout
    (root / "a").write_text("old line\nsecond line\n")
    parent = _tracked_broker(store, root)
    helper = parent.read_only_view(parent.limits)
    partial: dict[str, object] = {"path": "a", "start_line": 1, "end_line": 1}
    assert "old line" in helper.invoke("read_file", partial).content
    scanner = external.ExternalWorkspaceScanner(store)
    assert scanner.scan_session("reader") == 0
    (root / "a").write_text("outside edit\n")
    for expected in (1, 0):
        outcomes = helper.invoke_reads(
            [("read_file", partial), ("read_file", {"path": "a"})]
        )
        assert all("old line" in outcome.content for outcome in outcomes)
        assert scanner.scan_session("reader") == expected
    assert len(_events(store)) == 1
    outcomes = helper.invoke_reads(
        [("read_file", {**partial, "refresh": True}), ("read_file", {"path": "a"})]
    )
    assert all("outside edit" in outcome.content for outcome in outcomes)
    assert scanner.scan_session("reader") == 0
