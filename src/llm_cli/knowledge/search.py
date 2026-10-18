"""Scoped lexical retrieval with exact citations and current-identity revalidation."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from llm_cli.agent.source_policy import contains_secret_material, excluded_paths
from llm_cli.errors import LlmCoordError
from llm_cli.knowledge.store import Document, KnowledgeStore
from llm_cli.workspace.identity import ObjectKind, content_identity
from llm_cli.workspace.isolated import _read, _source

MAX_FILE = 1024 * 1024
_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".loupe",
        ".llm-coord",
        ".codex",
        ".agents",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
    }
)


def eligible_path(path: str) -> bool:
    from llm_cli.coordination.scopes import normalize_changed_path

    try:
        return normalize_changed_path(path) == path and not any(
            part.casefold() in _EXCLUDED_PARTS for part in Path(path).parts
        )
    except ValueError:
        return False


def current_state(root: Path, document: Document) -> str:
    try:
        with _source(root, document.path) as descriptor:
            import os
            import stat

            mode = (
                "100755"
                if stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o111
                else "100644"
            )
            body = _read(descriptor, MAX_FILE)
            if contains_secret_material(body):
                return "private"
            identity = content_identity(ObjectKind.REGULAR, mode, body)
            return "current" if identity == document.identity else "stale"
    except (OSError, ValueError, RuntimeError, LlmCoordError):
        return "stale"


def current_matches(root: Path, document: Document) -> bool:
    return current_state(root, document) == "current"


def search(
    store: KnowledgeStore,
    checkout: str,
    query: str,
    *,
    limit: int = 8,
    explain: bool = False,
) -> dict[str, Any]:
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > 500
        or contains_secret_material(query)
    ):
        raise ValueError(
            "lexical query must be nonempty, safe text of at most 500 characters"
        )
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("lexical limit must be between 1 and 20")
    source = store.source(checkout)
    if not source or not source["active_generation"]:
        return {"state": "not_built", "results": []}
    terms = re.findall(r"\w+", query, flags=re.UNICODE)[:32]
    if not terms:
        return {"state": "ready", "results": []}
    expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
    deadline = time.monotonic() + 2
    with store.connection() as con:
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1_000)
        rows = con.execute(
            "SELECT c.*,d.content_identity,d.mode,d.indexed_revision,d.generation_id,"
            "bm25(knowledge_fts,3.0,1.0) AS rank FROM knowledge_fts "
            "JOIN knowledge_chunks c ON c.rowid=knowledge_fts.rowid "
            "JOIN knowledge_documents d ON d.document_id=c.document_id "
            "WHERE knowledge_fts MATCH ? AND c.source_id=? "
            "ORDER BY CASE WHEN c.relative_path=? THEN 0 ELSE 1 "
            "END,rank,c.relative_path,c.ordinal LIMIT ?",
            (expression, source["source_id"], query, limit * 4),
        ).fetchall()
    root = Path(source["canonical_path"])
    excluded = excluded_paths(
        root,
        [str(row["relative_path"]) for row in rows],
        remaining=lambda: max(0.01, deadline - time.monotonic()),
    )
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        path = str(row["relative_path"])
        if path in excluded or not eligible_path(path) or path in seen:
            continue
        seen.add(path)
        doc = Document(
            path,
            str(row["content_identity"]),
            str(row["mode"]),
            str(row["indexed_revision"]),
            "",
        )
        state = current_state(root, doc)
        if state == "private":
            continue
        stale = state != "current"
        result: dict[str, Any] = {
            "chunk_id": row["chunk_id"],
            "source_id": source["source_id"],
            "checkout_id": checkout,
            "path": path,
            "content_identity": doc.identity,
            "indexed_revision": doc.revision,
            "generation": source["active_generation"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "text": row["text"],
            "stale": stale,
            "label": "stale indexed version" if stale else "current identity verified",
        }
        if explain:
            result["explain"] = {
                "arm": "lexical",
                "reason": "exact path" if path == query else "FTS5/BM25",
                "rank": row["rank"],
            }
        results.append(result)
        if len(results) >= limit:
            break
    return {
        "state": "ready",
        "results": results,
        "generation": source["active_generation"],
    }
