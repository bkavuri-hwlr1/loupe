"""Independent lexical generations; no coordination authority or provider imports."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm_cli.knowledge.chunker import VERSION, chunks
from llm_cli.paths import validate_owned_sqlite_paths
from llm_cli.storage.connection import connect_knowledge, immediate_transaction
from llm_cli.storage.migrations import KNOWLEDGE_MIGRATIONS, apply_migrations


@dataclass(frozen=True, slots=True)
class Document:
    path: str
    identity: str
    mode: str
    revision: str
    text: str


class KnowledgeStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def validate_paths(self) -> tuple[Path, ...]:
        return validate_owned_sqlite_paths(self.path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.validate_paths()
        with closing(connect_knowledge(self.path, timeout_seconds=0.25)) as connection:
            connection.set_progress_handler(lambda: 0, 10_000)
            yield connection

    def initialize(self) -> None:
        with self.connection() as con:
            apply_migrations(con, KNOWLEDGE_MIGRATIONS)
            con.execute(
                "UPDATE projection_jobs SET state='pending' WHERE state='running'"
            )

    def source(self, checkout: str) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM knowledge_sources WHERE checkout_id=?", (checkout,)
            ).fetchone()
        return dict(row) if row else None

    def documents(self, source: str) -> dict[str, Document]:
        with self.connection() as con:
            return {
                str(row["relative_path"]): Document(
                    str(row["relative_path"]),
                    str(row["content_identity"]),
                    str(row["mode"]),
                    str(row["indexed_revision"]),
                    str(row["text"]),
                )
                for row in con.execute(
                    "SELECT * FROM knowledge_documents WHERE source_id=?", (source,)
                )
            }

    def start_job(
        self, source: str, checkout: str, root: Path, oid: str, sequence: int, job: str
    ) -> None:
        now = time.time_ns() // 1_000_000
        with self.connection() as con, immediate_transaction(con):
            con.execute(
                "INSERT INTO knowledge_sources VALUES (?,?,?,1,?,NULL,?,?) "
                "ON CONFLICT(source_id) DO NOTHING",
                (source, checkout, str(root), oid, VERSION, now),
            )
            con.execute(
                "INSERT INTO projection_jobs VALUES (?,?,?,'running',NULL,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET "
                "state='running',error_code=NULL,updated_at=excluded.updated_at",
                (job, source, sequence, now, now),
            )

    def fail_job(self, job: str, error: str) -> None:
        with self.connection() as con:
            con.execute(
                "UPDATE projection_jobs SET state='failed',error_code=?,updated_at=? "
                "WHERE job_id=?",
                (error, time.time_ns() // 1_000_000, job),
            )

    def replace(
        self,
        *,
        source_id: str,
        checkout_id: str,
        root: Path,
        base_oid: str,
        sequence: int,
        outbox_id: int,
        documents: dict[str, Document],
        job_id: str,
        remaining: Callable[[], float] | None = None,
    ) -> dict[str, object]:
        if remaining is None:
            deadline = time.monotonic() + 30

            def remaining() -> float:
                value = deadline - time.monotonic()
                if value <= 0:
                    raise ValueError("lexical generation exceeded its time budget")
                return value

        remaining()
        manifest = [
            (path, doc.identity, doc.revision)
            for path, doc in sorted(documents.items())
        ]
        generation = hashlib.sha256(
            json.dumps([source_id, VERSION, manifest], separators=(",", ":")).encode()
        ).hexdigest()
        prepared = [
            (doc, chunks(source_id, doc.path, doc.identity, doc.text))
            for _, doc in sorted(documents.items())
        ]
        total = sum(len(parts) for _, parts in prepared)
        now = time.time_ns() // 1_000_000
        with self.connection() as con, immediate_transaction(con):
            deadline = time.monotonic() + remaining()
            con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1_000)
            con.execute(
                "INSERT INTO knowledge_sources VALUES (?,?,?,1,?,NULL,?,?) "
                "ON CONFLICT(source_id) DO UPDATE SET "
                "enabled=1,base_oid=excluded.base_oid",
                (source_id, checkout_id, str(root), base_oid, VERSION, now),
            )
            con.execute(
                "INSERT INTO projection_jobs VALUES (?,?,?,'running',NULL,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET "
                "state='running',updated_at=excluded.updated_at",
                (job_id, source_id, sequence, now, now),
            )
            con.execute(
                "UPDATE knowledge_generations SET state='superseded' WHERE source_id=?",
                (source_id,),
            )
            con.execute(
                "INSERT INTO knowledge_generations VALUES (?,?,'staging',?,?,?,?) "
                "ON CONFLICT(generation_id) DO UPDATE SET "
                "state='staging',source_sequence=excluded.source_sequence",
                (generation, source_id, sequence, len(documents), total, now),
            )
            self._clear(con, source_id)
            for doc, parts in prepared:
                remaining()
                document_id = hashlib.sha256(
                    f"{source_id}\0{doc.path}\0{doc.identity}".encode()
                ).hexdigest()
                con.execute(
                    "INSERT INTO knowledge_documents VALUES (?,?,?,?,?,?,?,?)",
                    (
                        document_id,
                        source_id,
                        doc.path,
                        doc.identity,
                        doc.mode,
                        doc.revision,
                        generation,
                        doc.text,
                    ),
                )
                for chunk in parts:
                    cursor = con.execute(
                        "INSERT INTO "
                        "knowledge_chunks(chunk_id,document_id,source_id,relative_path,"
                        "ordinal,start_line,end_line,text) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            chunk.chunk_id,
                            document_id,
                            source_id,
                            doc.path,
                            chunk.ordinal,
                            chunk.start_line,
                            chunk.end_line,
                            chunk.text,
                        ),
                    )
                    con.execute(
                        "INSERT INTO knowledge_fts(rowid,relative_path,text) VALUES "
                        "(?,?,?)",
                        (cursor.lastrowid, doc.path, chunk.text),
                    )
            actual = con.execute(
                "SELECT count(*) FROM knowledge_chunks WHERE source_id=?", (source_id,)
            ).fetchone()[0]
            if actual != total:
                raise ValueError("lexical generation count verification failed")
            con.execute(
                "UPDATE knowledge_generations SET state='active' WHERE generation_id=?",
                (generation,),
            )
            con.execute(
                "UPDATE knowledge_sources SET active_generation=? WHERE source_id=?",
                (generation, source_id),
            )
            con.execute(
                "INSERT INTO projection_cursors VALUES (?,?,?,?) ON "
                "CONFLICT(checkout_id) DO UPDATE SET "
                "event_sequence=excluded.event_sequence,outbox_id=excluded.outbox_id",
                (checkout_id, source_id, sequence, outbox_id),
            )
            con.execute(
                "UPDATE projection_jobs SET state='completed',updated_at=? WHERE "
                "job_id=?",
                (now, job_id),
            )
        return {
            "state": "ready",
            "source_id": source_id,
            "generation": generation,
            "indexed_sequence": sequence,
            "documents": len(documents),
            "chunks": total,
        }

    @staticmethod
    def _clear(con: sqlite3.Connection, source: str) -> None:
        for row in con.execute(
            "SELECT rowid,relative_path,text FROM knowledge_chunks WHERE source_id=?",
            (source,),
        ).fetchall():
            con.execute(
                "INSERT INTO knowledge_fts(knowledge_fts,rowid,relative_path,text) "
                "VALUES ('delete',?,?,?)",
                tuple(row),
            )
        con.execute("DELETE FROM knowledge_chunks WHERE source_id=?", (source,))
        con.execute("DELETE FROM knowledge_documents WHERE source_id=?", (source,))

    def drop(self, checkout: str) -> None:
        with self.connection() as con, immediate_transaction(con):
            row = con.execute(
                "SELECT source_id FROM knowledge_sources WHERE checkout_id=?",
                (checkout,),
            ).fetchone()
            if not row:
                return
            source = str(row[0])
            self._clear(con, source)
            for table in (
                "projection_jobs",
                "projection_cursors",
                "knowledge_generations",
            ):
                con.execute(f"DELETE FROM {table} WHERE source_id=?", (source,))
            con.execute("DELETE FROM knowledge_sources WHERE source_id=?", (source,))

    def status(self) -> list[dict[str, Any]]:
        with self.connection() as con:
            return [
                dict(row)
                for row in con.execute(
                    "SELECT s.checkout_id,s.source_id,s.active_generation AS "
                    "generation,"
                    "c.event_sequence AS indexed_sequence,g.document_count AS "
                    "documents,g.chunk_count AS chunks, "
                    "(SELECT state FROM projection_jobs j "
                    "WHERE j.source_id=s.source_id "
                    "ORDER BY updated_at DESC,rowid DESC LIMIT 1) AS job_state, "
                    "(SELECT error_code FROM projection_jobs j "
                    "WHERE j.source_id=s.source_id "
                    "ORDER BY updated_at DESC,rowid DESC LIMIT 1) AS error "
                    "FROM knowledge_sources s LEFT JOIN projection_cursors c "
                    "USING(source_id) "
                    "LEFT JOIN knowledge_generations g ON "
                    "g.generation_id=s.active_generation ORDER BY s.checkout_id"
                )
            ]
