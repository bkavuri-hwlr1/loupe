"""Real-checkout publication, durable conflicts, and crash-boundary recovery."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.storage.control import ControlStore
from llm_cli.workspace import batches
from llm_cli.workspace.batches import BatchFile, SharedBatchPublisher
from llm_cli.workspace.broker import WorkspaceBrokerError, load_candidate_content
from llm_cli.workspace.identity import (
    ABSENT,
    EXECUTABLE_MODE,
    REGULAR_MODE,
    identify_path,
)


@dataclass
class BatchEnvironment:
    repository: Path
    store: ControlStore
    publisher: SharedBatchPublisher
    workspace_id: str

    def files(self) -> tuple[BatchFile, ...]:
        return tuple(
            BatchFile(
                path, identify_path(self.repository / path), content, REGULAR_MODE
            )
            for path, content in (("a.txt", b"new a\n"), ("docs/b.txt", b"new b\n"))
        )


@pytest.fixture
def batch_env(
    tmp_path: Path, repository_factory: Callable[[Path, dict[str, str]], Path]
) -> BatchEnvironment:
    repository = repository_factory(
        tmp_path, {"a.txt": "old a\n", "docs/b.txt": "old b\n"}
    )
    store = ControlStore(tmp_path / "control.sqlite3")
    store.initialize()
    registered = store.register_repository(
        repo_key="a" * 64,
        display_name="batch fixture",
        git_common_dir=str(repository / ".git"),
        main_worktree_path=str(repository),
        target_ref="refs/heads/main",
        path_case_insensitive=False,
    )
    checkout = store.ensure_checkout(
        repository=registered,
        canonical_path=str(repository),
        git_common_dir=str(repository / ".git"),
    )
    workspace = store.ensure_shared_workspace(checkout)
    for session_id in ("first", "second"):
        store.open_session(
            session_id=session_id,
            checkout_id=checkout.checkout_id,
            resume_token_hash=hashlib.sha256(session_id.encode()).hexdigest(),
            provider="fake",
            model="fake",
            workspace_id=workspace.workspace_id,
        )
    with store.connection() as connection:
        connection.execute("UPDATE sessions SET state = 'active'")
    return BatchEnvironment(
        repository,
        store,
        SharedBatchPublisher(store, tmp_path / "candidates", threading.RLock()),
        workspace.workspace_id,
    )


def _events(environment: BatchEnvironment) -> int:
    with environment.store.connection() as connection:
        return int(
            connection.execute(
                "SELECT count(*) FROM checkout_events "
                "WHERE event_type = 'workspace.batch_published'"
            ).fetchone()[0]
        )


def test_batch_publishes_once_without_touching_git(
    batch_env: BatchEnvironment, git_run: Callable[..., str]
) -> None:
    env = batch_env
    baseline = tuple(
        git_run(env.repository, *args)
        for args in (("rev-parse", "HEAD"), ("write-tree",), ("show-ref",))
    )
    files = env.files()
    result = env.publisher.publish("batch", "first", files, ("*",))
    assert result.state == "published"
    assert result.workspace_revision == 1
    assert result.paths == ("a.txt", "docs/b.txt")
    assert all(
        (env.repository / item.relative_path).read_bytes() == item.content
        for item in files
    )
    assert env.publisher.publish("batch", "first", files, ("*",)) == result
    assert env.publisher.get("batch") == result
    assert _events(env) == 1
    assert not env.publisher.blocks_workspace(env.workspace_id)
    assert (
        tuple(
            git_run(env.repository, *args)
            for args in (("rev-parse", "HEAD"), ("write-tree",), ("show-ref",))
        )
        == baseline
    )


@pytest.mark.parametrize(
    "changed", ["content", "session", "scopes", "base", "order", "mode", "case"]
)
def test_batch_id_is_immutable(batch_env: BatchEnvironment, changed: str) -> None:
    env = batch_env
    files = env.files()
    env.publisher.publish("batch", "first", files, ("*",))
    session = "second" if changed == "session" else "first"
    scopes = ("a.txt", "docs/") if changed == "scopes" else ("*",)
    if changed == "content":
        files = (replace(files[0], content=b"another"), files[1])
    elif changed == "base":
        files = (replace(files[0], base=ABSENT), files[1])
    elif changed == "order":
        files = tuple(reversed(files))
    elif changed == "mode":
        files = (replace(files[0], mode=EXECUTABLE_MODE), files[1])
    with pytest.raises(WorkspaceBrokerError, match="bound to another"):
        env.publisher.publish("batch", session, files, scopes, changed == "case")


def test_conflict_preserves_whole_candidate_and_writes_nothing(
    batch_env: BatchEnvironment,
) -> None:
    env = batch_env
    first_files = env.files()
    second_files = tuple(
        replace(item, content=b"second " + item.content) for item in first_files
    )
    env.publisher.publish("first", "first", first_files, ("*",))
    result = env.publisher.publish("second", "second", second_files, ("*",))
    assert result.state == "diverged"
    assert result.workspace_revision is None
    assert _events(env) == 1
    with env.store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM workspace_batches WHERE batch_id = 'second'"
        ).fetchone()
    request = json.loads(row["request_json"])
    observations = json.loads(row["observations_json"])
    assert set(observations) == {"a.txt", "docs/b.txt"}
    for item, entry in zip(second_files, request["files"], strict=True):
        assert (
            load_candidate_content(env.publisher.candidate_dir, entry["content_hash"])
            == item.content
        )
    assert all(
        (env.repository / item.relative_path).read_bytes() == item.content
        for item in first_files
    )
    assert not env.publisher.blocks_workspace(env.workspace_id)
    assert env.publisher.recover() == []


@pytest.mark.parametrize("crash_after", [0, 1, 2])
def test_restart_rolls_forward_every_crash_boundary_once(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch, crash_after: int
) -> None:
    env = batch_env
    files = env.files()
    original = batches.atomic_replace_regular_file
    calls = 0

    def interrupted(**kwargs: Any) -> None:
        nonlocal calls
        if calls == crash_after:
            raise RuntimeError("simulated process death")
        original(**kwargs)
        calls += 1
        if calls == 2 and crash_after == 2:
            raise RuntimeError("simulated process death")

    with monkeypatch.context() as patch:
        patch.setattr(batches, "atomic_replace_regular_file", interrupted)
        with pytest.raises(RuntimeError, match="process death"):
            env.publisher.publish("crash", "first", files, ("*",))
    assert env.publisher.blocks_workspace(env.workspace_id)
    assert _events(env) == 0
    restored = SharedBatchPublisher(
        env.store, env.publisher.candidate_dir, threading.RLock()
    )
    (result,) = restored.recover("crash")
    assert result.state == "published"
    assert result.workspace_revision == 1
    assert _events(env) == 1
    assert all(
        (env.repository / item.relative_path).read_bytes() == item.content
        for item in files
    )
    assert restored.recover() == []
    assert restored.publish("crash", "first", files, ("*",)) == result


def test_third_state_blocks_workspace_until_explicit_recovery(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env
    files = env.files()
    original = batches.atomic_replace_regular_file
    calls = 0

    def interrupted(**kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("crash")
        original(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(batches, "atomic_replace_regular_file", interrupted)
        with pytest.raises(RuntimeError):
            env.publisher.publish("crash", "first", files, ("*",))
    (env.repository / "docs/b.txt").write_bytes(b"external edit")
    (result,) = env.publisher.recover()
    assert result.state == "operator_attention"
    assert (env.repository / "docs/b.txt").read_bytes() == b"external edit"
    assert _events(env) == 0
    with pytest.raises(WorkspaceBrokerError, match="unresolved batch"):
        env.publisher.publish("blocked", "second", env.files(), ("*",))
    (env.repository / "docs/b.txt").write_bytes(b"old b\n")
    # An ordinary identical retry cannot quietly resolve operator attention.
    assert (
        env.publisher.publish("crash", "first", files, ("*",)).state
        == "operator_attention"
    )
    assert env.publisher.blocks_workspace(env.workspace_id)
    assert env.publisher.recover("crash")[0].state == "published"
    assert not env.publisher.blocks_workspace(env.workspace_id)


def test_materialized_results_are_verified_before_revision(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env
    original = batches.atomic_replace_regular_file

    def external_edit(**kwargs: Any) -> None:
        original(**kwargs)
        if kwargs["relative_path"] == "docs/b.txt":
            (env.repository / "a.txt").write_bytes(b"external after first rename")

    monkeypatch.setattr(batches, "atomic_replace_regular_file", external_edit)
    result = env.publisher.publish("batch", "first", env.files(), ("*",))
    assert result.state == "operator_attention"
    assert result.workspace_revision is None
    assert _events(env) == 0
    assert env.publisher.blocks_workspace(env.workspace_id)


def test_equal_base_and_result_still_gets_one_verified_publication(
    batch_env: BatchEnvironment,
) -> None:
    env = batch_env
    files = tuple(
        replace(item, content=(env.repository / item.relative_path).read_bytes())
        for item in env.files()
    )
    assert (
        env.publisher.publish("unchanged", "first", files, ("*",)).state == "published"
    )
    assert _events(env) == 1


def test_new_file_and_executable_result(batch_env: BatchEnvironment) -> None:
    env = batch_env
    files = (BatchFile("new.sh", ABSENT, b"#!/bin/sh\n", EXECUTABLE_MODE), *env.files())
    assert env.publisher.publish("new", "first", files, ("*",)).state == "published"
    assert identify_path(env.repository / "new.sh").mode == EXECUTABLE_MODE


@pytest.mark.parametrize(
    "invalid",
    [
        "scope",
        "traversal",
        "admin",
        "duplicate",
        "alias",
        "parent",
        "binary",
        "oversize",
        "many",
        "aggregate",
        "base",
    ],
)
def test_unsupported_inputs_fail_before_checkout_writes(
    batch_env: BatchEnvironment, invalid: str
) -> None:
    env = batch_env
    files = env.files()
    scopes = ("docs/",) if invalid == "scope" else ("*",)
    if invalid == "traversal":
        files = (replace(files[0], relative_path="../escape"),)
    elif invalid == "admin":
        files = (replace(files[0], relative_path=".git/config"),)
    elif invalid == "duplicate":
        files = (files[0], files[0])
    elif invalid == "alias":
        files = (files[0], replace(files[1], relative_path="A.TXT"))
    elif invalid == "parent":
        files = (replace(files[0], relative_path="missing/new.txt", base=ABSENT),)
    elif invalid == "binary":
        files = (replace(files[0], content=b"\xff"),)
    elif invalid == "oversize":
        files = (replace(files[0], content=b"a" * (batches.MAX_SHARED_TEXT_BYTES + 1)),)
    elif invalid == "many":
        files = tuple(
            replace(files[0], relative_path=f"file-{n}", base=ABSENT) for n in range(51)
        )
    elif invalid == "aggregate":
        files = tuple(
            replace(
                files[0],
                relative_path=f"file-{n}",
                base=ABSENT,
                content=b"a" * batches.MAX_SHARED_TEXT_BYTES,
            )
            for n in range(17)
        )
    elif invalid == "base":
        files = (replace(files[0], base=replace(ABSENT, digest="0" * 64)),)
    with pytest.raises(ValueError):
        env.publisher.publish("invalid", "first", files, scopes, invalid == "alias")
    assert (env.repository / "a.txt").read_bytes() == b"old a\n"
    assert (env.repository / "docs/b.txt").read_bytes() == b"old b\n"
    assert _events(env) == 0
    assert env.publisher.get("invalid") is None


def test_inactive_session_is_refused(batch_env: BatchEnvironment) -> None:
    env = batch_env
    with env.store.connection() as connection:
        connection.execute(
            "UPDATE sessions SET state = 'closed' WHERE session_id = 'first'"
        )
    with pytest.raises(WorkspaceBrokerError, match="active shared"):
        env.publisher.publish("batch", "first", env.files(), ("*",))


def test_recovery_reconciles_journal_even_after_session_closes(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env

    def interrupted(**kwargs: Any) -> None:
        raise RuntimeError("crash before rename")

    with monkeypatch.context() as patch:
        patch.setattr(batches, "atomic_replace_regular_file", interrupted)
        with pytest.raises(RuntimeError):
            env.publisher.publish("crash", "first", env.files(), ("*",))
    with env.store.connection() as connection:
        connection.execute("UPDATE sessions SET state = 'closed'")
    assert env.publisher.recover("crash")[0].state == "published"


def test_corrupt_candidate_body_keeps_journal_and_checkout_safe(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env
    files = env.files()

    def interrupted(**kwargs: Any) -> None:
        raise RuntimeError("crash before rename")

    with monkeypatch.context() as patch:
        patch.setattr(batches, "atomic_replace_regular_file", interrupted)
        with pytest.raises(RuntimeError):
            env.publisher.publish("crash", "first", files, ("*",))
    digest = hashlib.sha256(files[1].content).hexdigest()
    (env.publisher.candidate_dir / digest).write_bytes(b"corrupt")
    assert env.publisher.recover("crash")[0].state == "operator_attention"
    assert (env.repository / "a.txt").read_bytes() == b"old a\n"
    assert (env.repository / "docs/b.txt").read_bytes() == b"old b\n"
    assert env.publisher.blocks_workspace(env.workspace_id)
    assert _events(env) == 0


@pytest.mark.parametrize("parent_kind", ["symlink", "nested_repository"])
def test_parent_cannot_redirect_batch_outside_checkout_boundary(
    batch_env: BatchEnvironment, tmp_path: Path, parent_kind: str
) -> None:
    env = batch_env
    if parent_kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        (env.repository / "escape").symlink_to(outside, target_is_directory=True)
    else:
        (env.repository / "escape/.git").mkdir(parents=True)
    candidate = BatchFile("escape/new.txt", ABSENT, b"content", REGULAR_MODE)
    with pytest.raises(WorkspaceBrokerError):
        env.publisher.publish("unsafe", "first", (candidate,), ("*",))
    assert not (env.repository / "escape/new.txt").exists()
    assert env.publisher.get("unsafe") is None


def test_case_policy_cannot_be_weakened_by_caller(batch_env: BatchEnvironment) -> None:
    env = batch_env
    with env.store.connection() as connection:
        connection.execute("UPDATE checkouts SET path_case_insensitive = 1")
    first, second = env.files()
    second = replace(second, relative_path="A.TXT")
    with pytest.raises(WorkspaceBrokerError, match="aliases"):
        env.publisher.publish("alias", "first", (first, second), ("*",))
    assert env.publisher.get("alias") is None


def test_unreadable_or_nontext_preflight_is_durable_divergence(
    batch_env: BatchEnvironment,
) -> None:
    env = batch_env
    files = env.files()
    (env.repository / "docs/b.txt").write_bytes(b"\xff")
    result = env.publisher.publish("binary", "first", files, ("*",))
    assert result.state == "diverged"
    assert (env.repository / "a.txt").read_bytes() == b"old a\n"
    assert (env.repository / "docs/b.txt").read_bytes() == b"\xff"
    assert _events(env) == 0


@pytest.mark.parametrize(
    "operation", ["link_create", "link_retarget", "link_delete", "binary"]
)
def test_typed_publication_crash_recovery_and_reverse_batch(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from llm_cli.workspace.batches import result_identity
    from llm_cli.workspace.identity import SYMLINK_MODE

    env = batch_env
    path = env.repository / "typed"
    original: bytes | None = None
    if operation in {"link_retarget", "link_delete"}:
        path.symlink_to("a.txt")
        original = b"a.txt"
    elif operation == "binary":
        original = b"\x00\xffbefore"
        path.write_bytes(original)
    content = (
        None
        if operation == "link_delete"
        else b"\xff\x00after"
        if operation == "binary"
        else b"docs/b.txt"
    )
    mode = (
        ""
        if content is None
        else REGULAR_MODE
        if operation == "binary"
        else SYMLINK_MODE
    )
    candidate = BatchFile(
        "typed",
        identify_path(path),
        content,
        mode,
        original,
        binary=operation == "binary",
    )
    with monkeypatch.context() as patch:

        def crash(*args: Any) -> None:
            raise RuntimeError("crash before journal confirmation")

        patch.setattr(env.publisher, "_confirm", crash)
        with pytest.raises(RuntimeError):
            env.publisher.publish("typed", "first", (candidate,), ("*",))
    assert env.publisher.recover("typed")[0].state == "published"
    assert identify_path(path) == result_identity(candidate)
    inverse = BatchFile(
        "typed",
        result_identity(candidate),
        original,
        candidate.base.mode,
        content,
        binary=operation == "binary",
    )
    assert (
        env.publisher.publish("undo-typed", "first", (inverse,), ("*",)).state
        == "published"
    )
    assert identify_path(path) == candidate.base


@pytest.mark.parametrize(
    "target", [b"../outside", b"/tmp/outside", b".git/config", b"jump/file"]
)
def test_symlink_publisher_rejects_escaping_or_chained_targets(
    batch_env: BatchEnvironment, target: bytes
) -> None:
    from llm_cli.workspace.identity import SYMLINK_MODE

    env = batch_env
    (env.repository / "jump").symlink_to(env.repository.parent)
    item = BatchFile("link", ABSENT, target, SYMLINK_MODE)
    with pytest.raises(WorkspaceBrokerError):
        env.publisher.publish("escape", "first", (item,), ("*",))
    assert not (env.repository / "link").is_symlink()


def test_symlink_recovery_refuses_changed_target_parent(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.workspace.identity import SYMLINK_MODE

    env = batch_env
    item = BatchFile("link", ABSENT, b"docs/b.txt", SYMLINK_MODE)
    with monkeypatch.context() as patch:
        patch.setattr(
            batches,
            "atomic_replace_symlink",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("crash")),
        )
        with pytest.raises(RuntimeError):
            env.publisher.publish("link", "first", (item,), ("*",))
    (env.repository / "docs").rename(env.repository / "old-docs")
    (env.repository / "docs").symlink_to(env.repository.parent)
    assert env.publisher.recover("link")[0].state == "operator_attention"
    assert not (env.repository / "link").is_symlink()


@pytest.mark.parametrize("kind", ["binary", "symlink"])
def test_typed_late_base_race_never_overwrites_new_state(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from llm_cli.workspace.identity import SYMLINK_MODE

    env = batch_env
    path = env.repository / "late"
    if kind == "binary":
        path.write_bytes(b"\x00before")
        original = b"\x00before"
        after = b"\xffafter"
        mode = REGULAR_MODE
        function = "atomic_replace_regular_file"
    else:
        path.symlink_to("a.txt")
        original = b"a.txt"
        after = b"docs/b.txt"
        mode = SYMLINK_MODE
        function = "atomic_replace_symlink"
    item = BatchFile(
        "late", identify_path(path), after, mode, original, binary=kind == "binary"
    )
    apply = getattr(batches, function)

    def race(**kwargs: Any) -> None:
        path.unlink()
        if kind == "binary":
            path.write_bytes(b"external edit")
        else:
            path.symlink_to("third-target")
        apply(**kwargs)

    monkeypatch.setattr(batches, function, race)
    assert (
        env.publisher.publish("late", "first", (item,), ("*",)).state
        == "operator_attention"
    )
    if kind == "binary":
        assert path.read_bytes() == b"external edit"
    else:
        assert path.readlink() == Path("third-target")
    assert _events(env) == 0


def test_symlink_parent_swap_during_staging_never_writes_outside(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.workspace import broker
    from llm_cli.workspace.identity import SYMLINK_MODE

    env = batch_env
    outside = env.repository.parent / "outside"
    outside.mkdir()
    item = BatchFile("docs/link", ABSENT, b"b.txt", SYMLINK_MODE)
    create = broker.os.symlink

    def swap(source: Any, target: Any, *args: Any, **kwargs: Any) -> None:
        create(source, target, *args, **kwargs)
        if str(target).endswith(".tmp"):
            (env.repository / "docs").rename(env.repository / "saved-docs")
            create(outside, env.repository / "docs")

    monkeypatch.setattr(broker.os, "symlink", swap)
    assert (
        env.publisher.publish("swap", "first", (item,), ("*",)).state
        == "operator_attention"
    )
    assert list(outside.iterdir()) == []
    assert not (env.repository / "saved-docs/link").is_symlink()


def test_symlink_scope_and_protected_target_revalidated_on_recovery(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.workspace.identity import SYMLINK_MODE

    env = batch_env
    item = BatchFile("docs/link", ABSENT, b"b.txt", SYMLINK_MODE)
    with monkeypatch.context() as patch:
        patch.setattr(
            batches,
            "atomic_replace_symlink",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("crash")),
        )
        with pytest.raises(RuntimeError):
            env.publisher.publish("link", "first", (item,), ("docs/",))
    # An ignored target is runtime-only even though the link text still fits scope.
    (env.repository / ".gitignore").write_text("docs/b.txt\n")
    # Tracked files remain source despite ignore patterns; make this one untracked.
    from llm_cli.git.environment import run_git

    run_git(env.repository, ["rm", "--cached", "docs/b.txt"])
    assert env.publisher.recover("link")[0].state == "operator_attention"
    assert not (env.repository / "docs/link").is_symlink()


@pytest.mark.parametrize("kind", ["binary", "symlink"])
def test_parent_swap_at_atomic_install_cannot_redirect_typed_write(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from llm_cli.workspace import broker
    from llm_cli.workspace.identity import SYMLINK_MODE

    env = batch_env
    outside = env.repository.parent / "outside"
    outside.mkdir()
    (outside / "new").write_bytes(b"outside sentinel")
    item = BatchFile(
        "docs/new",
        ABSENT,
        b"b.txt" if kind == "symlink" else b"\x00binary",
        SYMLINK_MODE if kind == "symlink" else REGULAR_MODE,
        binary=kind == "binary",
    )
    install = broker.os.replace

    def swap(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        if str(source).endswith(".tmp") and "llm-coord-stage" in str(source):
            (env.repository / "docs").rename(env.repository / "saved-docs")
            (env.repository / "docs").symlink_to(outside)
        install(source, destination, *args, **kwargs)

    monkeypatch.setattr(broker.os, "replace", swap)
    assert (
        env.publisher.publish("install-race", "first", (item,), ("*",)).state
        == "operator_attention"
    )
    assert (outside / "new").read_bytes() == b"outside sentinel"
    assert not (outside / "new").is_symlink()


@pytest.mark.parametrize("kind", ["binary", "symlink"])
def test_parent_swap_at_delete_cannot_remove_outside_file(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from llm_cli.workspace import broker

    env = batch_env
    target = env.repository / "docs/gone"
    if kind == "binary":
        target.write_bytes(b"\x00before")
        before = b"\x00before"
    else:
        target.symlink_to("b.txt")
        before = b"b.txt"
    outside = env.repository.parent / "outside"
    outside.mkdir()
    (outside / "gone").write_bytes(b"outside sentinel")
    item = BatchFile(
        "docs/gone", identify_path(target), None, "", before, binary=kind == "binary"
    )
    unlink = broker.os.unlink

    def swap(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path) == "gone" and "dir_fd" in kwargs:
            (env.repository / "docs").rename(env.repository / "saved-docs")
            (env.repository / "docs").symlink_to(outside)
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(broker.os, "unlink", swap)
    assert (
        env.publisher.publish("delete-race", "first", (item,), ("*",)).state
        == "operator_attention"
    )
    assert (outside / "gone").read_bytes() == b"outside sentinel"


@pytest.mark.parametrize("partial", [False, True])
def test_delete_refuses_to_publish_over_an_edit_after_model_read(
    batch_env: BatchEnvironment, partial: bool
) -> None:
    env = batch_env
    target = env.repository / "a.txt"
    target.write_text("first line\nsecond line\n")
    broker = SharedToolBroker(worktree=env.repository, scopes=("*",))
    options: dict[str, object] = {"path": "a.txt"}
    if partial:
        options.update(start_line=1, end_line=1)
    assert not broker.invoke("read_file", options).is_error
    target.write_text("user saved a newer version\n")
    current = identify_path(target)
    assert not broker.invoke("delete_file", {"path": "a.txt"}).is_error
    published = env.publisher.publish(
        "stale-delete", "first", broker.candidates(), ("*",)
    )
    assert published.state == "diverged"
    assert identify_path(target) == current
    assert target.read_text() == "user saved a newer version\n"
    assert _events(env) == 0


@pytest.mark.parametrize(
    "operation",
    [
        "index.lock",
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "rebase-merge",
        "rebase-apply",
    ],
)
def test_git_operation_refuses_publication_retryably(
    batch_env: BatchEnvironment, operation: str
) -> None:
    from llm_cli.errors import ErrorCode, LlmCoordError

    marker = batch_env.repository / ".git" / operation
    if operation.startswith("rebase-"):
        marker.mkdir()
    else:
        marker.write_text("active\n")
    with pytest.raises(LlmCoordError) as refusal:
        batch_env.publisher.publish("git-guard", "first", batch_env.files(), ("*",))
    assert refusal.value.code is ErrorCode.GIT_OPERATION_ACTIVE
    assert refusal.value.details and refusal.value.details["retryable"] is True
    assert (batch_env.repository / "a.txt").read_text() == "old a\n"
    assert batch_env.publisher.get("git-guard") is None
    if marker.is_dir():
        marker.rmdir()
    else:
        marker.unlink()
    assert (
        batch_env.publisher.publish(
            "git-guard", "first", batch_env.files(), ("*",)
        ).state
        == "published"
    )


def test_staged_path_notice_and_strict_refusal(
    batch_env: BatchEnvironment, git_run: Callable[..., str]
) -> None:
    from llm_cli.errors import ErrorCode, LlmCoordError

    env = batch_env
    (env.repository / "a.txt").write_text("user staged\n")
    git_run(env.repository, "add", "a.txt")
    index = git_run(env.repository, "write-tree")
    env.publisher.refuse_staged_paths = True
    with pytest.raises(LlmCoordError) as refused:
        env.publisher.publish("strict", "first", env.files(), ("*",))
    assert refused.value.code is ErrorCode.INDEX_PATH_STAGED
    assert env.publisher.get("strict") is None
    env.publisher.refuse_staged_paths = False
    assert (
        env.publisher.publish("advisory", "first", env.files(), ("*",)).state
        == "published"
    )
    assert git_run(env.repository, "write-tree") == index
    with env.store.connection() as con:
        notice = con.execute(
            "SELECT payload_json FROM checkout_events WHERE "
            "event_type='git.staged_paths_notice'"
        ).fetchone()
    assert notice is not None and json.loads(notice[0])["paths"] == ["a.txt"]


def test_unmerged_path_is_always_refused(
    batch_env: BatchEnvironment, git_run: Callable[..., str]
) -> None:
    import subprocess

    from llm_cli.errors import ErrorCode, LlmCoordError

    env = batch_env
    oid = git_run(env.repository, "rev-parse", "HEAD:a.txt")
    subprocess.run(
        ["git", "update-index", "--index-info"],
        cwd=env.repository,
        input=(
            f"0 {'0' * 40}\ta.txt\n"
            f"100644 {oid} 1\ta.txt\n100644 {oid} 2\ta.txt\n"
            f"100644 {oid} 3\ta.txt\n"
        ),
        text=True,
        check=True,
    )
    with pytest.raises(LlmCoordError) as refused:
        env.publisher.publish("conflicted", "first", env.files(), ("*",))
    assert refused.value.code is ErrorCode.INDEX_PATH_UNMERGED
    assert env.publisher.get("conflicted") is None
    assert (env.repository / "a.txt").read_text() == "old a\n"


def test_plain_commit_observed_without_epoch_or_duplicate_batches(
    batch_env: BatchEnvironment, git_run: Callable[..., str]
) -> None:
    from llm_cli.workspace.gitstate import reconcile

    env = batch_env
    env.publisher.publish("committed", "first", env.files(), ("*",))
    git_run(env.repository, "add", "-A")
    git_run(env.repository, "commit", "-m", "publish changes")
    head = git_run(env.repository, "rev-parse", "HEAD")
    reconcile(env.store, env.repository, candidate_dir=env.publisher.candidate_dir)
    reconcile(env.store, env.repository, candidate_dir=env.publisher.candidate_dir)
    with env.store.connection() as con:
        assert (
            con.execute(
                "SELECT workspace_epoch FROM workspaces WHERE workspace_id=?",
                (env.workspace_id,),
            ).fetchone()[0]
            == 1
        )
        events = con.execute(
            "SELECT payload_json FROM checkout_events WHERE "
            "event_type='git.commit_observed'"
        ).fetchall()
        assert len(events) == 1
        assert json.loads(events[0][0])["batch_ids"] == ["committed"]
        assert con.execute("SELECT count(*) FROM workspace_batches").fetchone()[0] == 1
        assert tuple(
            con.execute(
                "SELECT batch_id,commit_oid FROM workspace_git_batch_commits"
            ).fetchone()
        ) == ("committed", head)


def _git_events(env: BatchEnvironment, kind: str) -> list[dict[str, Any]]:
    with env.store.connection() as con:
        return [
            json.loads(row[0])
            for row in con.execute(
                "SELECT payload_json FROM checkout_events WHERE event_type=? "
                "ORDER BY sequence",
                (kind,),
            )
        ]


def test_later_commit_does_not_relink_an_earlier_batch(
    batch_env: BatchEnvironment, git_run: Callable[..., str]
) -> None:
    from llm_cli.workspace.gitstate import reconcile

    env = batch_env
    env.publisher.publish("one", "first", env.files(), ("*",))
    git_run(env.repository, "add", "-A")
    git_run(env.repository, "commit", "-m", "first")
    first = git_run(env.repository, "rev-parse", "HEAD")
    reconcile(env.store, env.repository, candidate_dir=env.publisher.candidate_dir)
    added = BatchFile(
        "c.txt", identify_path(env.repository / "c.txt"), b"c\n", REGULAR_MODE
    )
    env.publisher.publish("two", "first", (added,), ("*",))
    git_run(env.repository, "add", "-A")
    git_run(env.repository, "commit", "-m", "second")
    second = git_run(env.repository, "rev-parse", "HEAD")
    reconcile(env.store, env.repository, candidate_dir=env.publisher.candidate_dir)
    with env.store.connection() as con:
        links = set(
            con.execute("SELECT batch_id,commit_oid FROM workspace_git_batch_commits")
        )
    assert {tuple(link) for link in links} == {("one", first), ("two", second)}
    assert [e["batch_ids"] for e in _git_events(env, "git.commit_observed")] == [
        ["one"],
        ["two"],
    ]


def test_batch_already_in_the_previous_commit_is_not_linked_again(
    batch_env: BatchEnvironment, git_run: Callable[..., str]
) -> None:
    from llm_cli.workspace.gitstate import reconcile

    env = batch_env
    # The result equals the committed bytes, so a later commit doesn't add it.
    same = BatchFile(
        "a.txt", identify_path(env.repository / "a.txt"), b"old a\n", REGULAR_MODE
    )
    env.publisher.publish("unchanged", "first", (same,), ("*",))
    git_run(env.repository, "commit", "--allow-empty", "-m", "empty")
    reconcile(env.store, env.repository, candidate_dir=env.publisher.candidate_dir)
    assert _git_events(env, "git.commit_observed")[0]["batch_ids"] == []


@pytest.mark.parametrize("move", ["commit", "amend", "no_reflog"])
def test_committing_the_users_own_edits_is_not_a_checkout_change(
    batch_env: BatchEnvironment, git_run: Callable[..., str], move: str
) -> None:
    from llm_cli.workspace.gitstate import reconcile

    env = batch_env
    env.publisher.publish("one", "first", env.files(), ("*",))
    (env.repository / "user.txt").write_text("user work\n")
    (env.repository / "a.txt").write_text("user edit\n")
    git_run(env.repository, "add", "-A")
    if move == "amend":
        git_run(env.repository, "commit", "--amend", "-m", "amended")
    else:
        git_run(env.repository, "commit", "-m", "user work")
    if move == "no_reflog":
        (env.repository / ".git" / "logs" / "HEAD").unlink()
    reconcile(env.store, env.repository, candidate_dir=env.publisher.candidate_dir)
    with env.store.connection() as con:
        epoch = con.execute(
            "SELECT workspace_epoch FROM workspaces WHERE workspace_id=?",
            (env.workspace_id,),
        ).fetchone()[0]
    if move == "no_reflog":
        # Without the reflog, changed files stay conservatively structural.
        assert epoch == 2
        assert len(_git_events(env, "git.checkout_changed")) == 2
    else:
        assert epoch == 1
        assert _git_events(env, "git.checkout_changed") == []
        assert len(_git_events(env, "git.commit_observed")) == 1


def test_head_move_behind_an_unresolved_journal_names_the_journal(
    batch_env: BatchEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    git_run: Callable[..., str],
) -> None:
    env = batch_env
    original = batches.atomic_replace_regular_file
    calls = 0

    def interrupted(**kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("crash")
        original(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(batches, "atomic_replace_regular_file", interrupted)
        with pytest.raises(RuntimeError):
            env.publisher.publish("crash", "first", env.files(), ("*",))
    (env.repository / "docs/b.txt").write_bytes(b"external edit")
    assert env.publisher.recover()[0].state == "operator_attention"
    git_run(env.repository, "commit", "--allow-empty", "-m", "moved")
    with pytest.raises(WorkspaceBrokerError, match="unresolved batch"):
        env.publisher.publish("blocked", "second", env.files(), ("*",))


def test_branch_switch_notifies_all_sessions_and_revalidates_old_base(
    batch_env: BatchEnvironment, git_run: Callable[..., str]
) -> None:
    from llm_cli.workspace.context import SharedCoordinationContext
    from llm_cli.workspace.external import ExternalWorkspaceScanner
    from llm_cli.workspace.gitstate import reconcile

    env = batch_env
    old = env.files()
    git_run(env.repository, "checkout", "-b", "other")
    (env.repository / "a.txt").write_text("other branch\n")
    git_run(env.repository, "add", "a.txt")
    git_run(env.repository, "commit", "-m", "other")
    git_run(env.repository, "checkout", "main")
    reconcile(env.store, env.repository)
    git_run(env.repository, "checkout", "other")
    ExternalWorkspaceScanner(env.store).scan_session("first")
    with env.store.connection() as con:
        assert (
            con.execute(
                "SELECT workspace_epoch FROM workspaces WHERE workspace_id=?",
                (env.workspace_id,),
            ).fetchone()[0]
            == 2
        )
        rows = con.execute(
            "SELECT payload_json FROM checkout_events WHERE "
            "event_type='git.checkout_changed'"
        ).fetchall()
        assert {json.loads(row[0])["affected_session_id"] for row in rows} == {
            "first",
            "second",
        }
    for session in ("first", "second"):
        update = SharedCoordinationContext(env.store, session, ("*",))(None)
        assert update.text and "git.checkout_changed" in update.text
        assert "Re-read files" in update.text
    assert env.publisher.publish("old-epoch", "first", old, ("*",)).state == "diverged"
    assert (env.repository / "a.txt").read_text() == "other branch\n"


def test_git_change_during_apply_records_attention_and_forces_rescan(
    batch_env: BatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.errors import ErrorCode, LlmCoordError

    env = batch_env
    original = batches.atomic_replace_regular_file
    marker = env.repository / ".git" / "index.lock"

    def during(**kwargs: Any) -> None:
        original(**kwargs)
        marker.write_text("Git started during publication\n")

    monkeypatch.setattr(batches, "atomic_replace_regular_file", during)
    assert (
        env.publisher.publish("racing", "first", env.files(), ("*",)).state
        == "published"
    )
    with env.store.connection() as con:
        assert (
            con.execute("SELECT rescan_required FROM workspace_git_state").fetchone()[0]
            == 1
        )
        attention = con.execute(
            "SELECT payload_json FROM checkout_events WHERE "
            "event_type='git.concurrent_operation_attention'"
        ).fetchone()
        assert attention and json.loads(attention[0])["paths"] == [
            "a.txt",
            "docs/b.txt",
        ]
    with pytest.raises(LlmCoordError) as refused:
        env.publisher.publish("next", "second", env.files(), ("*",))
    assert refused.value.code is ErrorCode.GIT_OPERATION_ACTIVE
    marker.unlink()
    monkeypatch.setattr(batches, "atomic_replace_regular_file", original)
    assert (
        env.publisher.publish("next", "second", env.files(), ("*",)).state
        == "published"
    )
    with env.store.connection() as con:
        assert (
            con.execute("SELECT rescan_required FROM workspace_git_state").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "failure", [OSError, ValueError, __import__("sqlite3").OperationalError]
)
def test_git_postflight_failure_cannot_fail_confirmed_publication(
    batch_env: BatchEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception],
) -> None:
    from llm_cli.workspace import gitstate

    env = batch_env
    original = gitstate._postflight

    def fail(*args: Any, **kwargs: Any) -> None:
        raise failure("injected advisory observation failure")

    monkeypatch.setattr(gitstate, "_postflight", fail)
    assert (
        env.publisher.publish("confirmed", "first", env.files(), ("*",)).state
        == "published"
    )
    assert env.repository in gitstate._FAILED[env.store]
    monkeypatch.setattr(gitstate, "_postflight", original)
    scans = 0
    files = gitstate._files

    def scan(root: Path) -> dict[str, list[int] | None]:
        nonlocal scans
        scans += 1
        return files(root)

    monkeypatch.setattr(gitstate, "_files", scan)
    assert (
        env.publisher.publish("retry", "first", env.files(), ("*",)).state
        == "published"
    )
    assert scans == 1
    assert env.repository not in gitstate._FAILED.get(env.store, set())


def test_publisher_uses_durable_case_policy_for_staged_alias(
    batch_env: BatchEnvironment, git_run: Callable[..., str]
) -> None:
    from llm_cli.errors import ErrorCode, LlmCoordError

    env = batch_env
    (env.repository / "a.txt").write_text("staged\n")
    git_run(env.repository, "add", "a.txt")
    with env.store.connection() as con:
        con.execute("UPDATE checkouts SET path_case_insensitive=1")
    env.publisher.refuse_staged_paths = True
    files = env.files()
    alias = (replace(files[0], relative_path="A.txt"), *files[1:])
    with pytest.raises(LlmCoordError) as refusal:
        env.publisher.publish("staged-alias", "first", alias, ("*",))
    assert refusal.value.code is ErrorCode.INDEX_PATH_STAGED
    assert env.publisher.get("staged-alias") is None
