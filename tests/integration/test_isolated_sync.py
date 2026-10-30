"""Isolated incoming batches retain edits and recover exact local transitions."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from llm_cli.storage.control import ControlStore
from llm_cli.workspace import isolated_sync
from llm_cli.workspace.batches import (
    MAX_BATCH_BYTES,
    MAX_BATCH_FILES,
    BatchFile,
    SharedBatchPublisher,
)
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
    local = repository_factory(
        tmp_path / "private", {"a.txt": "first\nmiddle\nlast\n", "b.txt": "before\n"}
    )
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

    def capture(root: Path, protected_paths: Sequence[str] = ()) -> Manifest:
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


@pytest.mark.parametrize(
    "change",
    ["content", "create", "delete", "recreate", "rename", "executable", "binary"],
)
def test_unchanged_private_path_receives_batch_with_unseen_outside_base(
    env: Environment, change: str
) -> None:
    from llm_cli.workspace.identity import ABSENT, EXECUTABLE_MODE

    path = "new.txt" if change == "create" else "a.txt"
    original = b"" if change == "create" else (env.local / path).read_bytes()
    outside = b"operator note from outside Loupe\n"
    (env.primary / path).write_bytes(original + outside)
    if change == "recreate":
        (env.primary / path).unlink()
    elif change == "executable":
        (env.primary / path).chmod(0o755)
    base, before = read_identified_path(env.primary / path)
    assert base != identify_path(env.local / path)
    expected = b"remote\nmiddle\nlast\n" + outside
    if change == "binary":
        expected += b"\0\xff"
    mode = EXECUTABLE_MODE if change == "executable" else REGULAR_MODE
    if change == "rename":
        files = (
            BatchFile(path, base, None, "", before),
            BatchFile("renamed.txt", ABSENT, expected, mode),
        )
    else:
        files = (
            BatchFile(
                path,
                base,
                None if change == "delete" else expected,
                "" if change == "delete" else mode,
                before,
                binary=change == "binary",
            ),
        )
    assert (
        env.publisher.publish("outside-base", "shared", files, ("*",)).state
        == "published"
    )

    report = env.sync.synchronize(env.workspace_id, refresh_source=False)

    assert report.applied == tuple(file.relative_path for file in files)
    assert report.conflicted == report.merged == ()
    for file in files:
        assert identify_path(env.local / file.relative_path) == identify_path(
            env.primary / file.relative_path
        )
    received = "renamed.txt" if change == "rename" else path
    if change == "delete":
        assert not (env.local / received).exists()
    else:
        assert (env.local / received).read_bytes() == expected
    with env.store.connection() as con:
        state = con.execute(
            "SELECT generation,source_sequence FROM isolated_sync_state"
        ).fetchone()
        assert tuple(state) == (1, report.sequence)
    assert env.sync.synchronize(env.workspace_id, refresh_source=False).applied == ()
    assert env.sync.publication_files(env.workspace_id, ()) == ()


def test_unchanged_private_symlink_receives_unseen_outside_link_base(
    env: Environment,
) -> None:
    from llm_cli.workspace.identity import ABSENT, SYMLINK_MODE

    files = (
        BatchFile("c.txt", ABSENT, b"third target\n", REGULAR_MODE),
        BatchFile("source-link", ABSENT, b"a.txt", SYMLINK_MODE),
    )
    assert (
        env.publisher.publish("create-link", "shared", files, ("*",)).state
        == "published"
    )
    env.sync.synchronize(env.workspace_id, refresh_source=False)
    link = env.primary / "source-link"
    link.unlink()
    link.symlink_to("b.txt")
    base, before = read_candidate_path(link)
    assert (
        env.publisher.publish(
            "outside-link",
            "shared",
            (BatchFile("source-link", base, b"c.txt", SYMLINK_MODE, before),),
            ("*",),
        ).state
        == "published"
    )

    report = env.sync.synchronize(env.workspace_id, refresh_source=False)

    assert report.applied == ("source-link",)
    assert report.conflicted == ()
    assert (env.local / "source-link").readlink() == Path("c.txt")
    assert (env.local / "a.txt").read_text() == "first\nmiddle\nlast\n"
    assert (env.local / "b.txt").read_text() == "before\n"
    assert env.sync.publication_files(env.workspace_id, ()) == ()


@pytest.mark.parametrize(
    "private_change", ["content", "delete", "rename", "symlink", "directory"]
)
def test_unseen_incoming_base_preserves_genuine_private_changes(
    env: Environment, private_change: str
) -> None:
    local = env.local / "a.txt"
    if private_change == "content":
        local.write_bytes(b"private\nmiddle\nlast\n")
    elif private_change == "rename":
        local.rename(env.local / "private-renamed.txt")
    else:
        local.unlink()
        if private_change == "symlink":
            local.symlink_to("b.txt")
        elif private_change == "directory":
            local.mkdir()
            (local / "private.txt").write_text("private child")
    private_identity = identify_path(local)
    (env.primary / "a.txt").write_bytes(b"first\nmiddle\nlast\noperator note\n")
    env.publish("outside-base", {"a.txt": b"remote\nmiddle\nlast\noperator note\n"})
    assert env.sync.synchronize(env.workspace_id, refresh_source=False).conflicted == (
        "a.txt",
    )
    env.publish(
        "later",
        {
            "a.txt": b"remote\nnew middle\nlast\noperator note\n",
            "b.txt": b"unrelated\n",
        },
    )
    report = env.sync.synchronize(env.workspace_id, refresh_source=False)
    assert report.conflicted == ("a.txt",)
    assert report.applied == ("b.txt",)
    assert identify_path(local) == private_identity
    if private_change == "rename":
        assert (
            env.local / "private-renamed.txt"
        ).read_text() == "first\nmiddle\nlast\n"
    if private_change == "directory":
        assert (local / "private.txt").read_text() == "private child"
    restarted = IsolatedSynchronizer(env.store, env.publisher, env.sync.capture)
    restarted.synchronize(env.workspace_id, refresh_source=False)
    with pytest.raises(WorkspaceBrokerError, match="conflict remains unresolved"):
        restarted.publication_files(env.workspace_id, ())


@pytest.mark.parametrize("incoming_kind", ["symlink", "directory"])
def test_unseen_nonabsent_type_transition_remains_a_durable_conflict(
    env: Environment, incoming_kind: str
) -> None:
    from llm_cli.workspace.identity import ABSENT, DIRECTORY_MODE, SYMLINK_MODE

    original = (env.local / "a.txt").read_bytes()
    primary = env.primary / "a.txt"
    primary.unlink()
    if incoming_kind == "symlink":
        primary.symlink_to("b.txt")
        base, before = read_candidate_path(primary)
        incoming = BatchFile("a.txt", base, b"b.txt", SYMLINK_MODE, before)
    else:
        incoming = BatchFile("a.txt", ABSENT, b"", DIRECTORY_MODE)
    assert (
        env.publisher.publish("outside-type", "shared", (incoming,), ("*",)).state
        == "published"
    )
    assert env.sync.synchronize(env.workspace_id, refresh_source=False).conflicted == (
        "a.txt",
    )
    assert (env.local / "a.txt").read_bytes() == original
    base, before = read_candidate_path(primary)
    removed = BatchFile("a.txt", base, None, "", before)
    assert (
        env.publisher.publish("later-delete", "shared", (removed,), ("*",)).state
        == "published"
    )
    assert env.sync.synchronize(env.workspace_id, refresh_source=False).conflicted == (
        "a.txt",
    )
    restarted = IsolatedSynchronizer(env.store, env.publisher, env.sync.capture)
    restarted.synchronize(env.workspace_id, refresh_source=False)
    with pytest.raises(WorkspaceBrokerError, match="conflict remains unresolved"):
        restarted.publication_files(env.workspace_id, ())
    assert (env.local / "a.txt").read_bytes() == original
    assert not primary.exists()


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


@pytest.mark.parametrize("outside_base", [False, True])
@pytest.mark.parametrize("crash_after", [0, 1, 2])
def test_incoming_recovery_rolls_forward_without_duplicate_manifest_generation(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
    crash_after: int,
    outside_base: bool,
) -> None:
    outside = b"operator note\n" if outside_base else b""
    if outside_base:
        (env.primary / "a.txt").write_bytes(
            (env.primary / "a.txt").read_bytes() + outside
        )
    received = b"new a\n" + outside
    env.publish("incoming", {"a.txt": received, "b.txt": b"new b\n"})
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
    assert (env.local / "a.txt").read_bytes() == received
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


def test_incoming_policy_exclusion_precedes_local_secret_artifact_read(
    env: Environment, tmp_path: Path, git_run: Callable[..., str]
) -> None:
    protected = b"unknown-private-value-after-policy-change"
    (env.local / "a.txt").write_bytes(protected)
    excludes = tmp_path / "source-excludes"
    excludes.write_text("a.txt\n")
    git_run(env.local, "config", "core.excludesFile", str(excludes))
    env.publish("incoming", {"a.txt": b"shared source update\n"})
    artifact = env.publisher.candidate_dir / hashlib.sha256(protected).hexdigest()
    assert not artifact.exists()
    with pytest.raises(WorkspaceBrokerError, match="excluded"):
        env.sync.synchronize(env.workspace_id)
    assert not artifact.exists()
    assert (env.local / "a.txt").read_bytes() == protected


def test_later_disjoint_batch_on_conflicted_file_does_not_clear_divergence(
    env: Environment,
) -> None:
    (env.local / "a.txt").write_text("local\nmiddle\nlast\n")
    env.publish("first", {"a.txt": b"remote\nmiddle\nlast\n"})
    assert env.sync.synchronize(env.workspace_id).conflicted == ("a.txt",)
    env.publish(
        "second", {"a.txt": b"remote\nmiddle\nnew last\n", "b.txt": b"unrelated\n"}
    )
    report = env.sync.synchronize(env.workspace_id)
    assert report.conflicted == ("a.txt",)
    assert (env.local / "a.txt").read_text() == "local\nmiddle\nlast\n"
    assert (env.local / "b.txt").read_text() == "unrelated\n"
    restarted = IsolatedSynchronizer(env.store, env.publisher, env.sync.capture)
    restarted.synchronize(env.workspace_id)
    with pytest.raises(WorkspaceBrokerError, match="conflict remains unresolved"):
        restarted.publication_files(env.workspace_id, ())


@pytest.mark.parametrize(
    "body", [b"<<<<<<< your pending edits\n", b"\0\n<<<<<<< your pending edits\n\xff"]
)
def test_binary_marker_bytes_are_not_interpreted_as_text_conflicts(
    env: Environment, body: bytes
) -> None:
    from llm_cli.workspace.identity import ABSENT

    files = env.sync.publication_files(
        env.workspace_id,
        (BatchFile("binary.dat", ABSENT, body, REGULAR_MODE, binary=True),),
    )
    assert len(files) == 1
    assert files[0].binary
    assert files[0].content == body


@pytest.mark.parametrize("added_after_prepare", [False, True])
def test_incoming_directory_deletion_preserves_private_children_and_completes(
    env: Environment, monkeypatch: pytest.MonkeyPatch, added_after_prepare: bool
) -> None:
    from llm_cli.workspace.identity import ABSENT, DIRECTORY_MODE

    created = (
        BatchFile("folder", ABSENT, b"", DIRECTORY_MODE),
        BatchFile("folder/item.txt", ABSENT, b"original", REGULAR_MODE),
    )
    assert (
        env.publisher.publish("create", "shared", created, ("*",)).state == "published"
    )
    env.sync.synchronize(env.workspace_id)
    child = env.local / "folder/user-added.txt"
    if added_after_prepare:
        prepare = env.sync._prepare

        def add_after_preparation(*args: object) -> isolated_sync.SyncResult:
            result = prepare(*args)  # type: ignore[arg-type]
            child.write_text("private addition")
            return result

        monkeypatch.setattr(env.sync, "_prepare", add_after_preparation)
    else:
        child.write_text("private addition")
    base, original = read_identified_path(env.primary / "folder/item.txt")
    b_base, b_body = read_identified_path(env.primary / "b.txt")
    removed = (
        BatchFile("folder/item.txt", base, None, "", original),
        BatchFile("folder", identify_path(env.primary / "folder"), None, ""),
        BatchFile("b.txt", b_base, b"unrelated update", REGULAR_MODE, b_body),
    )
    assert (
        env.publisher.publish("remove", "shared", removed, ("*",)).state == "published"
    )
    env.sync.synchronize(env.workspace_id)
    assert child.read_text() == "private addition"
    assert not (env.local / "folder/item.txt").exists()
    assert (env.local / "b.txt").read_text() == "unrelated update"
    with env.store.connection() as con:
        assert (
            con.execute(
                "SELECT count(*) FROM isolated_sync_journal WHERE state='prepared'"
            ).fetchone()[0]
            == 0
        )
    restarted = IsolatedSynchronizer(env.store, env.publisher, env.sync.capture)
    restarted.synchronize(env.workspace_id)
    with pytest.raises(
        WorkspaceBrokerError, match="conflict remains unresolved: folder"
    ):
        restarted.publication_files(env.workspace_id, ())
    child.rename(env.local / "user-added.txt")
    child.parent.rmdir()
    files = restarted.publication_files(env.workspace_id, ())
    assert [file.relative_path for file in files] == ["user-added.txt"]


def test_idle_isolated_session_catches_up_across_multiple_event_pages(
    env: Environment,
) -> None:
    with env.store.connection() as con:
        checkout = con.execute(
            "SELECT checkout_id FROM workspaces WHERE workspace_id=?",
            (env.workspace_id,),
        ).fetchone()[0]
        for _ in range(1000):
            env.store.append_checkout_event(
                con,
                checkout_id=checkout,
                event_type="session.heartbeat",
                caused_by_session_id="shared",
                payload={},
                now=0,
            )
    env.publish("after-first-page", {"a.txt": b"after page boundary\n"})
    result = env.sync.synchronize(env.workspace_id)
    assert result.applied == ("a.txt",)
    assert (env.local / "a.txt").read_bytes() == b"after page boundary\n"
    with env.store.connection() as con:
        assert (
            con.execute("SELECT source_sequence FROM isolated_sync_state").fetchone()[0]
            == result.sequence
        )
    assert env.sync.synchronize(env.workspace_id).applied == ()


def test_runtime_alias_overlay_is_refused_before_retaining_its_body(
    env: Environment,
) -> None:
    from llm_cli.workspace.identity import ABSENT

    with env.store.connection() as con:
        con.execute(
            "UPDATE isolated_sync_state SET runtime_paths_json=?",
            ('["local.data"]',),
        )
    private = b"private unknown-format runtime value"
    artifact = env.publisher.candidate_dir / hashlib.sha256(private).hexdigest()
    with pytest.raises(WorkspaceBrokerError, match="runtime data"):
        env.sync.publication_files(
            env.workspace_id,
            (BatchFile("LOCAL.DATA", ABSENT, private, REGULAR_MODE),),
        )
    assert not artifact.exists()


def _real_capture(env: Environment) -> None:
    from llm_cli.workspace.isolated import SourceCaptureCache, capture_source

    cache = SourceCaptureCache()
    env.sync.capture = lambda root, protected=(): capture_source(
        root, env.publisher.candidate_dir, protected_paths=protected, cache=cache
    )
    env.sync.refresh_capture = lambda root, protected=(): capture_source(
        root,
        env.publisher.candidate_dir,
        protected_paths=protected,
        cache=cache,
        defer_changed=True,
    )


def test_moving_source_save_defers_one_path_and_receives_others(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.workspace import isolated

    _real_capture(env)
    env.sync.synchronize(env.workspace_id)
    (env.primary / "a.txt").write_text("saved source\n")
    (env.primary / "b.txt").write_text("unrelated change\n")
    original = isolated._capture_path

    def moving(root: Path, relative: str) -> tuple[bytes, object, bool]:
        if root == env.primary and relative == "a.txt":
            raise isolated._SourceChanged
        return original(root, relative)

    monkeypatch.setattr(isolated, "_capture_path", moving)
    report = env.sync.synchronize(env.workspace_id)
    assert report.deferred == ("a.txt",)
    assert (env.local / "a.txt").read_text() == "first\nmiddle\nlast\n"
    assert (env.local / "b.txt").read_text() == "unrelated change\n"
    monkeypatch.setattr(isolated, "_capture_path", original)
    assert env.sync.synchronize(env.workspace_id).applied == ("a.txt",)
    assert (env.local / "a.txt").read_text() == "saved source\n"


def test_new_screened_source_does_not_block_other_incoming_changes(
    env: Environment,
) -> None:
    _real_capture(env)
    secret = b"AKIA" + b"EXAMPLE000000000"
    (env.primary / "fixture.py").write_bytes(secret)
    (env.primary / "b.txt").write_text("new ordinary source\n")
    report = env.sync.synchronize(env.workspace_id)
    assert report.deferred == ()
    assert report.applied == ("b.txt", "fixture.py")
    assert (env.local / "b.txt").read_text() == "new ordinary source\n"
    assert (env.local / "fixture.py").read_bytes() == secret
    assert env.sync.publication_files(env.workspace_id, ()) == ()
    assert all(
        secret not in path.read_bytes()
        for path in env.publisher.candidate_dir.iterdir()
    )
    (env.primary / "b.txt").write_text("later ordinary source\n")
    assert env.sync.synchronize(env.workspace_id).applied == ("b.txt",)


def test_unchanged_source_scan_reuses_identities_and_artifacts(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.workspace import isolated

    _real_capture(env)
    env.sync.synchronize(env.workspace_id)
    reads: list[str] = []
    stores: list[bytes] = []
    original_read = isolated._capture_path
    original_store = isolated._store_source

    def read(root: Path, relative: str) -> tuple[bytes, object, bool]:
        reads.append(relative)
        return original_read(root, relative)

    def store(root: Path, body: bytes) -> str:
        stores.append(body)
        return original_store(root, body)

    monkeypatch.setattr(isolated, "_capture_path", read)
    monkeypatch.setattr(isolated, "_store_source", store)
    env.sync.synchronize(env.workspace_id)
    assert reads == stores == []
    import os

    original = (env.primary / "b.txt").stat()
    replacement = env.primary / "replacement.tmp"
    replacement.write_bytes(b"change\n")
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    replacement.replace(env.primary / "b.txt")
    env.sync.synchronize(env.workspace_id)
    assert reads == ["b.txt"]
    assert stores == [b"change\n"]


def test_source_scan_releases_publisher_lock_and_discards_raced_publication(
    env: Environment,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    capture = env.sync.refresh_capture
    once = False

    def scan(root: Path, protected: Sequence[str]) -> Manifest:
        nonlocal once
        snapshot = capture(root, protected)
        if not once:
            once = True
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    env.publish, "during-scan", {"b.txt": b"published\n"}
                )
                future.result(timeout=3)
        return snapshot

    env.sync.refresh_capture = scan
    report = env.sync.synchronize(env.workspace_id)
    assert report.deferred
    assert (env.local / "b.txt").read_text() == "before\n"
    env.sync.synchronize(env.workspace_id)
    assert (env.local / "b.txt").read_text() == "published\n"


def test_cached_source_still_honors_changed_exclusion_policy(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.workspace import isolated

    _real_capture(env)
    env.sync.synchronize(env.workspace_id)
    original = isolated.excluded_paths

    def exclusions(root: Path, paths: list[str]) -> set[str]:
        return original(root, paths) | ({"a.txt"} if root == env.primary else set())

    monkeypatch.setattr(isolated, "excluded_paths", exclusions)
    (env.primary / "b.txt").write_text("other changed source\n")
    report = env.sync.synchronize(env.workspace_id)
    assert report.deferred == ("a.txt",)
    assert (env.local / "a.txt").read_text() == "first\nmiddle\nlast\n"
    assert (env.local / "b.txt").read_text() == "other changed source\n"


def test_source_cache_evicts_closed_root_metadata_without_losing_source(
    env: Environment,
) -> None:
    from llm_cli.workspace.isolated import SourceCaptureCache, capture_source

    cache = SourceCaptureCache(max_entries=2)
    initial = capture_source(env.primary, env.publisher.candidate_dir, cache=cache)
    assert len(cache.entries) == 2
    capture_source(env.local, env.publisher.candidate_dir, cache=cache)
    assert len(cache.entries) == 2
    assert all(root == env.local for root, _ in cache.entries)
    refreshed = capture_source(env.primary, env.publisher.candidate_dir, cache=cache)
    assert refreshed == initial
    assert len(cache.entries) == 2


def _screened_fixture(env: Environment, label: bytes) -> bytes:
    body = b"KEY = 'AKIA" + label * 16 + b"'\n"
    (env.primary / "fixture.py").write_bytes(body)
    return body


def _no_private_artifacts(env: Environment, *bodies: bytes) -> None:
    assert all(
        all(body not in path.read_bytes() for body in bodies)
        for path in env.publisher.candidate_dir.iterdir()
        if path.is_file()
    )
    with env.store.connection() as con:
        journals = con.execute("SELECT * FROM isolated_sync_journal").fetchall()
    assert all(
        body.decode() not in repr(tuple(row)) for row in journals for body in bodies
    )


def test_screened_incoming_conflict_preserves_both_and_notifies_once(
    env: Environment,
) -> None:
    _real_capture(env)
    initial = _screened_fixture(env, b"A")
    assert env.sync.synchronize(env.workspace_id).applied == ("fixture.py",)
    local = initial.replace(b"A" * 16, b"L" * 16)
    (env.local / "fixture.py").write_bytes(local)
    incoming = _screened_fixture(env, b"B")
    report = env.sync.synchronize(env.workspace_id)
    assert report.conflicted == ("fixture.py",)
    assert incoming.decode() not in report.note()
    assert (env.local / "fixture.py").read_bytes() == local
    staged = list((env.local / ".git/loupe-screened-incoming").iterdir())
    assert any(path.read_bytes() == incoming for path in staged)
    assert all(not path.stat().st_mode & 0o077 for path in staged)
    assert env.sync.synchronize(env.workspace_id).note() == ""
    latest = _screened_fixture(env, b"C")
    assert env.sync.synchronize(env.workspace_id).note() == ""
    assert (env.local / "fixture.py").read_bytes() == local
    assert any(
        path.read_bytes() == latest
        for path in (env.local / ".git/loupe-screened-incoming").iterdir()
    )
    _no_private_artifacts(env, initial, local, incoming, latest)
    with pytest.raises(WorkspaceBrokerError, match="conflict"):
        env.sync.publication_files(env.workspace_id, ())


@pytest.mark.parametrize("after_write", [False, True])
def test_screened_sync_crash_replays_private_body_not_later_primary(
    env: Environment, monkeypatch: pytest.MonkeyPatch, after_write: bool
) -> None:
    _real_capture(env)
    captured = _screened_fixture(env, b"A")
    original = isolated_sync.atomic_replace_regular_file

    def crash(**kwargs: object) -> None:
        if after_write:
            original(**kwargs)
        raise RuntimeError("daemon interrupted")

    monkeypatch.setattr(isolated_sync, "atomic_replace_regular_file", crash)
    with pytest.raises(RuntimeError, match="interrupted"):
        env.sync.synchronize(env.workspace_id)
    later = _screened_fixture(env, b"B")
    monkeypatch.setattr(isolated_sync, "atomic_replace_regular_file", original)
    env.sync.recover(env.workspace_id)
    assert (env.local / "fixture.py").read_bytes() == captured
    assert not list((env.local / ".git/loupe-screened-incoming").iterdir())
    assert env.sync.synchronize(env.workspace_id).applied == ("fixture.py",)
    assert (env.local / "fixture.py").read_bytes() == later
    _no_private_artifacts(env, captured, later)


def test_screened_recovery_preserves_later_private_edit(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_capture(env)
    incoming = _screened_fixture(env, b"A")
    original = isolated_sync.atomic_replace_regular_file

    def crash(**kwargs: object) -> None:
        raise RuntimeError("daemon interrupted")

    monkeypatch.setattr(isolated_sync, "atomic_replace_regular_file", crash)
    with pytest.raises(RuntimeError):
        env.sync.synchronize(env.workspace_id)
    local = b"private later edit\n"
    (env.local / "fixture.py").write_bytes(local)
    monkeypatch.setattr(isolated_sync, "atomic_replace_regular_file", original)
    with pytest.raises(WorkspaceBrokerError, match="unrecognized local state"):
        env.sync.recover(env.workspace_id)
    assert (env.local / "fixture.py").read_bytes() == local
    assert (env.primary / "fixture.py").read_bytes() == incoming
    _no_private_artifacts(env, incoming, local)


def test_screened_save_between_scan_and_copy_defers_only_that_path(
    env: Environment,
) -> None:
    _real_capture(env)
    first = _screened_fixture(env, b"A")
    original = env.sync.refresh_capture

    def moving(root: Path, protected: Sequence[str]) -> Manifest:
        captured = original(root, protected)
        _screened_fixture(env, b"B")
        return captured

    env.sync.refresh_capture = moving
    (env.primary / "b.txt").write_text("ordinary update\n")
    result = env.sync.synchronize(env.workspace_id)
    assert result.applied == ("b.txt",)
    assert result.deferred == ("fixture.py",)
    assert not (env.local / "fixture.py").exists()
    env.sync.refresh_capture = original
    assert env.sync.synchronize(env.workspace_id).applied == ("fixture.py",)
    assert (env.local / "fixture.py").read_bytes() != first


def test_screened_deletion_conflict_keeps_private_cleanup_identity(
    env: Environment,
) -> None:
    _real_capture(env)
    initial = _screened_fixture(env, b"A")
    env.sync.synchronize(env.workspace_id)
    local = initial.replace(b"A" * 16, b"L" * 16)
    (env.local / "fixture.py").write_bytes(local)
    (env.primary / "fixture.py").unlink()
    assert env.sync.synchronize(env.workspace_id).conflicted == ("fixture.py",)
    assert env.sync.synchronize(env.workspace_id).note() == ""
    assert (env.local / "fixture.py").read_bytes() == local
    with env.store.connection() as con:
        manifest = json.loads(
            con.execute("SELECT manifest_json FROM isolated_sync_state").fetchone()[0]
        )
    assert manifest["fixture.py"]["screened"]
    assert manifest["fixture.py"]["kind"] == "absent"
    _no_private_artifacts(env, initial, local)


def test_nested_screened_fixture_arrives_and_its_directory_can_later_be_deleted(
    env: Environment,
) -> None:
    _real_capture(env)
    (env.primary / "fixtures").mkdir()
    private = b"KEY = 'AKIA" + b"A" * 16 + b"'\n"
    (env.primary / "fixtures/key.py").write_bytes(private)
    assert env.sync.synchronize(env.workspace_id).applied == ("fixtures/key.py",)
    assert (env.local / "fixtures/key.py").read_bytes() == private
    (env.primary / "fixtures/key.py").unlink()
    (env.primary / "fixtures").rmdir()
    result = env.sync.synchronize(env.workspace_id)
    assert "fixtures/key.py" in result.applied
    assert not result.conflicted
    assert not (env.local / "fixtures").exists()
    assert env.sync.synchronize(env.workspace_id).note() == ""
    _no_private_artifacts(env, private)


def test_screened_staging_symlink_is_refused_before_private_write(
    env: Environment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.errors import LlmCoordError

    _real_capture(env)
    incoming = _screened_fixture(env, b"A")
    original = isolated_sync.atomic_replace_regular_file

    def crash(**kwargs: object) -> None:
        raise RuntimeError("daemon interrupted")

    monkeypatch.setattr(isolated_sync, "atomic_replace_regular_file", crash)
    with pytest.raises(RuntimeError):
        env.sync.synchronize(env.workspace_id)
    staged = next((env.local / ".git/loupe-screened-incoming").iterdir())
    outside = tmp_path / "outside-private"
    outside.write_bytes(incoming)
    staged.unlink()
    staged.symlink_to(outside)
    monkeypatch.setattr(isolated_sync, "atomic_replace_regular_file", original)
    with pytest.raises((LlmCoordError, OSError)):
        env.sync.recover(env.workspace_id)
    assert not (env.local / "fixture.py").exists()
    assert outside.read_bytes() == incoming
    _no_private_artifacts(env, incoming)


def test_screened_fixture_above_changed_path_limit_defers_without_failing_others(
    env: Environment,
) -> None:
    from llm_cli.workspace.broker import MAX_SHARED_TEXT_BYTES

    _real_capture(env)
    private = b"AKIA" + b"A" * 16 + b"\n" + b"x" * MAX_SHARED_TEXT_BYTES
    (env.primary / "large_fixture.py").write_bytes(private)
    (env.primary / "b.txt").write_text("ordinary update\n")
    result = env.sync.synchronize(env.workspace_id)
    assert result.applied == ("b.txt",)
    assert result.deferred == ("large_fixture.py",)
    assert "large_fixture.py" in result.note()
    assert not (env.local / "large_fixture.py").exists()
    assert not (env.local / ".git/loupe-screened-incoming").exists()
    _no_private_artifacts(env, private)


@pytest.mark.parametrize("added_after_prepare", [False, True])
def test_unseen_file_base_deletion_preserves_accepted_directory_private_children(
    env: Environment, monkeypatch: pytest.MonkeyPatch, added_after_prepare: bool
) -> None:
    from llm_cli.workspace.identity import ABSENT, DIRECTORY_MODE

    assert (
        env.publisher.publish(
            "create-directory",
            "shared",
            (BatchFile("folder", ABSENT, b"", DIRECTORY_MODE),),
            ("*",),
        ).state
        == "published"
    )
    env.sync.synchronize(env.workspace_id, refresh_source=False)
    child = env.local / "folder/private.txt"
    if added_after_prepare:
        prepare = env.sync._prepare

        def add_after_preparation(*args: object) -> isolated_sync.SyncResult:
            report = prepare(*args)  # type: ignore[arg-type]
            child.write_text("private addition")
            return report

        monkeypatch.setattr(env.sync, "_prepare", add_after_preparation)
    else:
        child.write_text("private addition")
    source = env.primary / "folder"
    source.rmdir()
    source.write_text("operator replaced the source directory")
    base, before = read_candidate_path(source)
    b_base, b_body = read_candidate_path(env.primary / "b.txt")
    removed = (
        BatchFile("folder", base, None, "", before),
        BatchFile("b.txt", b_base, b"unrelated update", REGULAR_MODE, b_body),
    )
    assert (
        env.publisher.publish("remove-file", "shared", removed, ("*",)).state
        == "published"
    )

    env.sync.synchronize(env.workspace_id, refresh_source=False)

    assert child.read_text() == "private addition"
    assert (env.local / "b.txt").read_bytes() == b"unrelated update"
    assert not source.exists()
    with env.store.connection() as con:
        assert (
            con.execute(
                "SELECT count(*) FROM isolated_sync_journal WHERE state='prepared'"
            ).fetchone()[0]
            == 0
        )
    restarted = IsolatedSynchronizer(env.store, env.publisher, env.sync.capture)
    restarted.synchronize(env.workspace_id, refresh_source=False)
    with pytest.raises(
        WorkspaceBrokerError, match="conflict remains unresolved: folder"
    ):
        restarted.publication_files(env.workspace_id, ())


def test_receipt_only_locked_safe_point_does_not_refresh_runtime(
    env: Environment,
) -> None:
    refreshed: list[str] = []
    env.sync.runtime_refresh = lambda workspace: refreshed.append(workspace) or ()
    with env.publisher.lock:
        env.sync.synchronize(
            env.workspace_id, refresh_source=False, refresh_runtime=True
        )
    assert refreshed == []
    env.sync.synchronize(env.workspace_id)
    assert refreshed == []
    env.sync.synchronize(env.workspace_id, refresh_runtime=True)
    assert refreshed == [env.workspace_id]


def _prepared_journals(env: Environment) -> int:
    with env.store.connection() as con:
        return int(
            con.execute(
                "SELECT count(*) FROM isolated_sync_journal WHERE state='prepared'"
            ).fetchone()[0]
        )


def test_branch_switch_beyond_one_batch_arrives_and_switches_back(
    env: Environment, git_run: Callable[..., str]
) -> None:
    _real_capture(env)
    env.sync.synchronize(env.workspace_id)
    added = [
        f"pkg/part{number % 3}/module{number:03}.py"
        for number in range(3 * MAX_BATCH_FILES)
    ]
    git_run(env.primary, "checkout", "-b", "wide")
    for path in added:
        (env.primary / path).parent.mkdir(parents=True, exist_ok=True)
        (env.primary / path).write_text(f"name = {path!r}\n")
    (env.primary / "a.txt").write_text("wide branch\n")
    git_run(env.primary, "add", "-A")
    git_run(env.primary, "commit", "-m", "wide")

    report = env.sync.synchronize(env.workspace_id)

    assert {*added, "a.txt"} <= set(report.applied)
    for path in added:
        assert (env.local / path).read_text() == f"name = {path!r}\n"
    assert (env.local / "a.txt").read_text() == "wide branch\n"
    # The notice names a bounded sample, not every received path.
    assert f"and {len(report.applied) - 20} more" in report.note()

    git_run(env.primary, "checkout", "main")
    report = env.sync.synchronize(env.workspace_id)

    assert set(added) <= set(report.applied)
    assert not (env.local / "pkg").exists()
    assert (env.local / "a.txt").read_text() == "first\nmiddle\nlast\n"
    (env.primary / "b.txt").write_text("later edit\n")
    assert env.sync.synchronize(env.workspace_id).applied == ("b.txt",)
    assert _prepared_journals(env) == 0


def test_large_outside_source_files_reach_the_private_worktree(
    env: Environment,
) -> None:
    from llm_cli.workspace.broker import MAX_SHARED_TEXT_BYTES

    _real_capture(env)
    env.sync.synchronize(env.workspace_id)
    lock = "".join(f"package-{number} = {number}\n" for number in range(40_000))
    locks = [f"locks/{number}.lock" for number in range(8)]
    assert len(lock) > MAX_SHARED_TEXT_BYTES
    assert len(lock) * len(locks) > MAX_BATCH_BYTES
    (env.primary / "locks").mkdir()
    for path in locks:
        (env.primary / path).write_text(lock)

    report = env.sync.synchronize(env.workspace_id)

    assert set(locks) <= set(report.applied)
    assert all((env.local / path).read_text() == lock for path in locks)

    grown = lock + "package-new = 1\n"
    (env.primary / locks[0]).write_text(grown)
    (env.primary / locks[1]).write_text("small now\n")
    (env.primary / locks[2]).unlink()
    report = env.sync.synchronize(env.workspace_id)
    assert set(report.applied) == set(locks[:3])
    assert (env.local / locks[0]).read_text() == grown
    assert (env.local / locks[1]).read_text() == "small now\n"
    assert not (env.local / locks[2]).exists()

    # A private edit still merges with an outside edit to the same large file.
    rest = lock.split("\n", 1)[1]
    (env.local / locks[3]).write_text("private header\n" + rest)
    (env.primary / locks[3]).write_text(lock + "package-outside = 2\n")
    report = env.sync.synchronize(env.workspace_id)
    assert report.merged == (locks[3],)
    assert (env.local / locks[3]).read_text() == (
        "private header\n" + rest + "package-outside = 2\n"
    )


def test_outside_kind_changes_replace_private_paths(env: Environment) -> None:
    _real_capture(env)
    env.sync.synchronize(env.workspace_id)
    (env.primary / "a.txt").unlink()
    (env.primary / "a.txt").symlink_to("b.txt")

    assert env.sync.synchronize(env.workspace_id).applied == ("a.txt",)
    assert os.readlink(env.local / "a.txt") == "b.txt"

    (env.primary / "a.txt").unlink()
    (env.primary / "a.txt").write_text("a file again\n")
    assert env.sync.synchronize(env.workspace_id).applied == ("a.txt",)
    assert not (env.local / "a.txt").is_symlink()
    assert (env.local / "a.txt").read_text() == "a file again\n"


def test_file_and_directory_of_the_same_name_replace_each_other(
    env: Environment,
) -> None:
    _real_capture(env)
    (env.primary / "tool").write_text("#!/bin/sh\n")
    env.sync.synchronize(env.workspace_id)
    assert (env.local / "tool").read_text() == "#!/bin/sh\n"

    (env.primary / "tool").unlink()
    (env.primary / "tool").mkdir()
    (env.primary / "tool" / "main.py").write_text("print('tool')\n")
    report = env.sync.synchronize(env.workspace_id)
    assert report.conflicted == report.deferred == ()
    assert (env.local / "tool" / "main.py").read_text() == "print('tool')\n"

    (env.primary / "tool" / "main.py").unlink()
    (env.primary / "tool").rmdir()
    (env.primary / "tool").write_text("#!/bin/sh\n")
    report = env.sync.synchronize(env.workspace_id)
    assert report.conflicted == report.deferred == ()
    assert (env.local / "tool").read_text() == "#!/bin/sh\n"


def test_private_file_where_a_directory_arrives_defers_only_that_subtree(
    env: Environment,
) -> None:
    _real_capture(env)
    (env.primary / "tool").write_text("#!/bin/sh\n")
    env.sync.synchronize(env.workspace_id)
    (env.local / "tool").write_text("#!/bin/sh\necho private\n")
    (env.primary / "tool").unlink()
    (env.primary / "tool").mkdir()
    (env.primary / "tool" / "main.py").write_text("print('tool')\n")
    (env.primary / "b.txt").write_text("unrelated\n")

    report = env.sync.synchronize(env.workspace_id)

    assert report.conflicted == ("tool",)
    assert report.deferred == ("tool/main.py",)
    assert (env.local / "tool").read_text() == "#!/bin/sh\necho private\n"
    assert (env.local / "b.txt").read_text() == "unrelated\n"
    # The blocked path waits without failing later safe points.
    (env.primary / "a.txt").write_text("later\n")
    report = env.sync.synchronize(env.workspace_id)
    assert report.applied == ("a.txt",)
    assert report.deferred == ("tool/main.py",)
    assert _prepared_journals(env) == 0


def test_catch_up_stops_while_a_managed_writer_postpones_recovery(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_capture(env)
    env.sync.synchronize(env.workspace_id)
    paths = [f"many/{number:03}.txt" for number in range(2 * MAX_BATCH_FILES)]
    (env.primary / "many").mkdir()
    for path in paths:
        (env.primary / path).write_text(path + "\n")
    prepare = env.sync._prepare

    def writer_starts(*args: object) -> isolated_sync.SyncResult:
        result = prepare(*args)  # type: ignore[arg-type]
        env.sync.process_writer_notice = lambda workspace: "a writer is running"
        return result

    monkeypatch.setattr(env.sync, "_prepare", writer_starts)
    report = env.sync.synchronize(env.workspace_id)

    # The first journal waits for the writer; later ones are not prepared.
    assert _prepared_journals(env) == 1
    assert report.deferred == tuple(paths[MAX_BATCH_FILES - 1 :])
    assert not (env.local / "many").exists()

    monkeypatch.setattr(env.sync, "_prepare", prepare)
    env.sync.process_writer_notice = None
    env.sync.synchronize(env.workspace_id)
    assert _prepared_journals(env) == 0
    assert all((env.local / path).read_text() == path + "\n" for path in paths)


def test_catch_up_journals_respect_stored_batch_limits() -> None:
    def regular(size: int) -> dict[str, object]:
        return {
            "kind": "regular",
            "mode": REGULAR_MODE,
            "digest": "0" * 64,
            "size": size,
        }

    small = [
        isolated_sync._CatchUp(f"file{number}", None, regular(1))
        for number in range(2 * MAX_BATCH_FILES + 1)
    ]
    assert [len(chunk) for chunk in isolated_sync._chunks(small)] == [
        MAX_BATCH_FILES,
        MAX_BATCH_FILES,
        1,
    ]
    half = MAX_BATCH_BYTES // 2 + 1
    large = [
        isolated_sync._CatchUp("a", None, regular(half)),
        isolated_sync._CatchUp("b", regular(half), regular(1)),
        isolated_sync._CatchUp("c", regular(3 * MAX_BATCH_BYTES), None),
        isolated_sync._CatchUp("d", None, regular(1)),
    ]
    assert [
        [step.path for step in chunk] for chunk in isolated_sync._chunks(large)
    ] == [["a"], ["b"], ["c"], ["d"]]


@pytest.mark.parametrize(
    "private_edit",
    [None, "content", "new_file", "deleted", "empty_directory", "symlink"],
)
def test_opened_directory_replaced_by_outside_file(
    env: Environment, private_edit: str | None
) -> None:
    _real_capture(env)
    for root in (env.primary, env.local):
        (root / "opened/nested").mkdir(parents=True)
        (root / "opened/nested/child.txt").write_text("opened source\n")
    # Isolated opening captures source leaves, without directory markers.
    initial = env.sync.capture(env.primary, ())
    assert "opened" not in initial
    with env.store.connection() as con:
        con.execute(
            "UPDATE isolated_sync_state SET manifest_json=? WHERE workspace_id=?",
            (json.dumps(initial), env.workspace_id),
        )
    child = env.local / "opened/nested/child.txt"
    if private_edit == "content":
        child.write_text("private source\n")
    elif private_edit == "new_file":
        (env.local / "opened/private.txt").write_text("private addition\n")
    elif private_edit == "deleted":
        child.unlink()
    elif private_edit == "empty_directory":
        (env.local / "opened/private").mkdir()
    elif private_edit == "symlink":
        child.unlink()
        child.symlink_to("../../b.txt")
    (env.primary / "opened/nested/child.txt").unlink()
    (env.primary / "opened/nested").rmdir()
    (env.primary / "opened").rmdir()
    (env.primary / "opened").write_text("outside file\n")

    report = env.sync.synchronize(env.workspace_id)

    assert report.deferred == ()
    if private_edit:
        assert "opened" in report.conflicted
        assert (env.local / "opened").is_dir()
        if private_edit == "content":
            assert child.read_text() == "private source\n"
        elif private_edit == "new_file":
            assert (
                env.local / "opened/private.txt"
            ).read_text() == "private addition\n"
        elif private_edit == "deleted":
            assert not child.exists()
        elif private_edit == "empty_directory":
            assert (env.local / "opened/private").is_dir()
        elif private_edit == "symlink":
            assert child.is_symlink()
            assert os.readlink(child) == "../../b.txt"
    else:
        assert report.conflicted == ()
        assert (env.local / "opened").read_text() == "outside file\n"
    assert _prepared_journals(env) == 0
    (env.primary / "b.txt").write_text("later outside change\n")
    assert env.sync.synchronize(env.workspace_id).applied == ("b.txt",)
    assert _prepared_journals(env) == 0


@pytest.mark.parametrize("outside_only", [False, True])
def test_large_catch_up_yields_and_recomputes_after_interleaved_changes(
    env: Environment, monkeypatch: pytest.MonkeyPatch, outside_only: bool
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    _real_capture(env)
    env.sync.synchronize(env.workspace_id)
    paths = [f"outside{number:03}.txt" for number in range(3 * MAX_BATCH_FILES)]
    for path in paths:
        (env.primary / path).write_text("outside initial\n")
    interleaved = False
    pool = ThreadPoolExecutor(max_workers=1)

    def between_journals(workspace_id: str) -> None:
        nonlocal interleaved
        assert workspace_id == env.workspace_id
        if interleaved:
            return
        interleaved = True
        assert 0 < sum((env.local / path).exists() for path in paths) < len(paths)
        if outside_only:
            (env.primary / paths[-1]).write_text("new outside version\n")
            (env.primary / paths[-2]).unlink()
        else:
            future = pool.submit(
                env.publish,
                "between-journals",
                {
                    "b.txt": b"published between\n",
                    paths[-1]: b"new published version\n",
                },
            )
            future.result(timeout=3)
        assert not (env.local / paths[-1]).exists()

    monkeypatch.setattr(env.sync, "_between_journals", between_journals, raising=False)
    try:
        report = env.sync.synchronize(env.workspace_id)
    finally:
        pool.shutdown(wait=True)

    assert interleaved
    assert report.conflicted == report.deferred == ()
    assert all((env.local / path).is_file() for path in paths[:-2])
    if outside_only:
        assert not (env.local / paths[-2]).exists()
        assert (env.local / paths[-1]).read_text() == "new outside version\n"
    else:
        assert (env.local / "b.txt").read_text() == "published between\n"
        assert (env.local / paths[-1]).read_text() == "new published version\n"
    assert _prepared_journals(env) == 0


@pytest.mark.parametrize("publish_before_crash", [False, True])
def test_crash_between_catch_up_journals_preserves_confirmed_steps(
    env: Environment, monkeypatch: pytest.MonkeyPatch, publish_before_crash: bool
) -> None:
    _real_capture(env)
    env.sync.synchronize(env.workspace_id)
    paths = [f"outside{number:03}.txt" for number in range(3 * MAX_BATCH_FILES)]
    for path in paths:
        (env.primary / path).write_text("outside initial\n")

    def crash_between_journals(workspace_id: str) -> None:
        assert workspace_id == env.workspace_id
        if publish_before_crash:
            env.publish(
                "before-crash",
                {"b.txt": b"published before crash\n", paths[-1]: b"latest version\n"},
            )
        raise OSError("crash between confirmed journals")

    monkeypatch.setattr(
        env.sync, "_between_journals", crash_between_journals, raising=False
    )
    with pytest.raises(OSError, match="crash between confirmed journals"):
        env.sync.synchronize(env.workspace_id)
    confirmed = {path for path in paths if (env.local / path).exists()}
    assert len(confirmed) == MAX_BATCH_FILES
    assert _prepared_journals(env) == 0
    replace = isolated_sync.atomic_replace_regular_file

    def no_reapplication(**kwargs: object) -> None:
        assert kwargs["relative_path"] not in confirmed
        replace(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(isolated_sync, "atomic_replace_regular_file", no_reapplication)
    restarted = IsolatedSynchronizer(
        env.store,
        env.publisher,
        env.sync.capture,
        refresh_capture=env.sync.refresh_capture,
    )
    report = restarted.synchronize(env.workspace_id)

    assert report.conflicted == report.deferred == ()
    assert all((env.local / path).is_file() for path in paths)
    if publish_before_crash:
        assert (env.local / "b.txt").read_text() == "published before crash\n"
        assert (env.local / paths[-1]).read_text() == "latest version\n"
    assert _prepared_journals(env) == 0


def test_opening_directory_normalization_recovers_between_journals(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_capture(env)
    for root in (env.primary, env.local):
        (root / "opened/nested").mkdir(parents=True)
        (root / "opened/nested/child.txt").write_text("opened source\n")
    initial = env.sync.capture(env.primary, ())
    with env.store.connection() as con:
        con.execute(
            "UPDATE isolated_sync_state SET manifest_json=? WHERE workspace_id=?",
            (json.dumps(initial), env.workspace_id),
        )
    (env.primary / "opened/nested/child.txt").unlink()
    (env.primary / "opened/nested").rmdir()
    (env.primary / "opened").rmdir()
    (env.primary / "opened").write_text("outside file\n")

    def crash_after_normalization(workspace_id: str) -> None:
        with env.store.connection() as con:
            state = con.execute(
                "SELECT manifest_json FROM isolated_sync_state WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
        manifest = json.loads(state[0])
        assert manifest["opened"]["kind"] == "directory_marker"
        assert manifest["opened/nested"]["kind"] == "directory_marker"
        assert (env.local / "opened/nested/child.txt").read_text() == "opened source\n"
        raise OSError("crash after directory normalization")

    monkeypatch.setattr(
        env.sync, "_between_journals", crash_after_normalization, raising=False
    )
    with pytest.raises(OSError, match="crash after directory normalization"):
        env.sync.synchronize(env.workspace_id)
    assert _prepared_journals(env) == 0
    restarted = IsolatedSynchronizer(
        env.store,
        env.publisher,
        env.sync.capture,
        refresh_capture=env.sync.refresh_capture,
    )

    report = restarted.synchronize(env.workspace_id)

    assert report.conflicted == report.deferred == ()
    assert (env.local / "opened").read_text() == "outside file\n"
    assert _prepared_journals(env) == 0


@pytest.mark.parametrize("private_file", [False, True])
def test_opening_directory_changed_after_proof_is_not_recreated(
    env: Environment, monkeypatch: pytest.MonkeyPatch, private_file: bool
) -> None:
    _real_capture(env)
    for root in (env.primary, env.local):
        (root / "opened").mkdir()
        (root / "opened/child.txt").write_text("opened source\n")
    initial = env.sync.capture(env.primary, ())
    with env.store.connection() as con:
        con.execute(
            "UPDATE isolated_sync_state SET manifest_json=? WHERE workspace_id=?",
            (json.dumps(initial), env.workspace_id),
        )
    (env.primary / "opened/child.txt").unlink()
    (env.primary / "opened").rmdir()
    (env.primary / "opened").write_text("outside file\n")
    prepare = env.sync._prepare
    private = b"AKIA" + b"EXAMPLE000000000"

    def change_after_proof(*args: Any, **kwargs: Any) -> isolated_sync.SyncResult:
        if kwargs.get("existing_directories"):
            (env.local / "opened/child.txt").unlink()
            (env.local / "opened").rmdir()
            if private_file:
                (env.local / "opened").write_bytes(private)
        return prepare(*args, **kwargs)

    monkeypatch.setattr(env.sync, "_prepare", change_after_proof)
    report = env.sync.synchronize(env.workspace_id)

    assert report.deferred == ("opened",)
    assert _prepared_journals(env) == 0
    if private_file:
        assert (env.local / "opened").read_bytes() == private
    else:
        assert not (env.local / "opened").exists()
    assert all(
        private not in path.read_bytes()
        for path in env.publisher.candidate_dir.rglob("*")
        if path.is_file()
    )


@pytest.mark.parametrize("unknown_kind", ["large_file", "directory_fanout"])
def test_opening_directory_proof_never_reads_or_enumerates_unknown_private_data(
    env: Environment, monkeypatch: pytest.MonkeyPatch, unknown_kind: str
) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    _real_capture(env)
    for root in (env.primary, env.local):
        (root / "opened").mkdir()
        (root / "opened/child.txt").write_text("opened source\n")
    initial = env.sync.capture(env.primary, ())
    with env.store.connection() as con:
        con.execute(
            "UPDATE isolated_sync_state SET manifest_json=? WHERE workspace_id=?",
            (json.dumps(initial), env.workspace_id),
        )
    (env.primary / "opened/child.txt").unlink()
    (env.primary / "opened").rmdir()
    (env.primary / "opened").write_text("outside file\n")
    unknown = env.local / "opened/generated"
    if unknown_kind == "large_file":
        with unknown.open("wb") as stream:
            stream.truncate(isolated_sync._SOURCE_FILE_BYTES + 1)
    else:
        unknown.mkdir()
        directory = (env.local / "opened").stat()
        scandir = os.scandir

        @contextmanager
        def fanout() -> Any:
            def entries() -> Any:
                yield SimpleNamespace(name=unknown.name)
                pytest.fail(
                    "private fanout was enumerated after its first unknown entry"
                )

            yield entries()

        def bounded_scandir(path: Any = ".") -> Any:
            if isinstance(path, int):
                opened = os.fstat(path)
                if (opened.st_dev, opened.st_ino) == (
                    directory.st_dev,
                    directory.st_ino,
                ):
                    return fanout()
            return scandir(path)

        monkeypatch.setattr(os, "scandir", bounded_scandir)
    read = isolated_sync.read_candidate_path

    def expected_only(path: Path, **kwargs: Any) -> Any:
        assert path != unknown and unknown not in path.parents
        return read(path, **kwargs)

    monkeypatch.setattr(isolated_sync, "read_candidate_path", expected_only)

    report = env.sync.synchronize(env.workspace_id)

    assert "opened" in report.conflicted
    assert (env.local / "opened").is_dir()
    assert unknown.exists()
    assert _prepared_journals(env) == 0
    (env.primary / "b.txt").write_text("later outside change\n")
    assert env.sync.synchronize(env.workspace_id).applied == ("b.txt",)
