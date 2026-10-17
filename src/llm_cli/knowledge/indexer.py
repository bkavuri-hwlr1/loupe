"""Bounded indexing of immutable Git blobs and accepted artifact versions.

Current working bytes are only used to revalidate identities. They are never
presented as the contents of an earlier event or silently imported as history.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from llm_cli.agent.source_policy import contains_secret_material, excluded_paths
from llm_cli.errors import LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.knowledge import outbox
from llm_cli.knowledge.search import current_matches, eligible_path, search
from llm_cli.knowledge.store import Document, KnowledgeStore
from llm_cli.paths import reject_symlink_components
from llm_cli.storage.connection import immediate_transaction
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.broker import load_candidate_content
from llm_cli.workspace.identity import ObjectKind, content_identity

MAX_FILES = 20_000
MAX_FILE = 1024 * 1024
MAX_BYTES = 64 * 1024 * 1024
_ERRORS = (sqlite3.Error, OSError, ValueError, RuntimeError, LlmCoordError)


class KnowledgeIndex:
    def __init__(
        self, control: ControlStore, path: Path, candidate_dir: Path, *, enabled: bool
    ) -> None:
        self.control, self.store, self.candidate_dir = (
            control,
            KnowledgeStore(path),
            candidate_dir,
        )
        self.enabled = enabled
        self.error: str | None = None
        self.lock = threading.Lock()
        self._last_checkout = ""
        outbox.configure(control, enabled=enabled, candidate_dir=candidate_dir)

    def _failed(self, exc: Exception) -> dict[str, Any]:
        self.error = "optional lexical index unavailable (" + type(exc).__name__ + ")"
        return {"state": "unavailable", "error": self.error}

    def _job_failed(self, job: str | None, exc: Exception) -> dict[str, Any]:
        result = self._failed(exc)
        if job:
            with suppress(*_ERRORS):
                self.store.fail_job(job, str(result["error"]))
        return result

    def initialize(self) -> None:
        if not self.enabled:
            return
        if not self.lock.acquire(blocking=False):
            return
        try:
            self.store.initialize()
            self.error = None
        except _ERRORS as exc:
            self._failed(exc)
        finally:
            self.lock.release()

    def status(self, checkout_id: str | None = None) -> dict[str, Any]:
        if not self.enabled:
            return {"state": "off", "enabled": False, "sources": []}
        if self.error:
            return {
                "state": "unavailable",
                "enabled": True,
                "error": self.error,
                "sources": [],
            }
        if not self.lock.acquire(blocking=False):
            return {"state": "building", "enabled": True, "sources": []}
        try:
            sources = self.store.status()
            if checkout_id:
                sources = [
                    source for source in sources if source["checkout_id"] == checkout_id
                ]
            failed = any(
                source.get("job_state") == "failed" or not source.get("generation")
                for source in sources
            )
            result: dict[str, Any] = {
                "state": "degraded" if failed else "ready",
                "enabled": True,
                "sources": sources,
            }
            if failed:
                result["error"] = (
                    "A lexical projection is incomplete; retry index rebuild."
                )
            return result
        except _ERRORS as exc:
            return self._failed(exc)
        finally:
            self.lock.release()

    def build(self, checkout_id: str, *, rebuild: bool = False) -> dict[str, Any]:
        if not self.enabled:
            return {"state": "off", "enabled": False}
        with self.lock:
            job_id: str | None = None
            try:
                repaired = self._repair_store() if rebuild else False
                self.store.initialize()
                checkout = self.control.get_checkout(checkout_id)
                if checkout is None:
                    raise ValueError("unknown checkout")
                root = Path(checkout.canonical_path)
                source_id = hashlib.sha256(
                    ("lexical-v1\0" + checkout_id).encode()
                ).hexdigest()
                with self.control.connection() as con, immediate_transaction(con):
                    con.execute(
                        "INSERT INTO knowledge_subscriptions VALUES (?,?,1) "
                        "ON CONFLICT(checkout_id) DO UPDATE SET enabled=1",
                        (checkout_id, source_id),
                    )
                    sequence = int(
                        con.execute(
                            "SELECT next_event_sequence FROM checkout_heads WHERE "
                            "checkout_id=?",
                            (checkout_id,),
                        ).fetchone()[0]
                    )
                    last = int(
                        con.execute(
                            "SELECT COALESCE(max(outbox_id),0) FROM knowledge_outbox "
                            "WHERE checkout_id=?",
                            (checkout_id,),
                        ).fetchone()[0]
                    )
                deadline = time.monotonic() + 30

                def remaining() -> float:
                    value = deadline - time.monotonic()
                    if value <= 0:
                        raise ValueError("lexical baseline exceeded its time budget")
                    return value

                head = run_git(
                    root,
                    ["rev-parse", "--verify", "HEAD^{commit}"],
                    remaining=remaining,
                )
                assert isinstance(head.stdout, str)
                oid = head.stdout.strip()
                job_id = hashlib.sha256(
                    f"build:{source_id}:{oid}:{sequence}".encode()
                ).hexdigest()
                self.store.start_job(
                    source_id, checkout_id, root, oid, sequence, job_id
                )
                documents = self._baseline(root, oid, remaining)
                # Existing accepted artifacts can describe dirty current source,
                # but an old artifact must not override a newer committed file.
                receipts = self._receipts(checkout_id, sequence=sequence)
                self._apply_receipts(
                    root, documents, receipts, current_only=True, remaining=remaining
                )
                self._filter(root, documents, remaining=remaining)
                result = self.store.replace(
                    source_id=source_id,
                    checkout_id=checkout_id,
                    root=root,
                    base_oid=oid,
                    sequence=sequence,
                    outbox_id=last,
                    documents=documents,
                    job_id=job_id,
                    remaining=remaining,
                )
                self.error = None
                if repaired:
                    result["store_recreated"] = True
                    result["notice"] = (
                        "Damaged optional store recreated; "
                        "rebuild any other indexed checkouts."
                    )
                return result
            except _ERRORS as exc:
                return self._job_failed(job_id, exc)

    def _repair_store(self) -> bool:
        """Explicit rebuild may discard a damaged, owned, derived store only."""
        self.store.validate_paths()
        if not self.store.path.exists():
            return False
        try:
            self.store.initialize()
            with self.store.connection() as con:
                row = con.execute("PRAGMA integrity_check").fetchone()
                if row is None or row[0] != "ok":
                    raise ValueError("knowledge integrity check failed")
                con.execute(
                    "INSERT INTO knowledge_fts(knowledge_fts,rank) "
                    "VALUES ('integrity-check',1)"
                )
            return False
        except sqlite3.Error as exc:
            # Contention, permissions and schema incompatibility are not proof
            # of corruption and must never trigger deletion of a healthy store.
            if getattr(exc, "sqlite_errorcode", 0) & 255 not in {
                sqlite3.SQLITE_CORRUPT,
                sqlite3.SQLITE_NOTADB,
            }:
                raise
        except ValueError:
            pass
        paths = self.store.validate_paths()
        identities = []
        for path in paths:
            reject_symlink_components(path)
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
            ):
                raise ValueError("optional index repair requires owned regular files")
            identities.append((path, metadata))
        for path, expected in reversed(identities):
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError("optional index changed during repair")
            path.unlink()
        return True

    def drop(self, checkout_id: str) -> dict[str, Any]:
        if not self.enabled:
            return {"state": "off", "enabled": False}
        with self.lock:
            with self.control.connection() as con:
                con.execute(
                    "UPDATE knowledge_subscriptions SET enabled=0 WHERE checkout_id=?",
                    (checkout_id,),
                )
            try:
                if self.store.path.exists():
                    self.store.drop(checkout_id)
                self.error = None
                return {"state": "dropped", "checkout_id": checkout_id}
            except _ERRORS as exc:
                return self._failed(exc)

    def search_lexical(
        self, checkout_id: str, query: str, *, limit: int = 8, explain: bool = False
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"state": "off", "results": []}
        if self.error:
            return {"state": "unavailable", "error": self.error, "results": []}
        with self.lock:
            try:
                with self.control.connection() as con:
                    if not outbox.subscribed(con, self.control, checkout_id):
                        return {"state": "not_built", "results": []}
                return search(
                    self.store, checkout_id, query, limit=limit, explain=explain
                )
            except _ERRORS as exc:
                result = self._failed(exc)
                result["results"] = []
                return result

    def process_pending(self, *, limit: int = 8) -> int:
        if not self.enabled or not self.lock.acquire(blocking=False):
            return 0
        completed = 0
        job_id: str | None = None
        try:
            # Initialization can recover a repaired optional file on the next
            # pass. This code never executes under the publication barrier.
            self.store.initialize()
            with self.control.connection() as con:
                subscriptions = con.execute(
                    "SELECT checkout_id,source_id FROM knowledge_subscriptions "
                    "WHERE enabled=1 AND checkout_id>? ORDER BY checkout_id LIMIT 8",
                    (self._last_checkout,),
                ).fetchall()
            if not subscriptions:
                self._last_checkout = ""
                with self.control.connection() as con:
                    subscriptions = con.execute(
                        "SELECT checkout_id,source_id FROM knowledge_subscriptions "
                        "WHERE enabled=1 ORDER BY checkout_id LIMIT 8"
                    ).fetchall()
            deadline = time.monotonic() + 5

            def remaining() -> float:
                value = deadline - time.monotonic()
                if value <= 0:
                    raise ValueError("lexical projection exceeded its time budget")
                return value

            for subscription in subscriptions:
                checkout_id = str(subscription["checkout_id"])
                self._last_checkout = checkout_id
                source = self.store.source(checkout_id)
                if source is None or not source["active_generation"]:
                    # An interrupted first build has no baseline to advance.
                    # Later intents alone must never become a partial baseline.
                    continue
                with self.store.connection() as con:
                    cursor = con.execute(
                        "SELECT outbox_id FROM projection_cursors WHERE checkout_id=?",
                        (checkout_id,),
                    ).fetchone()
                after = int(cursor[0]) if cursor else 0
                with self.control.connection() as con:
                    rows = con.execute(
                        "SELECT * FROM knowledge_outbox WHERE checkout_id=? AND "
                        "outbox_id>? "
                        "ORDER BY outbox_id LIMIT ?",
                        (checkout_id, after, max(1, min(limit, 32))),
                    ).fetchall()
                if not rows:
                    continue
                final = rows[-1]
                job_id = f"outbox:{source['source_id']}:{final['outbox_id']}"
                root = Path(source["canonical_path"])
                self.store.start_job(
                    str(source["source_id"]),
                    checkout_id,
                    root,
                    str(source["base_oid"]),
                    int(final["event_sequence"]),
                    job_id,
                )
                documents = self.store.documents(str(source["source_id"]))
                root = Path(source["canonical_path"])
                receipts = self._receipts(checkout_id, outbox_rows=rows)
                self._apply_receipts(root, documents, receipts, remaining=remaining)
                self._filter(root, documents, remaining=remaining)
                self.store.replace(
                    source_id=str(source["source_id"]),
                    checkout_id=checkout_id,
                    root=root,
                    base_oid=str(source["base_oid"]),
                    sequence=int(final["event_sequence"]),
                    outbox_id=int(final["outbox_id"]),
                    documents=documents,
                    job_id=job_id,
                    remaining=remaining,
                )
                job_id = None
                completed += len(rows)
                if time.monotonic() >= deadline:
                    break
            self.error = None
        except _ERRORS as exc:
            self._job_failed(job_id, exc)
        finally:
            self.lock.release()
        return completed

    @staticmethod
    def _document(
        path: str, body: bytes, mode: str, revision: str, expected: str | None = None
    ) -> Document | None:
        if (
            not eligible_path(path)
            or len(body) > MAX_FILE
            or b"\0" in body
            or contains_secret_material(body)
        ):
            return None
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return None
        identity = content_identity(ObjectKind.REGULAR, mode, body)
        if expected is not None and expected != identity:
            raise ValueError("accepted artifact identity mismatch")
        return Document(path, identity, mode, revision, text)

    def _baseline(self, root: Path, oid: str, remaining: Any) -> dict[str, Document]:
        listing = run_git(
            root, ["ls-tree", "-r", "-z", "-l", oid], text=False, remaining=remaining
        )
        assert isinstance(listing.stdout, bytes)
        entries = listing.stdout.split(b"\0")
        if len(entries) > MAX_FILES + 1:
            raise ValueError("lexical baseline exceeds its file budget")
        metadata: list[tuple[str, str, str, int]] = []
        for entry in entries:
            if not entry:
                continue
            raw, raw_path = entry.split(b"\t", 1)
            raw_mode, kind, raw_blob, raw_size = raw.split()
            relative = os.fsdecode(raw_path)
            if (
                kind == b"blob"
                and raw_mode in {b"100644", b"100755"}
                and int(raw_size) <= MAX_FILE
                and eligible_path(relative)
            ):
                metadata.append(
                    (relative, raw_mode.decode(), raw_blob.decode(), int(raw_size))
                )
        excluded = excluded_paths(
            root, [entry[0] for entry in metadata], remaining=remaining
        )
        documents: dict[str, Document] = {}
        consumed = 0
        for path, mode, blob, size in metadata:
            remaining()
            if path in excluded:
                continue
            consumed += size
            if consumed > MAX_BYTES:
                raise ValueError("lexical baseline exceeds its byte budget")
            read = run_git(
                root, ["cat-file", "blob", blob], text=False, remaining=remaining
            )
            assert isinstance(read.stdout, bytes)
            document = self._document(path, read.stdout, mode, "git:" + oid)
            if document:
                documents[path] = document
        return documents

    def _receipts(
        self,
        checkout: str,
        *,
        sequence: int | None = None,
        outbox_rows: list[sqlite3.Row] | None = None,
    ) -> list[tuple[int, str, str | None, str | None, str | None]]:
        result: list[tuple[int, str, str | None, str | None, str | None]] = []
        with self.control.connection() as con:
            if outbox_rows is None:
                batches = con.execute(
                    "SELECT b.* FROM workspace_batches b JOIN workspaces w "
                    "USING(workspace_id) "
                    "WHERE b.checkout_id=? AND w.mode='shared' AND "
                    "b.state='published' AND b.checkout_event_sequence<=? "
                    "ORDER BY b.checkout_event_sequence LIMIT 20001",
                    (checkout, sequence),
                ).fetchall()
                versions = con.execute(
                    "SELECT o.event_sequence,v.* FROM knowledge_outbox o JOIN "
                    "knowledge_external_versions v USING(version_id) WHERE "
                    "o.checkout_id=? AND o.event_sequence<=? "
                    "ORDER BY o.event_sequence LIMIT 20001",
                    (checkout, sequence),
                ).fetchall()
            else:
                batch_ids = [
                    str(row["batch_id"]) for row in outbox_rows if row["batch_id"]
                ]
                version_ids = [
                    str(row["version_id"]) for row in outbox_rows if row["version_id"]
                ]
                batches = []
                versions = []
                for batch in batch_ids:
                    batches.extend(
                        con.execute(
                            "SELECT * FROM workspace_batches WHERE batch_id=? AND "
                            "state='published'",
                            (batch,),
                        ).fetchall()
                    )
                for version in version_ids:
                    versions.extend(
                        con.execute(
                            "SELECT o.event_sequence,v.* FROM "
                            "knowledge_external_versions v "
                            "JOIN knowledge_outbox o USING(version_id) WHERE "
                            "v.version_id=?",
                            (version,),
                        ).fetchall()
                    )
            if len(batches) + len(versions) > 20_000:
                raise ValueError(
                    "lexical accepted history exceeds its bounded build budget"
                )
            for batch in batches:
                value = json.loads(batch["request_json"])
                for item in value["files"]:
                    identity = item["result"]
                    mode = identity["mode"]
                    result.append(
                        (
                            int(batch["checkout_event_sequence"]),
                            item["relative_path"],
                            item["content_hash"]
                            if identity["kind"] == "regular"
                            else None,
                            str(identity["digest"])
                            if identity["kind"] == "regular"
                            else None,
                            mode,
                        )
                    )
            for version in versions:
                identity = (
                    json.loads(version["identity_json"])
                    if version["identity_json"] and version["identity_json"] != "opaque"
                    else None
                )
                result.append(
                    (
                        int(version["event_sequence"]),
                        str(version["relative_path"]),
                        version["content_hash"],
                        str(identity[2])
                        if identity and identity[0] == "regular"
                        else None,
                        str(identity[1]) if identity else None,
                    )
                )
        # Only the newest accepted version of each path can contribute to this
        # projection. Historical intermediates do not consume artifact budgets.
        latest = {item[1]: item for item in sorted(result, key=lambda item: item[0])}
        return sorted(latest.values(), key=lambda item: (item[0], item[1]))

    def _apply_receipts(
        self,
        root: Path,
        documents: dict[str, Document],
        receipts: list[tuple[int, str, str | None, str | None, str | None]],
        *,
        current_only: bool = False,
        remaining: Any = None,
    ) -> None:
        consumed = 0
        for sequence, path, artifact, identity, mode in receipts:
            if remaining is not None:
                remaining()
            if artifact is None or identity is None or mode not in {"100644", "100755"}:
                if current_only:
                    baseline = documents.get(path)
                    if baseline is not None and current_matches(root, baseline):
                        # An earlier opaque version cannot veto a newer safe
                        # immutable Git version verified against current bytes.
                        continue
                    if mode is not None and (root / path).exists():
                        continue
                documents.pop(path, None)
                continue
            if not eligible_path(path):
                documents.pop(path, None)
                continue
            body = load_candidate_content(self.candidate_dir, artifact)
            consumed += len(body)
            if consumed > MAX_BYTES:
                raise ValueError("lexical accepted artifacts exceed their byte budget")
            document = self._document(
                path, body, mode, "checkout-event:" + str(sequence), expected=identity
            )
            if document is None:
                documents.pop(path, None)
            elif not current_only or current_matches(root, document):
                documents[path] = document

    @staticmethod
    def _filter(
        root: Path, documents: dict[str, Document], *, remaining: Any = None
    ) -> None:
        excluded = excluded_paths(root, list(documents), remaining=remaining)
        for path in excluded:
            documents.pop(path, None)
        if (
            sum(len(document.text.encode()) for document in documents.values())
            > MAX_BYTES
            or len(documents) > MAX_FILES
        ):
            raise ValueError("lexical generation exceeds its bounded source budget")
