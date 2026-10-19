"""Bounded, read-only graph discovery with source-policy revalidation."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from llm_cli.agent.source_policy import contains_secret_material, excluded_paths
from llm_cli.knowledge.parsers import EDGE_TYPES, PROJECTION_VERSION
from llm_cli.knowledge.search import current_state, eligible_path
from llm_cli.knowledge.store import Document, KnowledgeStore

MAX_NODES = 100
MAX_EDGES = 200


def _node(con: sqlite3.Connection, source: int, key: str) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT "
        "n.node_id,n.kind,n.name,n.start_line,d.relative_path,d.content_identity,"
        "d.mode,d.indexed_revision FROM knowledge_graph_nodes n JOIN "
        "knowledge_graph_files f ON f.file_key=n.document_id JOIN "
        "knowledge_documents d ON d.document_id=f.document_id "
        "WHERE n.source_key=? AND n.node_id IN "
        "(SELECT ? UNION SELECT node_id FROM knowledge_graph_aliases "
        "WHERE source_key=? AND alias=?) "
        "ORDER BY n.node_id LIMIT ?",
        (source, key, source, key, MAX_NODES + 1),
    ).fetchall()
    if rows:
        return [dict(row) for row in rows]
    kind = "batch" if key.startswith("batch:") else "module"
    return [{"node_id": key, "kind": kind, "name": key.partition(":")[2]}]


def _seeds(con: sqlite3.Connection, source: int, node: str) -> list[str]:
    keys = (node, "file:" + node, "python:" + node, "python-symbol:" + node)
    placeholders = ",".join("?" for _ in keys)
    rows = con.execute(
        "SELECT node_id FROM knowledge_graph_nodes WHERE source_key=? "
        f"AND node_id IN ({placeholders}) "
        "UNION SELECT node_id FROM knowledge_graph_aliases WHERE "
        f"source_key=? AND alias IN ({placeholders}) "
        "ORDER BY node_id LIMIT ?",
        (source, *keys, source, *keys, MAX_NODES + 1),
    ).fetchall()
    if rows:
        return [str(row[0]) for row in rows]
    # A unique repository-relative suffix is convenient for src-layout projects.
    # Escaping keeps input literal; ambiguity never chooses an arbitrary module.
    escaped = node.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = con.execute(
        "SELECT 'file:' || relative_path FROM knowledge_graph_files "
        "WHERE source_key=? AND relative_path LIKE ? ESCAPE '\\' "
        "ORDER BY relative_path LIMIT 2",
        (source, "%/" + escaped),
    ).fetchall()
    if len(rows) == 1:
        return [str(rows[0][0])]
    if node.startswith(("batch:", "python:", "javascript:", "javascript-package:")):
        return [node]
    return []


def neighbors(
    store: KnowledgeStore,
    checkout: str,
    node: str,
    *,
    edge: str | None = None,
    depth: int = 1,
    current_root: Path | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(node, str)
        or not node.strip()
        or len(node) > 500
        or contains_secret_material(node)
    ):
        raise ValueError(
            "graph node must be nonempty safe text of at most 500 characters"
        )
    if edge is not None and edge not in EDGE_TYPES:
        raise ValueError("unknown graph edge type")
    if type(depth) is not int or not 1 <= depth <= 4:
        raise ValueError("graph depth must be between 1 and 4")
    source = store.source(checkout)
    if source is None or not source["active_generation"]:
        return {"state": "not_built", "nodes": [], "edges": []}
    source_id = str(source["source_id"])
    root = current_root or Path(source["canonical_path"])
    deadline = time.monotonic() + 2

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise ValueError("graph query exceeded its time budget")
        return value

    states: dict[str, str] = {}

    def check_states(items: list[dict[str, Any] | sqlite3.Row]) -> None:
        documents: dict[str, dict[str, Any] | sqlite3.Row] = {}
        for item in items:
            # sqlite3.Row membership checks values; inspect its column names.
            keys = item.keys()
            if "relative_path" in keys and item["relative_path"] not in states:
                documents[str(item["relative_path"])] = item
        if not documents:
            return
        excluded = excluded_paths(root, sorted(documents), remaining=remaining)
        for path, item in documents.items():
            remaining()
            states[path] = (
                "private"
                if path in excluded or not eligible_path(path)
                else current_state(
                    root,
                    Document(
                        path,
                        str(item["content_identity"]),
                        str(item["mode"]),
                        str(item["indexed_revision"]),
                        "",
                    ),
                )
            )

    def state(item: dict[str, Any] | sqlite3.Row) -> str:
        return states[str(item["relative_path"])]

    with store.connection() as con:
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1_000)
        version = con.execute(
            "SELECT value FROM store_metadata WHERE key=?",
            ("graph_version:" + source_id,),
        ).fetchone()
        if version is None or version[0] != PROJECTION_VERSION:
            return {
                "state": "not_built",
                "notice": "Run index rebuild to build this code graph.",
                "nodes": [],
                "edges": [],
            }
        graph_source = con.execute(
            "SELECT source_key FROM knowledge_graph_sources WHERE source_id=?",
            (source_id,),
        ).fetchone()
        source_key = int(graph_source[0]) if graph_source else 0
        frontier = _seeds(con, source_key, node)
        truncated = len(frontier) > MAX_NODES
        frontier = frontier[:MAX_NODES]
        node_cache: dict[str, list[dict[str, Any]]] = {
            seed: _node(con, source_key, seed) for seed in frontier
        }
        check_states([value for values in node_cache.values() for value in values])
        frontier = [
            seed
            for seed in frontier
            if any(
                "relative_path" not in value or state(value) != "private"
                for value in node_cache[seed]
            )
        ]
        seeds = list(frontier)
        seen: set[str] = set()
        found: dict[tuple[str, int], dict[str, Any]] = {}
        for distance in range(1, depth + 1):
            if not frontier:
                break
            remaining()
            keys = set(frontier[:MAX_NODES])
            placeholders = ",".join("?" for _ in keys)
            aliases = con.execute(
                "SELECT alias FROM knowledge_graph_aliases WHERE source_key=? "
                f"AND node_id IN ({placeholders})",
                (source_key, *sorted(keys)),
            ).fetchall()
            keys.update(str(row[0]) for row in aliases)
            placeholders = ",".join("?" for _ in keys)
            condition = " AND e.edge_type=?" if edge else ""
            rows = con.execute(
                "SELECT "
                "e.ordinal,e.source_node,e.target_node,e.edge_type,e.start_line,"
                "e.generation_id,d.document_id,d.source_id,d.relative_path,"
                "d.content_identity,d.mode,d.indexed_revision,"
                "f.parser,f.parser_version FROM knowledge_graph_edges e "
                "JOIN knowledge_graph_files f ON f.file_key=e.document_id "
                "JOIN knowledge_documents d ON d.document_id=f.document_id "
                f"WHERE e.source_key=? AND (e.source_node IN ({placeholders}) OR "
                f"e.target_node IN ({placeholders})){condition} "
                "ORDER BY d.relative_path,e.ordinal LIMIT ?",
                (
                    source_key,
                    *sorted(keys),
                    *sorted(keys),
                    *((edge,) if edge else ()),
                    MAX_EDGES + 1,
                ),
            ).fetchall()
            truncated = truncated or len(rows) > MAX_EDGES
            for row in rows[:MAX_EDGES]:
                for reference in (str(row["source_node"]), str(row["target_node"])):
                    if reference not in node_cache:
                        node_cache[reference] = _node(con, source_key, reference)
            check_states(
                [
                    *rows[:MAX_EDGES],
                    *(value for values in node_cache.values() for value in values),
                ]
            )
            next_frontier: set[str] = set()
            for row in rows[:MAX_EDGES]:
                if state(row) == "private":
                    continue
                identity = (str(row["document_id"]), int(row["ordinal"]))
                if identity in found:
                    continue
                item = dict(row)
                item["depth"] = distance
                item["direction"] = (
                    "outgoing" if row["source_node"] in keys else "incoming"
                )
                found[identity] = item
                for reference in (str(row["source_node"]), str(row["target_node"])):
                    next_frontier.update(
                        str(value["node_id"])
                        for value in node_cache[reference]
                        if "relative_path" not in value or state(value) != "private"
                    )
                if len(found) >= MAX_EDGES:
                    truncated = True
                    break
            seen.update(frontier)
            frontier = sorted(next_frontier - seen)
            if len(seen) + len(frontier) > MAX_NODES:
                truncated = True
                frontier = frontier[: max(0, MAX_NODES - len(seen))]
        for seed in seeds:
            node_cache.setdefault(seed, _node(con, source_key, seed))
        all_nodes = {
            str(value["node_id"]): value
            for values in node_cache.values()
            for value in values
        }
        failures = con.execute(
            "SELECT "
            "f.relative_path,f.parser,f.parser_version,f.error_code,d.content_identity,"
            "d.mode,d.indexed_revision FROM knowledge_graph_files f JOIN "
            "knowledge_documents d "
            "USING(document_id) WHERE f.source_key=? AND f.error_code IS NOT NULL "
            "ORDER BY f.relative_path LIMIT 21",
            (source_key,),
        ).fetchall()
        check_states([*failures, *all_nodes.values()])
    visible = {
        key: value
        for key, value in all_nodes.items()
        if "relative_path" not in value or state(value) != "private"
    }
    results: list[dict[str, Any]] = []
    used = set(seeds) & visible.keys()
    for item in found.values():
        if state(item) == "private":
            continue
        left = [
            value
            for value in node_cache[item["source_node"]]
            if value["node_id"] in visible
        ]
        right = [
            value
            for value in node_cache[item["target_node"]]
            if value["node_id"] in visible
        ]
        for source_node in left:
            for target_node in right:
                if (
                    len(results) >= MAX_EDGES
                    or len(used | {source_node["node_id"], target_node["node_id"]})
                    > MAX_NODES
                ):
                    truncated = True
                    continue
                used.update((source_node["node_id"], target_node["node_id"]))
                results.append(
                    {
                        "source": source_node["node_id"],
                        "target": target_node["node_id"],
                        "type": item["edge_type"],
                        "direction": item["direction"],
                        "depth": item["depth"],
                        "path": item["relative_path"],
                        "start_line": item["start_line"],
                        "parser": item["parser"],
                        "parser_version": item["parser_version"],
                        "content_identity": item["content_identity"],
                        "indexed_revision": item["indexed_revision"],
                        "generation": source["active_generation"],
                        "indexed_generation": item["generation_id"],
                        "stale": state(item) != "current",
                    }
                )
    output_nodes = [
        {
            key: value
            for key, value in visible[identifier].items()
            if key in {"node_id", "kind", "name", "start_line", "relative_path"}
        }
        for identifier in sorted(used)
    ]
    parse_failures = [
        {
            key: item[key]
            for key in (
                "relative_path",
                "parser",
                "parser_version",
                "error_code",
                "content_identity",
                "indexed_revision",
            )
        }
        for item in failures[:20]
        if state(item) != "private"
    ]
    return {
        "state": "ready",
        "node": node,
        "checkout_id": checkout,
        "generation": source["active_generation"],
        "nodes": output_nodes,
        "edges": results,
        "parse_failures": parse_failures,
        "truncated": truncated or len(failures) > 20,
    }
