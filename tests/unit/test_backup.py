from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from llm_cli.storage.backup import BackupError, backup_database


@contextmanager
def _connection(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _open_live_wal_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (7, 'fixture', ?, 1)",
        ("a" * 64,),
    )
    connection.execute("CREATE TABLE messages (body TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO messages(body) VALUES (?)",
        [("first committed WAL row",), ("second committed WAL row",)],
    )
    return connection


def test_backup_includes_committed_live_wal_and_returns_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "control.sqlite3"
    destination = tmp_path / "backups" / "control.sqlite3"
    destination.parent.mkdir()
    writer = _open_live_wal_database(source)
    try:
        wal = source.with_name(f"{source.name}-wal")
        assert wal.exists()
        assert wal.stat().st_size > 0

        metadata = backup_database(source, destination)

        # The writer remains open, so this verifies the backup API saw committed
        # pages still represented by the live WAL rather than copying only the
        # database's main file.
        assert wal.exists()
        with _connection(destination) as backup:
            assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            rows = backup.execute("SELECT body FROM messages ORDER BY rowid").fetchall()
            assert rows == [
                ("first committed WAL row",),
                ("second committed WAL row",),
            ]
    finally:
        writer.close()

    contents = destination.read_bytes()
    assert metadata.path == destination
    assert metadata.size == len(contents)
    assert metadata.sha256 == hashlib.sha256(contents).hexdigest()
    assert metadata.schema_version == 7
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_backup_atomically_replaces_an_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    with _connection(source) as connection:
        connection.execute("PRAGMA user_version = 23")
        connection.execute("CREATE TABLE values_for_backup (value TEXT)")
        connection.execute("INSERT INTO values_for_backup VALUES ('new')")
    destination.write_bytes(b"old backup contents")
    destination.chmod(0o644)

    metadata = backup_database(source, destination)

    with _connection(destination) as backup:
        assert backup.execute("SELECT value FROM values_for_backup").fetchone() == (
            "new",
        )
    assert metadata.schema_version == 23
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not tuple(tmp_path.glob(".llm-coord-backup-*.tmp"))


@pytest.mark.parametrize("relative_destination", ["source.sqlite3", "./source.sqlite3"])
def test_backup_refuses_source_as_destination(
    tmp_path: Path,
    relative_destination: str,
) -> None:
    source = tmp_path / "source.sqlite3"
    with _connection(source) as connection:
        connection.execute("CREATE TABLE content (value TEXT)")

    destination = tmp_path / relative_destination
    with pytest.raises(BackupError, match="different files"):
        backup_database(source, destination)


def test_backup_refuses_a_hard_link_to_the_source(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "same-file.sqlite3"
    with _connection(source) as connection:
        connection.execute("CREATE TABLE content (value TEXT)")
    os.link(source, destination)

    with pytest.raises(BackupError, match="different files"):
        backup_database(source, destination)


def test_backup_refuses_symlink_destination_without_touching_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    with _connection(source) as connection:
        connection.execute("CREATE TABLE content (value TEXT)")
    target.write_bytes(b"must remain unchanged")
    destination.symlink_to(target)

    with pytest.raises(BackupError, match="must not be a symlink"):
        backup_database(source, destination)

    assert destination.is_symlink()
    assert target.read_bytes() == b"must remain unchanged"


def test_backup_refuses_missing_or_invalid_destination_parent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    with _connection(source) as connection:
        connection.execute("CREATE TABLE content (value TEXT)")

    with pytest.raises(BackupError, match="parent does not exist"):
        backup_database(source, tmp_path / "missing" / "backup.sqlite3")

    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(BackupError, match="parent is not a directory"):
        backup_database(source, parent_file / "backup.sqlite3")


def test_backup_refuses_a_symlink_destination_parent(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    real_parent = tmp_path / "real-backups"
    linked_parent = tmp_path / "linked-backups"
    real_parent.mkdir()
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with _connection(source) as connection:
        connection.execute("CREATE TABLE content (value TEXT)")

    with pytest.raises(BackupError, match="parent must not be a symlink"):
        backup_database(source, linked_parent / "backup.sqlite3")

    assert not (real_parent / "backup.sqlite3").exists()


def test_failed_backup_preserves_existing_destination_and_cleans_temp(
    tmp_path: Path,
) -> None:
    source = tmp_path / "not-a-database.sqlite3"
    destination = tmp_path / "existing.sqlite3"
    source.write_bytes(b"this is not sqlite")
    destination.write_bytes(b"known good previous backup")

    with pytest.raises(BackupError):
        backup_database(source, destination)

    assert destination.read_bytes() == b"known good previous backup"
    assert not tuple(tmp_path.glob(".llm-coord-backup-*.tmp"))
