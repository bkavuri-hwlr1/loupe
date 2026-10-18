"""Code graph transitions are optional, atomic and independently evidenced."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_knowledge_index import _index
from test_shared_agent_execution import _request
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.cli.app import _graph_neighbors_text, build_parser, dispatch
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.knowledge import store as store_module
from llm_cli.knowledge.indexer import KnowledgeIndex
from llm_cli.knowledge.parsers import PROJECTION_VERSION, PYTHON_VERSION, VERSION
from llm_cli.storage.backup import backup_database
from llm_cli.storage.migrations import KNOWLEDGE_MIGRATIONS, apply_migrations
from llm_cli.workspace.batches import BatchFile
from llm_cli.workspace.context import SharedCoordinationContext
from llm_cli.workspace.identity import ABSENT, REGULAR_MODE, identify_path

FILES = {
    "src/pkg/workspace/batches.py": "class Batch:\n    def publish(self): pass\n",
    "src/pkg/consumer.py": "from .workspace.batches import Batch\n",
    "tests/test_batches.py": "from pkg.workspace.batches import Batch\n",
    "web/work.ts": "export const work = () => 1;\n",
    "web/tests/work.test.ts": "import {work} from '../work';\n",
    "broken.py": "def invalid(:\n",
}


def _prepare(env: BatchEnvironment, git_run: Callable[..., str]) -> None:
    for path, text in FILES.items():
        target = env.repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git_run(env.repository, "add", ".")
    git_run(env.repository, "commit", "-m", "code graph fixture")


def _publish(env: BatchEnvironment, path: str, body: bytes | None, batch: str) -> None:
    file = BatchFile(
        path,
        identify_path(env.repository / path),
        body,
        REGULAR_MODE if body is not None else ABSENT.mode,
    )
    result = env.publisher.publish(batch, "first", (file,), ("*",))
    assert result.state == "published", result


def test_neighbors_importers_tests_symbols_and_deterministic_evidence(
    batch_env: BatchEnvironment, tmp_path: Path, git_run: Callable[..., str]
) -> None:
    env = batch_env
    _prepare(env, git_run)
    before = SharedCoordinationContext(env.store, "first", ("*",), scan_external=False)(
        None
    )
    index, checkout = _index(env, tmp_path)
    built = index.build(checkout)
    assert built["state"] == "ready", built
    result = index.graph_neighbors(checkout, "workspace/batches.py")
    assert result["state"] == "ready", result
    edges = result["edges"]
    assert any(
        edge["path"] == "src/pkg/consumer.py"
        and edge["type"] == "imports"
        and edge["direction"] == "incoming"
        for edge in edges
    )
    assert any(
        edge["path"] == "tests/test_batches.py" and edge["type"] == "tests"
        for edge in edges
    )
    for edge in edges:
        assert edge["parser"] == "python-ast"
        assert edge["parser_version"] == PYTHON_VERSION
        assert edge["content_identity"]
        assert edge["generation"] == edge["indexed_generation"] == built["generation"]
        assert edge["indexed_revision"].startswith("git:")
        assert not edge["stale"]
    assert result["parse_failures"][0]["relative_path"] == "broken.py"
    assert result["parse_failures"][0]["error_code"] == "SyntaxError"
    assert index.search_lexical(checkout, "invalid")["results"]
    assert index.graph_neighbors(checkout, "pkg.workspace.batches", edge="tests")[
        "edges"
    ]
    definitions = index.graph_neighbors(
        checkout, "src/pkg/workspace/batches.py", edge="defines", depth=3
    )
    assert {node["kind"] for node in definitions["nodes"]} == {
        "file",
        "module",
        "class",
        "function",
    }
    rendered = _graph_neighbors_text(definitions)
    assert "pkg.workspace.batches" in rendered and "Batch.publish" in rendered
    javascript = index.graph_neighbors(checkout, "web/work.ts", edge="tests")
    assert javascript["edges"][0]["source"] == "file:web/tests/work.test.ts"
    assert javascript["edges"][0]["parser_version"] == VERSION
    assert before == SharedCoordinationContext(
        env.store, "first", ("*",), scan_external=False
    )(None)
    rebuilt = index.build(checkout, rebuild=True)
    assert rebuilt["generation"] == built["generation"]
    assert index.graph_neighbors(checkout, "workspace/batches.py") == result


def test_changed_import_and_delete_remove_old_edges_without_reparsing_other_files(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = batch_env
    _prepare(env, git_run)
    index, checkout = _index(env, tmp_path)
    built = index.build(checkout)
    assert built["state"] == "ready"
    with index.store.connection() as con:
        for table in (
            "knowledge_graph_files",
            "knowledge_graph_nodes",
            "knowledge_graph_aliases",
            "knowledge_graph_edges",
        ):
            con.execute(
                f"CREATE TRIGGER protect_{table} BEFORE DELETE ON {table} "
                "WHEN OLD.document_id IN (SELECT document_id FROM knowledge_documents "
                "WHERE relative_path='tests/test_batches.py') BEGIN "
                "SELECT RAISE(ABORT,'unchanged graph rewrite'); END"
            )
    calls: list[str] = []
    original = store_module.parse

    def record(path: str, text: str) -> Any:
        calls.append(path)
        return original(path, text)

    monkeypatch.setattr(store_module, "parse", record)
    _publish(env, "src/pkg/consumer.py", b"import os\n", "graph-change")
    assert index.process_pending() == 1
    assert calls == ["src/pkg/consumer.py"]
    result = index.graph_neighbors(checkout, "workspace/batches.py", edge="imports")
    assert result["generation"] != built["generation"]
    assert not any(item["path"] == "src/pkg/consumer.py" for item in result["edges"])
    assert any(item["path"] == "tests/test_batches.py" for item in result["edges"])
    changed = index.graph_neighbors(checkout, "src/pkg/consumer.py", edge="changed-by")
    assert changed["edges"][0]["target"] == "batch:graph-change"
    assert changed["edges"][0]["indexed_revision"].startswith("checkout-event:")
    _publish(env, "src/pkg/consumer.py", None, "graph-delete")
    assert index.process_pending() == 1
    assert not index.graph_neighbors(checkout, "src/pkg/consumer.py")["edges"]
    assert calls == ["src/pkg/consumer.py"]


def test_graph_failure_rolls_back_lexical_rows_and_cursor_then_retries(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = batch_env
    _prepare(env, git_run)
    index, checkout = _index(env, tmp_path)
    initial = index.build(checkout)
    _publish(env, "src/pkg/consumer.py", b"import changed\n", "graph-rollback")
    original = index.store._insert_graph

    def fail(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise ValueError("injected")

    with monkeypatch.context() as patch:
        patch.setattr(index.store, "_insert_graph", fail)
        assert index.process_pending() == 0
    with index.store.connection() as con:
        assert (
            con.execute("SELECT active_generation FROM knowledge_sources").fetchone()[0]
            == initial["generation"]
        )
        assert (
            con.execute("SELECT outbox_id FROM projection_cursors").fetchone()[0] == 0
        )
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert index.process_pending() == 1
    assert index.graph_neighbors(checkout, "src/pkg/consumer.py", edge="changed-by")[
        "edges"
    ]


def test_graph_queries_hide_ignored_and_newly_private_sources_and_mark_safe_staleness(
    batch_env: BatchEnvironment, tmp_path: Path, git_run: Callable[..., str]
) -> None:
    env = batch_env
    _prepare(env, git_run)
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    (env.repository / "src/pkg/consumer.py").write_text("import safe_local_edit\n")
    stale = index.graph_neighbors(checkout, "workspace/batches.py", edge="imports")
    assert any(
        item["stale"]
        for item in stale["edges"]
        if item["path"] == "src/pkg/consumer.py"
    )
    (env.repository / "src/pkg/consumer.py").write_text(
        "token = '" + "sk-" + "a" * 40 + "'\n"
    )
    (env.repository / "tests/.gitignore").write_text("test_batches.py\n")
    result = index.graph_neighbors(checkout, "workspace/batches.py")
    serialized = json.dumps(result)
    assert "src/pkg/consumer.py" not in serialized
    assert "tests/test_batches.py" not in serialized


def test_off_and_missing_graph_do_not_create_optional_files(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    index, checkout = _index(batch_env, tmp_path, enabled=False)
    assert index.graph_neighbors(checkout, "a.txt")["state"] == "off"
    assert not index.store.path.exists()


def test_graph_schema_upgrade_requires_explicit_rebuild_and_backup_is_complete(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    with index.store.connection() as con:
        for table in (
            "knowledge_graph_edges",
            "knowledge_graph_aliases",
            "knowledge_graph_nodes",
            "knowledge_graph_files",
        ):
            con.execute("DROP TABLE " + table)
        con.execute("DELETE FROM schema_migrations WHERE version=4")
        con.execute("DELETE FROM store_metadata WHERE key LIKE 'graph_version:%'")
        assert apply_migrations(con, KNOWLEDGE_MIGRATIONS) == 3
    index.initialize()
    result = index.graph_neighbors(checkout, "a.txt")
    assert result["state"] == "not_built" and "rebuild" in result["notice"]
    assert index.search_lexical(checkout, "old")["results"]
    assert index.build(checkout, rebuild=True)["state"] == "ready"
    metadata = backup_database(index.store.path, tmp_path / "graph-backup.sqlite3")
    assert metadata.schema_version == 4
    with index.store.connection() as con:
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    index.drop(checkout)
    with index.store.connection() as con:
        assert (
            con.execute("SELECT count(*) FROM knowledge_graph_nodes").fetchone()[0] == 0
        )


def test_cli_rpc_graph_lifecycle_and_parameter_validation(
    tmp_path: Path, repository_factory: Any, service_factory: Any
) -> None:
    async def scenario() -> None:
        repo = repository_factory(tmp_path, FILES)
        service = service_factory(tmp_path)
        service.knowledge = KnowledgeIndex(
            service.store,
            service.paths.knowledge_db,
            service.paths.candidate_dir,
            enabled=True,
        )
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repo)}))
            assert (await service.handle(_request("index.build", {"path": str(repo)})))[
                "state"
            ] == "ready"
            params = {
                "path": str(repo),
                "node": "workspace/batches.py",
                "edge": "tests",
                "depth": 1,
            }
            result = await service.handle(_request("graph.neighbors", params))
            assert result["edges"][0]["type"] == "tests"
            absent = await service.handle(
                _request("graph.neighbors", {**params, "node": "does-not-exist.py"})
            )
            assert absent["state"] == "ready" and not absent["edges"]
            for depth in (0, 5, True, "bad"):
                with pytest.raises(LlmCoordError) as caught:
                    await service.handle(
                        _request("graph.neighbors", {**params, "depth": depth})
                    )
                assert caught.value.code == ErrorCode.CONFIG_INVALID
            invalid = await service.handle(
                _request("graph.neighbors", {**params, "edge": "unsafe"})
            )
            assert invalid["state"] == "unavailable"
            assert (
                await service.handle(
                    _request("search.lexical", {"path": str(repo), "query": "Batch"})
                )
            )["state"] == "ready"
        finally:
            service.close()

    asyncio.run(scenario())

    class Client:
        def call(self, method: str, params: Any = None) -> dict[str, Any]:
            return {"method": method, "params": params}

    args = build_parser().parse_args(
        [
            "--json",
            "graph",
            "neighbors",
            "workspace/batches.py",
            "--edge",
            "tests",
            "--depth",
            "2",
        ]
    )
    result = dispatch(args, Client())
    assert result["method"] == "graph.neighbors"
    assert result["params"]["depth"] == 2


def test_private_intermediate_files_are_not_traversed(
    batch_env: BatchEnvironment, tmp_path: Path, git_run: Callable[..., str]
) -> None:
    env = batch_env
    for path, text in {
        "start.py": "import middle\n",
        "middle.py": "import end\n",
        "end.py": "import unrelated\n",
    }.items():
        (env.repository / path).write_text(text)
    git_run(env.repository, "add", ".")
    git_run(env.repository, "commit", "-m", "graph privacy bridge")
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    (env.repository / "middle.py").write_text("token = '" + "sk-" + "a" * 40 + "'\n")
    result = index.graph_neighbors(checkout, "start.py", depth=4)
    assert not any(item["path"] in {"middle.py", "end.py"} for item in result["edges"])
    private = index.graph_neighbors(checkout, "middle.py", depth=4)
    assert not private["nodes"] and not private["edges"]


def test_ambiguous_symbol_seeds_obey_the_total_node_cap(
    batch_env: BatchEnvironment, tmp_path: Path, git_run: Callable[..., str]
) -> None:
    env = batch_env
    (env.repository / "many.py").write_text("def repeated(): pass\n" * 120)
    git_run(env.repository, "add", ".")
    git_run(env.repository, "commit", "-m", "repeated symbol graph fixture")
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    result = index.graph_neighbors(checkout, "many.repeated", depth=4)
    assert result["state"] == "ready" and result["truncated"]
    assert len(result["nodes"]) <= 100
    assert len(result["edges"]) <= 200


def test_graph_grammar_upgrade_marker_preserves_lexical_search_until_rebuild(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    index, checkout = _index(batch_env, tmp_path)
    built = index.build(checkout)
    assert built["state"] == "ready"
    with index.store.connection() as con:
        source = con.execute("SELECT source_id FROM knowledge_sources").fetchone()[0]
        con.execute(
            "UPDATE store_metadata SET value='old-grammar' WHERE key=?",
            ("graph_version:" + source,),
        )
    assert index.graph_neighbors(checkout, "a.txt")["state"] == "not_built"
    assert index.search_lexical(checkout, "old")["results"]
    assert index.build(checkout, rebuild=True)["state"] == "ready"
    with index.store.connection() as con:
        assert (
            con.execute(
                "SELECT value FROM store_metadata WHERE key=?",
                ("graph_version:" + source,),
            ).fetchone()[0]
            == PROJECTION_VERSION
        )
    assert index.graph_neighbors(checkout, "a.txt")["state"] == "ready"


def test_same_identity_publication_changes_receipt_without_reparsing(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = batch_env
    _prepare(env, git_run)
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    body = b"import os\n"
    _publish(env, "src/pkg/consumer.py", body, "graph-first-receipt")
    assert index.process_pending() == 1
    with index.store.connection() as con:
        original = [
            tuple(row)
            for row in con.execute(
                "SELECT * FROM knowledge_graph_edges WHERE edge_type='imports' "
                "ORDER BY document_id,ordinal"
            )
        ]

    def forbidden(*args: Any) -> Any:
        pytest.fail("a same-identity publication must preserve parsed graph rows")

    monkeypatch.setattr(store_module, "parse", forbidden)
    _publish(env, "src/pkg/consumer.py", body, "graph-noop-receipt")
    assert index.process_pending() == 1
    result = index.graph_neighbors(checkout, "src/pkg/consumer.py", edge="changed-by")
    assert [edge["target"] for edge in result["edges"]] == ["batch:graph-noop-receipt"]
    with index.store.connection() as con:
        assert [
            tuple(row)
            for row in con.execute(
                "SELECT * FROM knowledge_graph_edges WHERE edge_type='imports' "
                "ORDER BY document_id,ordinal"
            )
        ] == original


def test_graph_query_budget_expiry_is_read_only_and_keeps_lexical_healthy(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.knowledge import graph as graph_module

    index, checkout = _index(batch_env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    with index.store.connection() as con:
        before = tuple(
            con.execute("SELECT active_generation FROM knowledge_sources").fetchone()
        )
        cursor = tuple(
            con.execute(
                "SELECT event_sequence,outbox_id FROM projection_cursors"
            ).fetchone()
        )

    class ExpiredClock:
        calls = 0

        def monotonic(self) -> float:
            self.calls += 1
            return 100.0 if self.calls == 1 else 103.0

    with monkeypatch.context() as patch:
        patch.setattr(graph_module, "time", ExpiredClock())
        result = index.graph_neighbors(checkout, "a.txt")
    assert result["state"] == "unavailable"
    assert result["nodes"] == result["edges"] == []
    assert index.error is None
    with index.store.connection() as con:
        assert (
            tuple(
                con.execute(
                    "SELECT active_generation FROM knowledge_sources"
                ).fetchone()
            )
            == before
        )
        assert (
            tuple(
                con.execute(
                    "SELECT event_sequence,outbox_id FROM projection_cursors"
                ).fetchone()
            )
            == cursor
        )
    assert index.search_lexical(checkout, "old")["state"] == "ready"
    assert index.search_lexical(checkout, "old")["results"]


def test_duplicate_imports_obey_edge_budget_even_with_only_two_nodes(
    batch_env: BatchEnvironment, tmp_path: Path, git_run: Callable[..., str]
) -> None:
    env = batch_env
    (env.repository / "many.ts").write_text("import 'same-package';\n" * 250)
    git_run(env.repository, "add", ".")
    git_run(env.repository, "commit", "-m", "duplicate import fixture")
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    result = index.graph_neighbors(checkout, "many.ts", edge="imports")
    assert result["state"] == "ready" and result["truncated"]
    assert len(result["nodes"]) == 2
    assert 0 < len(result["edges"]) <= 200
    assert all(
        edge["target"] == "javascript-package:same-package" for edge in result["edges"]
    )
