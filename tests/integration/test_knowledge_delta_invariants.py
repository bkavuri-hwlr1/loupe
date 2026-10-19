"""Projection deltas preserve published generations across upgrades and failures."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from test_knowledge_index import _index
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.knowledge import indexer
from llm_cli.knowledge.store import Document, KnowledgeStore
from llm_cli.storage.migrations import KNOWLEDGE_MIGRATIONS, apply_migrations
from llm_cli.workspace.batches import BatchFile
from llm_cli.workspace.identity import ABSENT, REGULAR_MODE, identify_path


def _snapshot(store: KnowledgeStore) -> dict[str, list[tuple[Any, ...]]]:
    with store.connection() as con:
        return {
            table: [
                tuple(row) for row in con.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]
            for table in (
                "knowledge_sources",
                "knowledge_generations",
                "knowledge_documents",
                "knowledge_chunks",
                "projection_cursors",
                "store_metadata",
            )
        }


def _assert_fts_integrity(store: KnowledgeStore) -> None:
    with store.connection() as con:
        con.execute(
            "INSERT INTO knowledge_fts(knowledge_fts,rank) VALUES ('integrity-check',1)"
        )


def test_v2_upgrade_recovers_missing_byte_aggregate_from_unchanged_documents(
    tmp_path: Path,
) -> None:
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3")
    with store.connection() as con:
        assert apply_migrations(con, KNOWLEDGE_MIGRATIONS[:2]) == 2
    source, checkout = "upgrade-source", "upgrade-checkout"
    store.start_job(source, checkout, tmp_path, "base", 0, "baseline")
    documents = {
        "a.py": Document("a.py", "a-id", REGULAR_MODE, "git:base", "oldmarker\n"),
        "b.py": Document("b.py", "b-id", REGULAR_MODE, "git:base", "é" * 20 + "\n"),
    }
    baseline = store.replace(
        source_id=source,
        checkout_id=checkout,
        root=tmp_path,
        base_oid="base",
        sequence=0,
        outbox_id=0,
        documents=documents,
        job_id="baseline",
    )
    with store.connection() as con:
        # Existing v2 projections predate the incremental source byte aggregate.
        con.execute(
            "DELETE FROM store_metadata WHERE key=?", ("source_bytes:" + source,)
        )
        assert (
            con.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 2
        )
        assert not con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='knowledge_chunks_document'"
        ).fetchone()
        unchanged = tuple(
            con.execute(
                "SELECT rowid,* FROM knowledge_chunks WHERE relative_path='b.py'"
            ).fetchone()
        )
    store.initialize()
    store.initialize()
    with store.connection() as con:
        assert (
            con.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 5
        )
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='knowledge_chunks_document'"
        ).fetchone()
    changed = Document(
        "a.py", "new-a-id", REGULAR_MODE, "checkout-event:1", "newmarker\n"
    )
    store.start_job(source, checkout, tmp_path, "base", 1, "delta")
    before = _snapshot(store)
    arguments = {
        "source_id": source,
        "checkout_id": checkout,
        "sequence": 1,
        "outbox_id": 1,
        "documents": {"a.py": changed},
        "job_id": "delta",
        "max_files": 2,
        "remaining": lambda: 30.0,
    }
    # The changed file fits, but the unchanged UTF-8 bytes exceed this total.
    with pytest.raises(ValueError, match="bounded source budget"):
        store.update(**arguments, max_bytes=len(changed.text.encode()))
    assert _snapshot(store) == before
    updated = store.update(**arguments, max_bytes=1024)
    assert updated["generation"] != baseline["generation"]
    with store.connection() as con:
        assert int(
            con.execute(
                "SELECT value FROM store_metadata WHERE key=?",
                ("source_bytes:" + source,),
            ).fetchone()[0]
        ) == len(changed.text.encode()) + len(documents["b.py"].text.encode())
        assert (
            tuple(
                con.execute(
                    "SELECT rowid,* FROM knowledge_chunks WHERE relative_path='b.py'"
                ).fetchone()
            )
            == unchanged
        )
        assert (
            con.execute("SELECT outbox_id FROM projection_cursors").fetchone()[0] == 1
        )
    _assert_fts_integrity(store)


def test_same_identity_receipt_advances_revision_without_chunk_or_fts_writes(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env
    index, checkout = _index(env, tmp_path)
    baseline = index.build(checkout)
    assert baseline["state"] == "ready"
    with index.store.connection() as con:
        before = [tuple(row) for row in con.execute("SELECT * FROM knowledge_chunks")]
    original_connection = index.store.connection

    @contextmanager
    def no_chunk_fts_mutation() -> Iterator[sqlite3.Connection]:
        with original_connection() as con:

            def authorize(action: int, table: str | None, *unused: str | None) -> int:
                if action in {
                    sqlite3.SQLITE_INSERT,
                    sqlite3.SQLITE_UPDATE,
                    sqlite3.SQLITE_DELETE,
                } and table in {"knowledge_chunks", "knowledge_fts"}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            con.set_authorizer(authorize)
            yield con

    path = env.repository / "a.txt"
    publication = env.publisher.publish(
        "same-identity",
        "first",
        (BatchFile("a.txt", identify_path(path), path.read_bytes(), REGULAR_MODE),),
        ("*",),
    )
    assert publication.state == "published"
    with monkeypatch.context() as patch:
        patch.setattr(index.store, "connection", no_chunk_fts_mutation)
        assert index.process_pending() == 1
    with index.store.connection() as con:
        assert [
            tuple(row) for row in con.execute("SELECT * FROM knowledge_chunks")
        ] == before
        sequence = con.execute(
            "SELECT event_sequence FROM projection_cursors"
        ).fetchone()[0]
        assert (
            con.execute(
                "SELECT indexed_revision FROM knowledge_documents "
                "WHERE relative_path='a.txt'"
            ).fetchone()[0]
            == f"checkout-event:{sequence}"
        )
    result = index.search_lexical(checkout, "a.txt")
    assert result["generation"] != baseline["generation"]
    assert result["results"][0]["generation"] == result["generation"]
    assert not result["results"][0]["stale"]
    _assert_fts_integrity(index.store)


@pytest.mark.parametrize("budget", ["bytes", "files"])
def test_aggregate_budget_failure_preserves_projection_and_replays_cursor(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget: str,
) -> None:
    env = batch_env
    index, checkout = _index(env, tmp_path)
    baseline = index.build(checkout)
    assert baseline["state"] == "ready"
    if budget == "bytes":
        file = BatchFile(
            "a.txt",
            identify_path(env.repository / "a.txt"),
            b"newmarker\n",
            REGULAR_MODE,
        )
        cap = ("MAX_BYTES", len(file.content or b""))
    else:
        file = BatchFile("new.txt", ABSENT, b"newmarker\n", REGULAR_MODE)
        cap = ("MAX_FILES", 2)
    assert env.publisher.publish("budget-delta", "first", (file,), ("*",)).state == (
        "published"
    )
    before = _snapshot(index.store)
    with monkeypatch.context() as patch:
        patch.setattr(indexer, *cap)
        assert index.process_pending() == 0
    assert _snapshot(index.store) == before
    assert index.process_pending() == 1
    assert index.search_lexical(checkout, "newmarker")["results"]
    assert index.status(checkout)["sources"][0]["documents"] == (
        2 if budget == "bytes" else 3
    )
    _assert_fts_integrity(index.store)


def test_expired_sqlite_progress_handler_rolls_back_partial_delta_and_retries(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = batch_env
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    file = BatchFile(
        "a.txt",
        identify_path(env.repository / "a.txt"),
        b"timeoutmarker\n",
        REGULAR_MODE,
    )
    assert env.publisher.publish("timeout-delta", "first", (file,), ("*",)).state == (
        "published"
    )
    before = _snapshot(index.store)
    original_insert = index.store._insert_document
    writes: list[str] = []

    def expire_after_insert(
        con: sqlite3.Connection,
        source: str,
        generation: str,
        doc: Document,
        parts: Any,
    ) -> None:
        original_insert(con, source, generation, doc, parts)
        writes.append(doc.path)
        deadline = time.monotonic() - 1
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1)

    with monkeypatch.context() as patch:
        patch.setattr(index.store, "_insert_document", expire_after_insert)
        assert index.process_pending() == 0
    assert writes == ["a.txt"]
    assert _snapshot(index.store) == before
    with index.store.connection() as con:
        assert (
            con.execute(
                "SELECT state FROM projection_jobs WHERE job_id LIKE 'outbox:%'"
            ).fetchone()[0]
            == "failed"
        )
    assert index.process_pending() == 1
    assert index.search_lexical(checkout, "timeoutmarker")["results"]
    _assert_fts_integrity(index.store)
