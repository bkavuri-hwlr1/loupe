"""Private, bounded path metadata for one hydration inventory.

Ordinary source does not need a retained stat record in a runtime plan. This
catalogue retains only its spelling, alias and ignored classification, so the
planner can still reject aliases and recheck every classification. It never
reads repository file contents and is removed before the preview returns.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path


class CatalogueLimit(Exception):
    pass


class PathCatalogue:
    def __init__(self, maximum_bytes: int) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="loupe-hydration-"))
        self.path = self.directory / "paths.sqlite3"
        self.connection: sqlite3.Connection | None = None
        try:
            self.connection = sqlite3.connect(self.path)
            os.chmod(self.path, 0o600)
            self.connection.execute("PRAGMA journal_mode=OFF")
            self.connection.execute("PRAGMA synchronous=OFF")
            self.connection.execute("PRAGMA cache_size=-64")
            self.connection.execute("PRAGMA mmap_size=0")
            self.connection.execute("PRAGMA page_size=4096")
            # SQLite's actual page allocation, not a smaller accounting weight,
            # bounds the catalogue. Other live metadata shares this ceiling.
            self.connection.execute(
                f"PRAGMA max_page_count={max(1, maximum_bytes // 4096)}"
            )
            self.connection.execute(
                "CREATE TABLE paths(alias TEXT PRIMARY KEY, spelling TEXT, "
                "ignored INTEGER NOT NULL, administration INTEGER NOT NULL) "
                "WITHOUT ROWID"
            )
        except BaseException as exc:
            self.close()
            if isinstance(exc, sqlite3.Error):
                if exc.sqlite_errorcode == sqlite3.SQLITE_FULL:
                    raise CatalogueLimit from None
                raise OSError(
                    "hydration metadata catalogue could not be stored"
                ) from exc
            raise

    @property
    def allocated_bytes(self) -> int:
        assert self.connection is not None
        return int(self.connection.execute("PRAGMA page_count").fetchone()[0]) * 4096

    def add(self, path: str, *, ignored: bool, administration: bool) -> str | None:
        assert self.connection is not None
        alias = unicodedata.normalize("NFC", path).casefold()
        try:
            previous = self.connection.execute(
                "SELECT spelling FROM paths WHERE alias=?", (alias,)
            ).fetchone()
            if previous is not None:
                return str(previous[0]) if previous[0] is not None else alias
            self.connection.execute(
                "INSERT INTO paths VALUES (?,?,?,?)",
                (alias, path if path != alias else None, ignored, administration),
            )
        except sqlite3.Error as exc:
            if exc.sqlite_errorcode == sqlite3.SQLITE_FULL:
                raise CatalogueLimit from None
            raise OSError("hydration metadata catalogue could not be stored") from exc
        return None

    def records(self) -> Iterator[tuple[str, bool]]:
        assert self.connection is not None
        cursor = self.connection.execute(
            "SELECT alias,spelling,ignored FROM paths "
            "WHERE administration=0 ORDER BY alias"
        )
        try:
            for alias, spelling, ignored in cursor:
                yield (
                    str(spelling) if spelling is not None else str(alias),
                    bool(ignored),
                )
        finally:
            cursor.close()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        # Do not recurse into a pathname supplied by repository contents.
        # These are the only files this private SQLite connection can own.
        for suffix in ("", "-journal", "-wal", "-shm"):
            with suppress(FileNotFoundError):
                (self.directory / ("paths.sqlite3" + suffix)).unlink()
        self.directory.rmdir()
