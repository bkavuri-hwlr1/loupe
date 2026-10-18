"""Installation identity bound to one physical SQLite object.

The external witness deliberately cannot be selected by an authority-root
setting. A copied profile database must be explicitly repaired by its operator.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import reject_symlink_components
from llm_cli.storage.connection import immediate_transaction

if TYPE_CHECKING:
    from llm_cli.storage.control import ControlStore


def recovery_error(message: str, **details: Any) -> LlmCoordError:
    return LlmCoordError(
        ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED,
        message,
        {"retryable": False, **details},
    )


def private_directory(path: Path) -> None:
    try:
        reject_symlink_components(path)
    except LlmCoordError as exc:
        raise recovery_error("authority storage contains a symlink") from exc
    # Never chmod existing storage: an unsafe installation is an error.
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for child in reversed(missing):
        with contextlib.suppress(FileExistsError):
            child.mkdir(mode=0o700)
    reject_symlink_components(path)
    status = path.lstat()
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_mode & 0o077
    ):
        raise recovery_error("authority storage must be an owned private directory")


def private_read(path: Path) -> dict[str, Any] | None:
    try:
        reject_symlink_components(path)
    except LlmCoordError as exc:
        raise recovery_error("authority record contains a symlink") from exc
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise recovery_error("authority record cannot be safely opened") from exc
    try:
        status = os.fstat(fd)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_mode & 0o077
            or status.st_nlink != 1
            or status.st_size > 65_536
        ):
            raise recovery_error("authority record has unsafe storage")
        data = os.read(fd, 65_537)
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("record is not an object")
        checksum = value.pop("checksum")
        if checksum != hashlib.sha256(encode(value)).hexdigest():
            raise ValueError("record checksum differs")
        return value
    except (ValueError, KeyError, UnicodeError) as exc:
        raise recovery_error("authority record is corrupt or unsupported") from exc
    finally:
        os.close(fd)


def encode(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def private_write(path: Path, value: dict[str, Any]) -> None:
    private_directory(path.parent)
    record = {**value, "checksum": hashlib.sha256(encode(value)).hexdigest()}
    temporary = path.with_name("." + secrets.token_hex(16) + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        data = memoryview(encode(record))
        while data:
            data = data[os.write(fd, data) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def database_identity(path: Path) -> dict[str, int]:
    reject_symlink_components(path)
    status = path.lstat()
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_mode & 0o077
        or status.st_nlink != 1
    ):
        raise recovery_error("authority database has unsafe storage")
    return {
        "dev": status.st_dev,
        "ino": status.st_ino,
        "birth_ns": int(getattr(status, "st_birthtime", 0) * 1_000_000_000),
    }


class InstallationIdentity:
    def __init__(self, store: ControlStore) -> None:
        self.store = store
        self.record = store.path.parent / "authority-installation.json"
        self.instance_id: str | None = None

    def validate(self) -> str:
        private_directory(self.store.path.parent)
        physical = database_identity(self.store.path)
        witness = private_read(self.record)
        with self.store.connection() as connection, immediate_transaction(connection):
            row = connection.execute(
                "SELECT database_instance_id FROM authority_installation WHERE "
                "singleton=1"
            ).fetchone()
            if row is None:
                if witness is not None:
                    raise recovery_error(
                        "database-instance witness has no matching database identity"
                    )
                instance_id = secrets.token_hex(32)
                # External first: a crash cannot silently re-initialize an old DB.
                private_write(
                    self.record,
                    {"version": 1, "instance_id": instance_id, "physical": physical},
                )
                connection.execute(
                    "INSERT INTO authority_installation VALUES (1, ?)", (instance_id,)
                )
            else:
                instance_id = str(row[0])
                if witness != {
                    "version": 1,
                    "instance_id": instance_id,
                    "physical": physical,
                }:
                    raise recovery_error(
                        "database-instance binding is missing or changed; explicit "
                        "repair is required"
                    )
        if database_identity(self.store.path) != physical:
            raise recovery_error("database object changed during authority acquisition")
        if self.instance_id is not None and self.instance_id != instance_id:
            raise recovery_error("database-instance identity changed")
        self.instance_id = instance_id
        return instance_id
