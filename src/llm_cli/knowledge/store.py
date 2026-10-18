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

from llm_cli.knowledge.chunker import VERSION, Chunk, chunks
from llm_cli.knowledge.graph_schema import GRAPH_MIGRATIONS
from llm_cli.knowledge.parsers import PROJECTION_VERSION as GRAPH_VERSION
from llm_cli.knowledge.parsers import VERSION as TOKENIZER_VERSION
from llm_cli.knowledge.parsers import Edge, parse
from llm_cli.paths import validate_owned_sqlite_paths
from llm_cli.storage.connection import connect_knowledge, immediate_transaction
from llm_cli.storage.migrations import apply_migrations


@dataclass(frozen=True, slots=True)
class Document:
    path: str
    identity: str
    mode: str
    revision: str
    text: str
    batch_id: str | None = None


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
            apply_migrations(con, GRAPH_MIGRATIONS)
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
            json.dumps(
                [source_id, VERSION, GRAPH_VERSION, manifest], separators=(",", ":")
            ).encode()
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
            graph_available = self._graph_available(con)
            for doc, parts in prepared:
                remaining()
                self._insert_document(con, source_id, generation, doc, parts)
                if graph_available:
                    self._insert_graph(con, source_id, generation, doc)
            con.execute(
                "INSERT INTO store_metadata VALUES (?,?,?) ON CONFLICT(key) DO "
                "UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (
                    "source_bytes:" + source_id,
                    str(
                        sum(len(doc.text.encode("utf-8")) for doc in documents.values())
                    ),
                    now,
                ),
            )
            if graph_available:
                self._graph_version(con, source_id, now)
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
    def _graph_available(con: sqlite3.Connection) -> bool:
        # Direct store callers can still prepare a pre-graph migration fixture.
        return (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE name='knowledge_graph_files'"
            ).fetchone()
            is not None
        )

    @staticmethod
    def _graph_version(con: sqlite3.Connection, source: str, now: int) -> None:
        con.execute(
            "INSERT INTO store_metadata VALUES (?,?,?) ON CONFLICT(key) DO "
            "UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            ("graph_version:" + source, GRAPH_VERSION, now),
        )

    @staticmethod
    def _graph_source_key(con: sqlite3.Connection, source: str) -> int:
        row = con.execute(
            "SELECT source_key FROM knowledge_graph_sources WHERE source_id=?",
            (source,),
        ).fetchone()
        if row is not None:
            return int(row[0])
        cursor = con.execute(
            "INSERT INTO knowledge_graph_sources(source_id) VALUES (?)", (source,)
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    @staticmethod
    def _insert_graph(
        con: sqlite3.Connection, source: str, generation: str, doc: Document
    ) -> None:
        parsed = parse(doc.path, doc.text)
        document = hashlib.sha256(
            f"{source}\0{doc.path}\0{doc.identity}".encode()
        ).hexdigest()
        source_key = KnowledgeStore._graph_source_key(con, source)
        cursor = con.execute(
            "INSERT INTO knowledge_graph_files"
            "(document_id,source_key,relative_path,parser,parser_version,error_code) "
            "VALUES (?,?,?,?,?,?)",
            (
                document,
                source_key,
                doc.path,
                parsed.parser,
                GRAPH_VERSION if parsed.parser == "python-ast" else TOKENIZER_VERSION,
                parsed.error,
            ),
        )
        file_key = cursor.lastrowid
        con.executemany(
            "INSERT INTO knowledge_graph_nodes VALUES (?,?,?,?,?,?)",
            [
                (source_key, node.key, file_key, node.kind, node.name, node.line)
                for node in parsed.nodes
            ],
        )
        con.executemany(
            "INSERT INTO knowledge_graph_aliases VALUES (?,?,?,?)",
            [(source_key, alias, node, file_key) for alias, node in parsed.aliases],
        )
        edges = list(parsed.edges)
        if doc.batch_id:
            edges.append(
                Edge("file:" + doc.path, "batch:" + doc.batch_id, "changed-by", 1)
            )
        con.executemany(
            "INSERT INTO knowledge_graph_edges VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    source_key,
                    file_key,
                    ordinal,
                    edge.source,
                    edge.target,
                    edge.kind,
                    edge.line,
                    generation,
                )
                for ordinal, edge in enumerate(edges)
            ],
        )

    @staticmethod
    def _refresh_batch(
        con: sqlite3.Connection, document: str, generation: str, doc: Document
    ) -> None:
        # An accepted no-op changes receipt evidence, not its parsed imports.
        row = con.execute(
            "SELECT file_key,source_key FROM knowledge_graph_files WHERE document_id=?",
            (document,),
        ).fetchone()
        if row is None:
            # An upgraded lexical-only projection awaits its complete rebuild.
            return
        con.execute(
            "DELETE FROM knowledge_graph_edges WHERE document_id=? AND "
            "edge_type='changed-by'",
            (row[0],),
        )
        if doc.batch_id:
            con.execute(
                "INSERT INTO knowledge_graph_edges VALUES (?,?,?,?,?,'changed-by',1,?)",
                (
                    row[1],
                    row[0],
                    -1,
                    "file:" + doc.path,
                    "batch:" + doc.batch_id,
                    generation,
                ),
            )

    @staticmethod
    def _insert_document(
        con: sqlite3.Connection,
        source_id: str,
        generation: str,
        doc: Document,
        parts: tuple[Chunk, ...],
    ) -> None:
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
                "INSERT INTO knowledge_fts(rowid,relative_path,text) VALUES (?,?,?)",
                (cursor.lastrowid, doc.path, chunk.text),
            )

    def paths(self, source: str) -> list[str]:
        """Load path metadata only when source policy changes or a worker restarts."""
        with self.connection() as con:
            return [
                str(row[0])
                for row in con.execute(
                    "SELECT relative_path FROM knowledge_documents WHERE source_id=?",
                    (source,),
                )
            ]

    @staticmethod
    def _remove_document(con: sqlite3.Connection, document_id: str) -> int:
        rows = con.execute(
            "SELECT rowid,relative_path,text FROM knowledge_chunks WHERE document_id=?",
            (document_id,),
        ).fetchall()
        con.executemany(
            "INSERT INTO knowledge_fts(knowledge_fts,rowid,relative_path,text) "
            "VALUES ('delete',?,?,?)",
            [tuple(row) for row in rows],
        )
        con.execute("DELETE FROM knowledge_chunks WHERE document_id=?", (document_id,))
        con.execute(
            "DELETE FROM knowledge_documents WHERE document_id=?", (document_id,)
        )
        return len(rows)

    def update(
        self,
        *,
        source_id: str,
        checkout_id: str,
        sequence: int,
        outbox_id: int,
        documents: dict[str, Document | None],
        job_id: str,
        max_files: int,
        max_bytes: int,
        remaining: Callable[[], float],
    ) -> dict[str, object]:
        """Atomically advance a generation by touching only changed documents.

        A generation identifies an immutable projection transition. Unchanged
        document/chunk rows are shared with the prior generation; active source
        metadata and the event cursor become visible in the same transaction.
        """
        remaining()
        now = time.time_ns() // 1_000_000
        with self.connection() as con, immediate_transaction(con):
            deadline = time.monotonic() + remaining()
            con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1_000)
            previous = con.execute(
                "SELECT g.* FROM knowledge_sources s JOIN knowledge_generations g "
                "ON g.generation_id=s.active_generation WHERE s.source_id=?",
                (source_id,),
            ).fetchone()
            if previous is None:
                raise ValueError("lexical update requires an active baseline")
            manifest = [
                (path, doc.identity if doc else None, doc.revision if doc else None)
                for path, doc in sorted(documents.items())
            ]
            generation = hashlib.sha256(
                json.dumps(
                    [
                        source_id,
                        VERSION,
                        GRAPH_VERSION,
                        previous["generation_id"],
                        sequence,
                        outbox_id,
                        manifest,
                    ],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            document_count = int(previous["document_count"])
            chunk_count = int(previous["chunk_count"])
            byte_row = con.execute(
                "SELECT value FROM store_metadata WHERE key=?",
                ("source_bytes:" + source_id,),
            ).fetchone()
            # Upgrade an index built before incremental bookkeeping once. New
            # builds write this aggregate as part of their generation swap.
            byte_count = (
                int(byte_row[0])
                if byte_row
                else int(
                    con.execute(
                        "SELECT COALESCE(sum(length(CAST(text AS BLOB))),0) "
                        "FROM knowledge_documents WHERE source_id=?",
                        (source_id,),
                    ).fetchone()[0]
                )
            )
            graph_available = self._graph_available(con)
            for path, doc in sorted(documents.items()):
                remaining()
                old = con.execute(
                    "SELECT document_id,content_identity,text FROM knowledge_documents "
                    "WHERE source_id=? AND relative_path=?",
                    (source_id, path),
                ).fetchone()
                if doc is not None and doc.path != path:
                    raise ValueError("lexical update path mismatch")
                if old is not None:
                    if doc is not None and str(old["content_identity"]) == doc.identity:
                        # Accepted revisions can change without changing bytes.
                        # Keep even this document's chunks and FTS rows intact.
                        con.execute(
                            "UPDATE knowledge_documents SET indexed_revision=?,"
                            "generation_id=? WHERE document_id=?",
                            (doc.revision, generation, old["document_id"]),
                        )
                        if graph_available:
                            self._refresh_batch(
                                con, str(old["document_id"]), generation, doc
                            )
                        continue
                    chunk_count -= self._remove_document(con, str(old["document_id"]))
                    document_count -= 1
                    byte_count -= len(str(old["text"]).encode("utf-8"))
                if doc is not None:
                    parts = chunks(source_id, doc.path, doc.identity, doc.text)
                    self._insert_document(con, source_id, generation, doc, parts)
                    if graph_available:
                        self._insert_graph(con, source_id, generation, doc)
                    document_count += 1
                    chunk_count += len(parts)
                    byte_count += len(doc.text.encode("utf-8"))
            if byte_count > max_bytes or document_count > max_files:
                raise ValueError("lexical generation exceeds its bounded source budget")
            con.execute(
                "UPDATE knowledge_generations SET state='superseded' "
                "WHERE generation_id=?",
                (previous["generation_id"],),
            )
            con.execute(
                "INSERT INTO knowledge_generations VALUES (?,?,'active',?,?,?,?)",
                (generation, source_id, sequence, document_count, chunk_count, now),
            )
            con.execute(
                "UPDATE knowledge_sources SET active_generation=? WHERE source_id=?",
                (generation, source_id),
            )
            con.execute(
                "INSERT INTO store_metadata VALUES (?,?,?) ON CONFLICT(key) DO "
                "UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                ("source_bytes:" + source_id, str(byte_count), now),
            )
            con.execute(
                "INSERT INTO projection_cursors VALUES (?,?,?,?) ON "
                "CONFLICT(checkout_id) DO UPDATE SET "
                "event_sequence=excluded.event_sequence,outbox_id=excluded.outbox_id",
                (checkout_id, source_id, sequence, outbox_id),
            )
            con.execute(
                "UPDATE projection_jobs SET state='completed',updated_at=? "
                "WHERE job_id=?",
                (now, job_id),
            )
            remaining()
        return {
            "state": "ready",
            "source_id": source_id,
            "generation": generation,
            "indexed_sequence": sequence,
            "documents": document_count,
            "chunks": chunk_count,
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
            con.execute(
                "DELETE FROM store_metadata WHERE key=?", ("source_bytes:" + source,)
            )
            con.execute(
                "DELETE FROM store_metadata WHERE key=?", ("graph_version:" + source,)
            )
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
