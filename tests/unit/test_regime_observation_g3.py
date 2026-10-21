"""Regime observers retain ownership checks without the SQLite/authority lock cycle."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from test_physical_authority import _marker, _owner

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.workspace.installation import private_read, private_write
from llm_cli.workspace.regime import CutoverManager


@pytest.fixture
def repository(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
) -> Path:
    return repository_factory(tmp_path, {"source.py": "VALUE = 1\n"})


@pytest.mark.parametrize("workspace", [False, True], ids=["legacy-v1", "workspace-v2"])
def test_observer_refuses_tampered_marker_while_owned(
    tmp_path: Path,
    repository: Path,
    workspace: bool,
) -> None:
    authority = _owner(tmp_path, repository, "observer")
    try:
        if workspace:
            manager = CutoverManager(authority.store, authority)
            manager.begin(repository, manager.preview(repository)["checkpoint"])
        else:
            with authority.pin(repository):
                pass
        marker_path = _marker(authority, repository)
        marker = private_read(marker_path)
        assert marker is not None and marker["version"] == (2 if workspace else 1)
        private_write(marker_path, {**marker, "owner_profile": "unexpected-owner"})
        with pytest.raises(LlmCoordError) as error:
            authority.regime(repository)
        assert error.value.code is ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
    finally:
        authority.close()


def test_observer_refuses_replaced_ownership_lock_while_descriptor_is_held(
    tmp_path: Path,
    repository: Path,
) -> None:
    authority = _owner(tmp_path, repository, "observer")
    try:
        with authority.pin(repository):
            pass
        lock = _marker(authority, repository).parent / "ownership.lock"
        lock.rename(lock.with_name("previous-ownership.lock"))
        lock.touch(mode=0o600)
        with pytest.raises(LlmCoordError) as error:
            authority.regime(repository)
        assert error.value.code is ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
    finally:
        authority.close()


def test_published_marker_can_be_observed_while_its_database_update_waits(
    tmp_path: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _owner(tmp_path, repository, "observer")
    store = authority.store
    with authority.pin(repository) as pin:
        owner = pin.owner
    original = store.connection
    published = threading.Event()
    permit_database = threading.Event()
    observed = threading.Event()
    failures: list[BaseException] = []
    results: list[dict[str, Any]] = []
    marker = private_read(_marker(authority, repository))
    assert marker is not None
    prior_generation = marker["generation"]

    @contextmanager
    def delayed_connection() -> Iterator[sqlite3.Connection]:
        if threading.current_thread().name == "marker-publisher":
            # _write_marker reaches this only after publishing and releasing its
            # observation lock. It still holds the unrelated authority lock.
            published.set()
            if not permit_database.wait(5):
                raise AssertionError("test did not permit the marker database update")
        with original() as connection:
            yield connection

    monkeypatch.setattr(store, "connection", delayed_connection)

    def publish() -> None:
        try:
            authority._write_marker(owner, "clean")
        except BaseException as error:
            failures.append(error)

    def observe() -> None:
        try:
            results.append(authority.regime(repository))
        except BaseException as error:
            failures.append(error)
        finally:
            observed.set()

    publisher = threading.Thread(target=publish, name="marker-publisher")
    reader = threading.Thread(target=observe, name="regime-reader")
    try:
        publisher.start()
        assert published.wait(5)
        current = private_read(_marker(authority, repository))
        assert current is not None and current["generation"] == prior_generation + 1
        with original() as connection:
            row = connection.execute(
                "SELECT marker_generation FROM authority_checkouts "
                "WHERE physical_key=?",
                (owner.physical.key,),
            ).fetchone()
            assert row is not None and row[0] == prior_generation
        reader.start()
        assert observed.wait(2), (
            "observer waited for the authority lock/database writer"
        )
        assert not failures
        assert results == [
            {
                "physical_key": owner.physical.key,
                "regime": "legacy",
                "generation": 0,
                "intent_id": None,
                "checkpoint": None,
            }
        ]
    finally:
        permit_database.set()
        publisher.join(5)
        if reader.ident is not None:
            reader.join(5)
        authority.close()
    assert not publisher.is_alive() and not reader.is_alive()
    assert not failures


def test_checkout_replacement_after_binding_read_fails_closed(
    tmp_path: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _owner(tmp_path, repository, "observer")
    store = authority.store
    with authority.pin(repository):
        pass
    original = store.connection
    administration = repository / ".git"
    displaced = repository / "previous-git-administration"
    changed = False

    @contextmanager
    def changed_after_read() -> Iterator[sqlite3.Connection]:
        nonlocal changed
        with original() as connection:
            yield connection
        if not changed:
            changed = True
            administration.rename(displaced)
            administration.mkdir(mode=0o700)

    monkeypatch.setattr(store, "connection", changed_after_read)
    try:
        with pytest.raises(LlmCoordError) as error:
            authority.regime(repository)
        assert changed and error.value.code is ErrorCode.CHECKOUT_IDENTITY_CHANGED
    finally:
        if changed:
            administration.rmdir()
            displaced.rename(administration)
        authority.close()
