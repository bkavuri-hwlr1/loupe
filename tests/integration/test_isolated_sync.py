"""Isolated incoming batches retain edits and recover exact local transitions."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from llm_cli.storage.control import ControlStore
from llm_cli.workspace import isolated_sync
from llm_cli.workspace.batches import BatchFile, SharedBatchPublisher
from llm_cli.workspace.broker import (
    WorkspaceBrokerError,
    read_candidate_path,
    store_candidate_content,
)
from llm_cli.workspace.identity import REGULAR_MODE, identify_path, read_identified_path
from llm_cli.workspace.isolated_sync import IsolatedSynchronizer, Manifest, _entry


@dataclass
class Environment:
    primary: Path
    local: Path
    store: ControlStore
    publisher: SharedBatchPublisher
    sync: IsolatedSynchronizer
    workspace_id: str

    def publish(self, batch: str, changes: dict[str, bytes]) -> None:
        files = []
        for path, body in changes.items():
            base, original = read_identified_path(self.primary / path)
            files.append(BatchFile(path, base, body, REGULAR_MODE, original))
        result = self.publisher.publish(batch, "shared", files, ("*",))
        assert result.state == "published"


@pytest.fixture
def env(
    tmp_path: Path, repository_factory: Callable[[Path, dict[str, str]], Path]
) -> Environment:
    primary = repository_factory(
        tmp_path, {"a.txt": "first\nmiddle\nlast\n", "b.txt": "before\n"}
    )
    local = tmp_path / "isolated"
    local.mkdir()
    for path in ("a.txt", "b.txt"):
        (local / path).write_bytes((primary / path).read_bytes())
    store = ControlStore(tmp_path / "control.sqlite3")
    store.initialize()
    registered = store.register_repository(
        repo_key="b" * 64,
        display_name="isolated fixture",
        git_common_dir=str(primary / ".git"),
        main_worktree_path=str(primary),
        target_ref="refs/heads/main",
        path_case_insensitive=False,
    )
    checkout = store.ensure_checkout(
        repository=registered,
        canonical_path=str(primary),
        git_common_dir=str(primary / ".git"),
    )
    workspace = store.ensure_shared_workspace(checkout)
    store.open_session(
        session_id="shared",
        checkout_id=checkout.checkout_id,
        resume_token_hash=hashlib.sha256(b"shared").hexdigest(),
        provider="fake",
        model="fake",
        workspace_id=workspace.workspace_id,
    )
    with store.connection() as con:
        con.execute("UPDATE sessions SET state='active'")
        con.execute(
            "INSERT INTO workspaces(workspace_id,checkout_id,kind,mode,state,"
            "canonical_path,source_workspace_id,created_at,updated_at) "
            "VALUES('isolated',?,'linked_worktree','isolated','active',?,?,0,0)",
            (checkout.checkout_id, str(local), workspace.workspace_id),
        )
        sequence = int(
            con.execute(
                "SELECT next_event_sequence FROM checkout_heads WHERE checkout_id=?",
                (checkout.checkout_id,),
            ).fetchone()[0]
        )
    publisher = SharedBatchPublisher(store, tmp_path / "candidates", threading.RLock())

    def capture(root: Path) -> Manifest:
        manifest: Manifest = {}
        for path in root.rglob("*"):
            if (
                (path.is_file() or path.is_symlink())
                and ".git" not in path.parts
                and path.name != ".env"
            ):
                identity, body = read_candidate_path(path)
                assert body is not None
                manifest[path.relative_to(root).as_posix()] = _entry(
                    identity, store_candidate_content(publisher.candidate_dir, body)
                )
        return manifest

    sync = IsolatedSynchronizer(store, publisher, capture)
    sync.register("isolated", capture(primary), sequence, (".env",))
    return Environment(primary, local, store, publisher, sync, "isolated")


def test_incoming_clean_merge_keeps_local_edits_and_primary_untouched(
    env: Environment,
) -> None:
    (env.local / "a.txt").write_text("local\nmiddle\nlast\n")
    env.publish(
        "incoming", {"a.txt": b"first\nmiddle\nremote\n", "b.txt": b"remote b\n"}
    )
    result = env.sync.synchronize(env.workspace_id)
    assert result.merged == ("a.txt",)
    assert result.applied == ("b.txt",)
    assert (env.local / "a.txt").read_text() == "local\nmiddle\nremote\n"
    assert (env.primary / "a.txt").read_text() == "first\nmiddle\nremote\n"
    files = env.sync.publication_files(env.workspace_id, ())
    assert len(files) == 1
    assert files[0].original == b"first\nmiddle\nremote\n"
    assert files[0].content == b"local\nmiddle\nremote\n"
    assert env.sync.synchronize(env.workspace_id).applied == ()


def test_conflict_survives_restart_and_cannot_publish_over_received_change(
    env: Environment,
) -> None:
    (env.local / "a.txt").write_text("local\nmiddle\nlast\n")
    env.publish(
        "incoming", {"a.txt": b"remote\nmiddle\nlast\n", "b.txt": b"remote b\n"}
    )
    assert env.sync.synchronize(env.workspace_id).conflicted == ("a.txt",)
    assert (env.local / "a.txt").read_text() == "local\nmiddle\nlast\n"
    assert (env.local / "b.txt").read_text() == "remote b\n"
    restarted = IsolatedSynchronizer(env.store, env.publisher, env.sync.capture)
    restarted.synchronize(env.workspace_id)
    with pytest.raises(WorkspaceBrokerError, match="conflict remains unresolved"):
        restarted.publication_files(env.workspace_id, ())
    (env.local / "a.txt").write_text("both local and remote\nmiddle\nlast\n")
    resolved = restarted.publication_files(env.workspace_id, ())
    assert resolved[0].original == b"remote\nmiddle\nlast\n"
    assert b"both local and remote" in (resolved[0].content or b"")


@pytest.mark.parametrize("crash_after", [0, 1, 2])
def test_incoming_recovery_rolls_forward_without_duplicate_manifest_generation(
    env: Environment, monkeypatch: pytest.MonkeyPatch, crash_after: int
) -> None:
    env.publish("incoming", {"a.txt": b"new a\n", "b.txt": b"new b\n"})
    original = isolated_sync.atomic_replace_regular_file
    calls = 0

    def crash(**kwargs: object) -> None:
        nonlocal calls
        if calls == crash_after:
            raise RuntimeError("daemon terminated")
        original(**kwargs)  # type: ignore[arg-type]
        calls += 1
        if calls == crash_after:
            raise RuntimeError("daemon terminated")

    monkeypatch.setattr(isolated_sync, "atomic_replace_regular_file", crash)
    with pytest.raises(RuntimeError, match="terminated"):
        env.sync.synchronize(env.workspace_id)
    monkeypatch.setattr(isolated_sync, "atomic_replace_regular_file", original)
    restarted = IsolatedSynchronizer(env.store, env.publisher, env.sync.capture)
    restarted.synchronize(env.workspace_id)
    assert (env.local / "a.txt").read_bytes() == b"new a\n"
    assert (env.local / "b.txt").read_bytes() == b"new b\n"
    with env.store.connection() as con:
        assert (
            con.execute("SELECT generation FROM isolated_sync_state").fetchone()[0] == 1
        )
        assert (
            con.execute(
                "SELECT count(*) FROM isolated_sync_journal WHERE state='applied'"
            ).fetchone()[0]
            == 1
        )


def test_recovery_preserves_unrecognized_local_write(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.publish("incoming", {"a.txt": b"new a\n", "b.txt": b"new b\n"})
    original = isolated_sync.atomic_replace_regular_file

    def crash(**kwargs: object) -> None:
        raise RuntimeError("daemon terminated")

    monkeypatch.setattr(isolated_sync, "atomic_replace_regular_file", crash)
    with pytest.raises(RuntimeError):
        env.sync.synchronize(env.workspace_id)
    monkeypatch.setattr(isolated_sync, "atomic_replace_regular_file", original)
    (env.local / "a.txt").write_text("user recovered this file\n")
    with pytest.raises(WorkspaceBrokerError, match="unrecognized local state"):
        env.sync.synchronize(env.workspace_id)
    assert (env.local / "a.txt").read_text() == "user recovered this file\n"
    assert (env.local / "b.txt").read_text() == "before\n"


def test_runtime_is_not_published_even_after_ignore_changes(env: Environment) -> None:
    (env.local / ".env").write_text("PRIVATE=value\n")
    base = identify_path(env.local / ".env")
    with pytest.raises(WorkspaceBrokerError, match="runtime data"):
        env.sync.publication_files(
            env.workspace_id, (BatchFile(".env", base, b"OTHER=value\n", REGULAR_MODE),)
        )
    assert env.sync.publication_files(env.workspace_id, ()) == ()
    assert not (env.primary / ".env").exists()


def test_freeze_combines_physical_edits_and_overlay(env: Environment) -> None:
    (env.local / "a.txt").write_text("physical\nmiddle\nlast\n")
    identity, body = read_identified_path(env.local / "a.txt")
    overlay = BatchFile(
        "a.txt", identity, b"physical\nmodel\nlast\n", REGULAR_MODE, body
    )
    files = env.sync.publication_files(env.workspace_id, (overlay,))
    assert files[0].content == b"physical\nmodel\nlast\n"
    assert files[0].original == b"first\nmiddle\nlast\n"
    assert files[0].base == identify_path(env.primary / "a.txt")
    assert (env.primary / "a.txt").read_text() == "first\nmiddle\nlast\n"


def test_outside_source_changes_reconcile_without_a_batch(env: Environment) -> None:
    (env.local / "a.txt").write_text("local\nmiddle\nlast\n")
    (env.primary / "a.txt").write_text("first\nmiddle\nremote\n")
    (env.primary / "new").mkdir()
    (env.primary / "new" / "file.txt").write_text("new source\n")
    report = env.sync.synchronize(env.workspace_id)
    assert report.merged == ("a.txt",)
    assert (env.local / "new" / "file.txt").read_text() == "new source\n"
    assert (env.local / "a.txt").read_text() == "local\nmiddle\nremote\n"
    assert [
        file.relative_path for file in env.sync.publication_files(env.workspace_id, ())
    ] == ["a.txt"]


def test_binary_flag_prevents_text_merge_and_survives_freezing(
    env: Environment,
) -> None:
    (env.local / "a.txt").write_text("local\nmiddle\nlast\n")
    base, original = read_identified_path(env.primary / "a.txt")
    assert (
        env.publisher.publish(
            "binary",
            "shared",
            (
                BatchFile(
                    "a.txt",
                    base,
                    b"first\nmiddle\nremote\n",
                    REGULAR_MODE,
                    original,
                    binary=True,
                ),
            ),
            ("*",),
        ).state
        == "published"
    )
    assert env.sync.synchronize(env.workspace_id).conflicted == ("a.txt",)
    assert (env.local / "a.txt").read_text() == "local\nmiddle\nlast\n"
    local_base, local_body = read_identified_path(env.local / "a.txt")
    resolved = BatchFile(
        "a.txt",
        local_base,
        b"resolved\nmiddle\nlast\n",
        REGULAR_MODE,
        local_body,
        binary=True,
    )
    files = env.sync.publication_files(env.workspace_id, (resolved,))
    assert files[0].binary is True
    assert files[0].original == b"first\nmiddle\nremote\n"


def test_incoming_symlink_is_recreated_without_following_its_target(
    env: Environment,
) -> None:
    from llm_cli.workspace.identity import ABSENT, SYMLINK_MODE

    item = BatchFile("source-link", ABSENT, b"a.txt", SYMLINK_MODE)
    assert env.publisher.publish("link", "shared", (item,), ("*",)).state == "published"
    report = env.sync.synchronize(env.workspace_id)
    assert report.applied == ("source-link",)
    assert (env.local / "source-link").is_symlink()
    assert (env.local / "source-link").readlink() == Path("a.txt")
    assert env.sync.publication_files(env.workspace_id, ()) == ()
