"""Isolated incoming batches retain edits and recover exact local transitions."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Sequence
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
