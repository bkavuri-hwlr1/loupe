"""Physical ownership is selected by kernel identity, never a profile path."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.inspect import inspect_repository
from llm_cli.storage.control import ControlStore
from llm_cli.workspace import authority as module
from llm_cli.workspace.authority import (
    PhysicalAuthority,
    PhysicalCheckout,
    account_authority_root,
    managed_git_operation,
    process_start_identity,
)
from llm_cli.workspace.installation import private_read, private_write


def _owner(tmp_path: Path, repository: Path, name: str) -> PhysicalAuthority:
    directory = tmp_path / name
    directory.mkdir(mode=0o700, exist_ok=True)
    store = ControlStore(directory / "control.sqlite3")
    store.initialize()
    info = inspect_repository(repository, profile_id=name)
    registered = store.register_repository(
        repo_key=info.repo_key,
        display_name=name,
        git_common_dir=str(info.common_git_dir),
        main_worktree_path=str(info.main_worktree),
        target_ref=info.target_ref,
        profile_id=name,
    )
    checkout = store.ensure_checkout(
        repository=registered,
        canonical_path=str(repository.resolve()),
        git_common_dir=str(info.common_git_dir),
    )
    store.ensure_shared_workspace(checkout)
    result = PhysicalAuthority(
        store,
        name,
        "boot-" + name,
        directory / "socket",
        root=tmp_path / "authority" / "v1",
    )
    store.authority = result
    return result


def _marker(owner: PhysicalAuthority, repository: Path) -> Path:
    physical = PhysicalCheckout.inspect(repository)
    try:
        return owner.root / "checkouts" / physical.key / "marker.json"
    finally:
        physical.close()


def test_account_root_ignores_all_environment_and_profile_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        module.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_dir=str(tmp_path / "account")),
    )
    for key in (
        "HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "LLM_COORD_DATA_HOME",
        "LLM_COORD_RUNTIME_DIR",
        "LLM_COORD_AUTHORITY_ROOT",
    ):
        monkeypatch.setenv(key, str(tmp_path / "wrong"))
    assert (
        account_authority_root()
        == tmp_path / "account/.llm-coord/physical-authority/v1"
    )
    assert process_start_identity(os.getpid()) == process_start_identity(os.getpid())


def test_aliases_contend_but_clones_and_linked_worktrees_have_correct_domains(
    tmp_path: Path, repository_factory: Callable[..., Path], git_run: Callable[..., str]
) -> None:
    repository = repository_factory(tmp_path, {"source.py": "VALUE = 1\n"})
    alias = tmp_path / "alias"
    alias.symlink_to(repository, target_is_directory=True)
    linked = tmp_path / "linked"
    git_run(repository, "worktree", "add", "--detach", str(linked), "HEAD")
    clone = tmp_path / "clone"
    git_run(repository, "clone", str(repository), str(clone))
    objects = [
        PhysicalCheckout.inspect(path) for path in (repository, alias, linked, clone)
    ]
    try:
        primary, aliased, worktree, independent = objects
        assert primary.key == aliased.key
        assert worktree.key != primary.key
        assert worktree.store_key == primary.store_key
        assert independent.key != primary.key
        assert independent.store_key != primary.store_key
    finally:
        for item in objects:
            item.close()
    first, second = (
        _owner(tmp_path, repository, "first"),
        _owner(tmp_path, repository, "second"),
    )
    try:
        with first.pin(repository):
            with pytest.raises(LlmCoordError, match="another profile") as refusal:
                second.acquire(alias)
            assert refusal.value.code is ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE
        with second.pin(clone):
            pass
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("location", ["authority", "root", "admin", "common"])
def test_unverified_filesystem_refuses_mutation(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    repository = repository_factory(tmp_path, {"file": "base"})
    owner = _owner(tmp_path, repository, "first")
    target = (
        owner.root
        if location == "authority"
        else repository
        if location == "root"
        else repository / ".git"
    )
    original = module._filesystem
    monkeypatch.setattr(
        module, "_filesystem", lambda path: "nfs" if path == target else original(path)
    )
    try:
        with pytest.raises(LlmCoordError) as refused:
            owner.acquire(repository)
        assert refused.value.code is ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
    finally:
        owner.close()


@pytest.mark.parametrize(
    "damage", ["missing", "checksum", "version", "permissions", "symlink"]
)
def test_bound_marker_storage_cannot_be_silently_reinitialized(
    tmp_path: Path, repository_factory: Callable[..., Path], damage: str
) -> None:
    repository = repository_factory(tmp_path, {"file": "base"})
    owner = _owner(tmp_path, repository, "first")
    with owner.pin(repository):
        pass
    owner.close()
    marker = _marker(owner, repository)
    value = private_read(marker)
    assert value is not None
    if damage == "missing":
        marker.unlink()
    elif damage == "checksum":
        marker.write_text('{"version":1,"checksum":"wrong"}')
    elif damage == "version":
        private_write(marker, {**value, "version": 999})
    elif damage == "permissions":
        marker.chmod(0o644)
    else:
        marker.unlink()
        marker.symlink_to(tmp_path / "unrelated")
    restarted = PhysicalAuthority(
        owner.store, owner.profile, "boot-restarted", owner.socket, root=owner.root
    )
    try:
        with pytest.raises(LlmCoordError) as refused:
            restarted.acquire(repository)
        assert refused.value.code in {
            ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED,
            ErrorCode.CONFIG_INVALID,
        }
        assert not restarted._owned
    finally:
        restarted.close()


def test_database_replacement_or_missing_external_binding_stays_read_only(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    repository = repository_factory(tmp_path, {"file": "base"})
    owner = _owner(tmp_path, repository, "first")
    with owner.pin(repository):
        pass
    owner.close()
    replacement = tmp_path / "replacement.sqlite3"
    with owner.store.connection() as connection, sqlite3.connect(replacement) as other:
        connection.backup(other)
    replacement.chmod(0o600)
    os.replace(replacement, owner.store.path)
    restarted = PhysicalAuthority(
        owner.store, owner.profile, "new-boot", owner.socket, root=owner.root
    )
    try:
        with pytest.raises(LlmCoordError, match="binding"):
            restarted.acquire(repository)
        assert restarted.status(repository)["state"] == "foreign_recovery"
    finally:
        restarted.close()
    owner.installation.record.unlink()
    with pytest.raises(LlmCoordError, match="binding"):
        restarted.acquire(repository)


def test_idle_release_waits_30_seconds_and_handoff_emits_only_paths(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    secret = "runtime-canary-value-592730"
    repository = repository_factory(
        tmp_path, {"file": "ordinary bytes\n", ".gitignore": ".env\n"}
    )
    (repository / ".env").write_text("NONSECRET_NAME=" + secret)
    first, second = (
        _owner(tmp_path, repository, "first"),
        _owner(tmp_path, repository, "second"),
    )
    try:
        with first.pin(repository) as pin:
            acquired = pin.owner.idle_since
            first.release_idle(now=acquired + 31)
            with pytest.raises(LlmCoordError):
                second.acquire(repository)
        idle = pin.owner.idle_since
        first.release_idle(now=idle + 29.99)
        assert first._owned
        first.release_idle(now=idle + 30)
        assert not first._owned
        with second.pin(repository), second.store.connection() as connection:
            event = connection.execute(
                "SELECT payload_json FROM checkout_events WHERE "
                "event_type='checkout.authority_handoff'"
            ).fetchone()
            assert event is not None
            payload = json.loads(event[0])
            assert payload["history_gap"] is True
            assert payload["paths"] == []
            assert (
                connection.execute("SELECT workspace_epoch FROM workspaces").fetchone()[
                    0
                ]
                == 2
            )
        shared = b"".join(path.read_bytes() for path in first.root.rglob("*.json"))
        assert secret.encode() not in shared
        assert hashlib.sha256(secret.encode()).hexdigest().encode() not in shared
        assert b"ordinary bytes" not in shared
    finally:
        first.close()
        second.close()


def test_shutdown_retains_live_worker_pin_and_does_not_trust_pid_metadata(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    repository = repository_factory(tmp_path, {"file": "base"})
    first, second = (
        _owner(tmp_path, repository, "first"),
        _owner(tmp_path, repository, "second"),
    )
    started, finish = threading.Event(), threading.Event()
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            with first.pin(repository):
                started.set()
                assert finish.wait(10)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(5)
    try:
        marker = _marker(first, repository)
        owner_file = marker.with_name("owner.json")
        owner_record = private_read(owner_file)
        assert owner_record is not None
        private_write(
            owner_file,
            {
                **owner_record,
                "pid": os.getpid(),
                "process_start": "reused-pid-with-wrong-start",
            },
        )
        first.close()
        with pytest.raises(LlmCoordError) as refused:
            second.acquire(repository)
        assert refused.value.code is ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE
        assert first._owned
    finally:
        finish.set()
        thread.join(5)
        second.close()
    assert not failures
    assert not first._owned
    with second.pin(repository):
        pass
    second.close()


def test_nested_git_guard_reuses_store_lock_and_parent_dirty_journal(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    repository = repository_factory(tmp_path, {"file": "base"})
    owner = _owner(tmp_path, repository, "first")
    called: list[str] = []

    @managed_git_operation
    def inner() -> None:
        called.append("inner")

    @managed_git_operation
    def outer() -> None:
        inner()
        called.append("outer")

    try:
        with owner.pin(repository):
            outer()
        assert called == ["inner", "outer"]
        assert private_read(_marker(owner, repository))["state"] == "clean"  # type: ignore[index]
    finally:
        owner.close()


def test_reused_path_identity_change_does_not_inherit_authority_history(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    repository = repository_factory(tmp_path, {"file": "base"})
    owner = _owner(tmp_path, repository, "first")
    with owner.pin(repository):
        pass
    owner.close()
    old = tmp_path / "old"
    repository.rename(old)
    shutil.copytree(old, repository)
    restarted = PhysicalAuthority(
        owner.store, owner.profile, "next", owner.socket, root=owner.root
    )
    try:
        with pytest.raises(LlmCoordError) as refused:
            restarted.acquire(repository)
        assert refused.value.code is ErrorCode.CHECKOUT_IDENTITY_CHANGED
        assert restarted.status(repository)["state"] == "identity_changed"
    finally:
        restarted.close()


def test_linked_worktree_processes_share_common_git_store_lock(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
) -> None:
    import subprocess
    import sys

    repository = repository_factory(tmp_path, {"file": "base"})
    linked = tmp_path / "linked"
    git_run(repository, "worktree", "add", "--detach", str(linked), "HEAD")
    owner = _owner(tmp_path, repository, "parent")
    signals = tmp_path / "signals"
    signals.mkdir()
    child_state = tmp_path / "child"
    child_state.mkdir(mode=0o700)
    script = """
import sys
from pathlib import Path
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.authority import PhysicalAuthority
repository, state, root, signals = map(Path, sys.argv[1:])
store = ControlStore(state / 'control.sqlite3'); store.initialize()
owner = PhysicalAuthority(store, 'child', 'child-boot', state / 'socket', root=root)
try:
    with owner.pin(repository) as pin:
        (signals / 'owns-distinct-worktree').touch()
        with owner.common_store(pin.owner):
            (signals / 'entered-common-store').touch()
finally:
    owner.close()
"""
    process: subprocess.Popen[str] | None = None
    try:
        with owner.pin(repository) as pin, owner.common_store(pin.owner):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(linked),
                    str(child_state),
                    str(owner.root),
                    str(signals),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={
                    **os.environ,
                    "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
                },
            )
            deadline = time.monotonic() + 10
            while (
                not (signals / "owns-distinct-worktree").exists()
                and time.monotonic() < deadline
            ):
                assert process.poll() is None
                time.sleep(0.01)
            assert (signals / "owns-distinct-worktree").exists()
            time.sleep(0.15)
            assert not (signals / "entered-common-store").exists()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stdout + stderr
        assert (signals / "entered-common-store").exists()
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
        owner.close()
