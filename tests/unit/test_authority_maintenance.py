"""Authority maintenance retires dead records without clearing ownership."""

from __future__ import annotations

import fcntl
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from test_physical_authority import _marker, _owner
from test_workspace_cutover_cli import _Client

from llm_cli.cli.app import build_parser, dispatch
from llm_cli.protocol.client import DaemonClient
from llm_cli.workspace import authority as module
from llm_cli.workspace.authority import PhysicalAuthority
from llm_cli.workspace.installation import private_read, private_write


def _dead_record(
    tmp_path: Path, factory: Callable[..., Path]
) -> tuple[PhysicalAuthority, Path, Path]:
    repository = factory(tmp_path, {"source.py": "VALUE = 1\n"})
    former = _owner(tmp_path, repository, "former")
    with former.pin(repository):
        marker_path = _marker(former, repository)
    former.close()
    observer = _owner(tmp_path, repository, "observer")
    return observer, repository, marker_path.parent


def test_path_is_diagnostic_and_current_profile_binding_is_never_pruned(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    observer, repository, directory = _dead_record(tmp_path, repository_factory)
    owner = private_read(directory / "owner.json")
    assert owner is not None and owner["checkout_path"] == str(repository.resolve())
    assert (
        observer.records(prune=True, apply=True)["records"][0]["reason"]
        == "checkout_present"
    )
    with observer.pin(repository):
        pass
    observer.close()
    shutil.rmtree(repository)
    record = observer.records(prune=True, apply=True)["records"][0]
    assert record["reason"] == "bound_by_profile" and not record["removed"]
    assert directory.exists()


def test_dry_run_then_apply_removes_only_dead_clean_unbound_record(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    observer, repository, directory = _dead_record(tmp_path, repository_factory)
    shutil.rmtree(repository)
    record = observer.records(prune=True, apply=True)["records"][0]
    assert record["reason"] == "other_profile" and not record["removed"]
    record = observer.records(prune=True, include_other_profiles=True)["records"][0]
    assert record["eligible"] and not record["removed"] and directory.exists()
    record = observer.records(prune=True, apply=True, include_other_profiles=True)[
        "records"
    ][0]
    assert record["removed"] and not directory.exists()
    assert observer.records()["records"] == []


@pytest.mark.parametrize("state", ["dirty", "attention"])
def test_dirty_and_attention_records_are_never_removed(
    tmp_path: Path, repository_factory: Callable[..., Path], state: str
) -> None:
    observer, repository, directory = _dead_record(tmp_path, repository_factory)
    marker = private_read(directory / "marker.json")
    assert marker is not None
    marker["state"] = state
    private_write(directory / "marker.json", marker)
    shutil.rmtree(repository)
    record = observer.records(prune=True, apply=True)["records"][0]
    assert not record["eligible"] and record["reason"] == "recovery_required"
    assert directory.exists()


def test_owned_record_is_never_unlinked(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    observer, repository, directory = _dead_record(tmp_path, repository_factory)
    descriptor = os.open(directory / "ownership.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        shutil.rmtree(repository)
        record = observer.records(prune=True, apply=True)["records"][0]
        assert record["reason"] == "owned" and not record["removed"]
        assert (directory / "ownership.lock").stat().st_ino == os.fstat(
            descriptor
        ).st_ino
    finally:
        os.close(descriptor)


def test_missing_path_requires_explicit_include_unrecorded(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    observer, repository, directory = _dead_record(tmp_path, repository_factory)
    owner = private_read(directory / "owner.json")
    assert owner is not None
    del owner["checkout_path"]
    private_write(directory / "owner.json", owner)
    shutil.rmtree(repository)
    record = observer.records(prune=True, apply=True)["records"][0]
    assert record["reason"] == "unrecorded_path" and not record["removed"]
    assert observer.records(
        prune=True, apply=True, include_unrecorded=True, include_other_profiles=True
    )["records"][0]["removed"]


def test_other_profiles_live_record_needs_explicit_inclusion(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    # A record written before paths were recorded, for a checkout that still
    # exists and that its last owner still binds.
    observer, repository, directory = _dead_record(tmp_path, repository_factory)
    owner = private_read(directory / "owner.json")
    assert owner is not None
    del owner["checkout_path"]
    private_write(directory / "owner.json", owner)
    record = observer.records(prune=True, apply=True, include_unrecorded=True)[
        "records"
    ][0]
    assert record["reason"] == "other_profile" and not record["removed"]
    assert record["owner_profile"] == "former"
    observer.close()
    former = _owner(tmp_path, repository, "former")
    with former.pin(repository):
        pass
    former.close()


def test_own_unbound_record_is_prunable_without_other_profiles(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    repository = repository_factory(tmp_path, {"source.py": "VALUE = 1\n"})
    owner = _owner(tmp_path, repository, "solo")
    with owner.pin(repository):
        directory = _marker(owner, repository).parent
    owner.close()
    shutil.rmtree(repository)
    # The binding disappears (as after deleting checkout rows); the database
    # instance and profile still identify this installation as the last owner.
    with owner.store.connection() as connection:
        connection.execute("DELETE FROM authority_checkouts")
    restarted = PhysicalAuthority(
        owner.store,
        "solo",
        "boot-solo-restarted",
        tmp_path / "solo" / "socket",
        root=tmp_path / "authority" / "v1",
    )
    record = restarted.records(prune=True, apply=True)["records"][0]
    assert record["removed"] and not directory.exists()
    restarted.close()


@pytest.mark.parametrize("damage", ["symlink", "corrupt", "extra"])
def test_uncertain_records_are_preserved(
    tmp_path: Path, repository_factory: Callable[..., Path], damage: str
) -> None:
    observer, repository, directory = _dead_record(tmp_path, repository_factory)
    shutil.rmtree(repository)
    if damage == "symlink":
        saved = tmp_path / "saved-marker"
        (directory / "marker.json").rename(saved)
        (directory / "marker.json").symlink_to(saved)
    elif damage == "corrupt":
        (directory / "marker.json").write_text("{}")
    else:
        (directory / "unknown-journal").write_text("retain")
    record = observer.records(prune=True, apply=True)["records"][0]
    assert not record["eligible"] and directory.exists()


def test_replaced_checkout_has_different_identity_and_is_prunable(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    observer, repository, directory = _dead_record(tmp_path, repository_factory)
    repository.rename(tmp_path / "moved")
    repository_factory(tmp_path, {"other.py": "new identity\n"})
    assert observer.records(prune=True, apply=True, include_other_profiles=True)[
        "records"
    ][0]["removed"]
    assert not directory.exists() and (repository / "other.py").exists()


def test_unsupported_filesystem_reports_supported_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.errors import LlmCoordError
    from llm_cli.storage.control import ControlStore

    store = ControlStore(tmp_path / "control.sqlite3")
    store.initialize()
    owner = PhysicalAuthority(
        store, "test", "boot", tmp_path / "socket", root=tmp_path / "authority"
    )
    monkeypatch.setattr(module, "_filesystem", lambda path: "virtiofs")
    with pytest.raises(LlmCoordError, match=r"virtiofs.*supported:.*apfs") as caught:
        owner._prepare_root()
    assert caught.value.details is not None
    assert caught.value.details["supported_filesystems"] == sorted(
        module.SUPPORTED_FILESYSTEMS
    )


@pytest.mark.parametrize(
    "options,expected",
    [
        (
            ["status"],
            {
                "prune": False,
                "apply": False,
                "include_unrecorded": False,
                "include_other_profiles": False,
            },
        ),
        (
            ["prune"],
            {
                "prune": True,
                "apply": False,
                "include_unrecorded": False,
                "include_other_profiles": False,
            },
        ),
        (
            ["prune", "--apply", "--include-unrecorded", "--include-other-profiles"],
            {
                "prune": True,
                "apply": True,
                "include_unrecorded": True,
                "include_other_profiles": True,
            },
        ),
    ],
)
def test_cli_maintenance_is_explicit(
    options: list[str], expected: dict[str, Any]
) -> None:
    result = {"root": "/private/authority", "records": [], "applied": expected["apply"]}
    client = _Client(result)
    args = build_parser().parse_args(["--json", "workspace", "authority", *options])
    assert dispatch(args, cast(DaemonClient, client)) == result
    assert client.calls == [("workspace.authority.maintenance", expected)]


def test_retirement_serializes_with_new_lock_open(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    observer, repository, directory = _dead_record(tmp_path, repository_factory)
    contender = _owner(tmp_path, repository, "contender")
    owner = private_read(directory / "owner.json")
    assert owner is not None
    owner.pop("checkout_path")
    private_write(directory / "owner.json", owner)
    retiring, release, acquired = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    failures: list[BaseException] = []
    original = Path.unlink

    def pause(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == directory / "owner.json":
            retiring.set()
            assert release.wait(5)
        original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", pause)

    def prune() -> None:
        try:
            assert observer.records(
                prune=True,
                apply=True,
                include_unrecorded=True,
                include_other_profiles=True,
            )["records"][0]["removed"]
        except BaseException as exc:
            failures.append(exc)

    def acquire() -> None:
        try:
            with contender.pin(repository):
                acquired.set()
                assert private_read(directory / "owner.json")["profile"] == "contender"
        except BaseException as exc:
            failures.append(exc)

    pruning = threading.Thread(target=prune)
    pruning.start()
    assert retiring.wait(5)
    opening = threading.Thread(target=acquire)
    opening.start()
    try:
        assert not acquired.wait(0.1)
    finally:
        release.set()
        pruning.join(5)
        opening.join(5)
    assert not pruning.is_alive() and not opening.is_alive()
    assert not failures and acquired.is_set()
    contender.close()


def test_maintenance_rpc_keeps_profile_binding_read_only(
    tmp_path: Path,
    service_factory: Callable[..., Any],
    request_factory: Callable[..., Any],
) -> None:
    import asyncio

    service = service_factory(tmp_path)
    service.initialize()
    request = request_factory("workspace.authority.maintenance", {"prune": True})
    result = asyncio.run(service._dispatch(request))
    assert result["records"] == [] and not result["applied"]
    with service.store.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM authority_checkouts").fetchone()[0]
            == 0
        )
    with pytest.raises(ValueError, match="booleans"):
        asyncio.run(
            service._dispatch(
                request_factory("workspace.authority.maintenance", {"apply": "yes"})
            )
        )
    service.physical_authority.close()


@pytest.mark.parametrize("damage", ["binding", "generation", "journal"])
def test_checksummed_malformed_marker_is_not_pruned(
    tmp_path: Path, repository_factory: Callable[..., Path], damage: str
) -> None:
    observer, repository, directory = _dead_record(tmp_path, repository_factory)
    marker = private_read(directory / "marker.json")
    assert marker is not None
    if damage == "binding":
        marker["binding"] = {}
    elif damage == "generation":
        marker["generation"] = 0
    else:
        marker["journal_id"] = "unfinished"
    private_write(directory / "marker.json", marker)
    shutil.rmtree(repository)
    record = observer.records(prune=True, apply=True)["records"][0]
    assert not record["eligible"] and not record["removed"] and directory.exists()


def test_retained_regime_binding_also_preserves_record(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    observer, repository, directory = _dead_record(tmp_path, repository_factory)
    with observer.store.connection() as connection:
        connection.execute(
            "INSERT INTO checkout_regimes(physical_key,canonical_path,regime,"
            "generation,intent_id,checkpoint,updated_at) "
            "VALUES (?,?,'legacy',0,NULL,?,0)",
            (directory.name, str(repository), "0" * 64),
        )
    shutil.rmtree(repository)
    record = observer.records(prune=True, apply=True)["records"][0]
    assert record["bound_by_profile"] and not record["removed"] and directory.exists()


def test_status_does_not_recreate_a_record_pruned_after_listing(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    observer, repository, directory = _dead_record(tmp_path, repository_factory)
    rival = _owner(tmp_path, repository, "rival")
    shutil.rmtree(repository)
    original = observer._maintenance
    first = True

    @contextmanager
    def interleave() -> Iterator[None]:
        nonlocal first
        if first:
            first = False
            assert rival.records(prune=True, apply=True, include_other_profiles=True)[
                "records"
            ][0]["removed"]
            assert not directory.exists()
        with original():
            yield

    monkeypatch.setattr(observer, "_maintenance", interleave)
    result = observer.records()
    assert result["records"][0]["reason"] == "already_removed"
    assert not directory.exists()


@pytest.mark.parametrize("unrecorded", [False, True])
def test_moved_workspace_checkout_retains_its_irreversible_regime_fence(
    tmp_path: Path, repository_factory: Callable[..., Path], unrecorded: bool
) -> None:
    from llm_cli.workspace.regime import CutoverManager

    repository = repository_factory(tmp_path, {"source.py": "VALUE = 1\n"})
    former = _owner(tmp_path, repository, "former")
    manager = CutoverManager(former.store, former)
    assert (
        manager.begin(repository, manager.preview(repository)["checkpoint"])["regime"]
        == "workspace"
    )
    directory = _marker(former, repository).parent
    former.close()
    observer = _owner(tmp_path, repository, "observer")
    moved = tmp_path / "moved"
    repository.rename(moved)
    if unrecorded:
        owner = private_read(directory / "owner.json")
        assert owner is not None
        owner.pop("checkout_path")
        private_write(directory / "owner.json", owner)
    record = observer.records(prune=True, apply=True, include_unrecorded=unrecorded)[
        "records"
    ][0]
    assert record["reason"] == "regime_tombstone" and not record["removed"]
    assert directory.exists()
    third = _owner(tmp_path, moved, "third")
    assert third.regime(moved)["regime"] == "workspace"
    with third.pin(moved):
        assert third.regime(moved)["regime"] == "workspace"
    assert private_read(directory / "marker.json")["version"] == 2
    third.close()
