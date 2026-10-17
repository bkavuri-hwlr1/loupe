"""Lexical projections are immutable discovery facts, never publication authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_shared_agent_execution import (
    ScriptedProvider,
    _open_session,
    _register,
    _request,
)
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.cli.app import build_parser, dispatch
from llm_cli.config.loader import load_settings
from llm_cli.errors import LlmCoordError
from llm_cli.knowledge.chunker import MAX_TEXT_BYTES, chunks
from llm_cli.knowledge.indexer import KnowledgeIndex
from llm_cli.workspace.batches import BatchFile
from llm_cli.workspace.context import SharedCoordinationContext
from llm_cli.workspace.external import ExternalWorkspaceScanner
from llm_cli.workspace.identity import REGULAR_MODE, identify_path


def _index(
    env: BatchEnvironment, tmp_path: Path, *, enabled: bool = True
) -> tuple[KnowledgeIndex, str]:
    workspace = env.store.get_workspace(env.workspace_id)
    assert workspace
    index = KnowledgeIndex(
        env.store,
        tmp_path / "knowledge.sqlite3",
        env.publisher.candidate_dir,
        enabled=enabled,
    )
    index.initialize()
    return index, workspace.checkout_id


def test_off_creates_no_index_or_intents(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    index, checkout = _index(batch_env, tmp_path, enabled=False)
    assert index.build(checkout)["state"] == "off"
    assert index.search_lexical(checkout, "old")["state"] == "off"
    assert not index.store.path.exists()
    batch_env.publisher.publish("off-batch", "first", batch_env.files(), ("*",))
    with batch_env.store.connection() as con:
        assert con.execute("SELECT count(*) FROM knowledge_outbox").fetchone()[0] == 0
        assert (
            con.execute("SELECT count(*) FROM knowledge_subscriptions").fetchone()[0]
            == 0
        )


def test_build_search_citations_and_drop_rebuild_stable_ids(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    index, checkout = _index(batch_env, tmp_path)
    built = index.build(checkout)
    assert built["state"] == "ready", built
    results = index.search_lexical(checkout, "a.txt", explain=True)["results"]
    assert results and results[0]["path"] == "a.txt"
    item = results[0]
    assert item["indexed_revision"].startswith("git:")
    assert (
        item["content_identity"] == identify_path(batch_env.repository / "a.txt").digest
    )
    assert item["start_line"] == 1 and item["end_line"] == 1
    assert item["generation"] == built["generation"] and not item["stale"]
    ids = [result["chunk_id"] for result in results]
    assert index.drop(checkout)["state"] == "dropped"
    assert index.search_lexical(checkout, "old")["state"] == "not_built"
    assert index.build(checkout)["state"] == "ready"
    assert [
        item["chunk_id"] for item in index.search_lexical(checkout, "a.txt")["results"]
    ] == ids


def test_publication_outbox_is_atomic_id_only_and_uses_exact_artifact(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    file = BatchFile(
        "a.txt",
        identify_path(env.repository / "a.txt"),
        b"acceptedmarker\n",
        REGULAR_MODE,
    )
    published = env.publisher.publish("indexed-batch", "first", (file,), ("*",))
    assert published.state == "published"
    with env.store.connection() as con:
        row = con.execute("SELECT * FROM knowledge_outbox").fetchone()
        assert row["batch_id"] == "indexed-batch"
        assert (
            row["event_sequence"]
            == con.execute(
                "SELECT checkout_event_sequence FROM workspace_batches "
                "WHERE batch_id='indexed-batch'"
            ).fetchone()[0]
        )
        assert "acceptedmarker" not in json.dumps(dict(row))
    # A later unobserved save cannot replace the accepted artifact's old bytes.
    (env.repository / "a.txt").write_text("unacceptedlaterbytes\n")
    assert index.process_pending() == 1
    item = index.search_lexical(checkout, "acceptedmarker")["results"][0]
    assert item["path"] == "a.txt" and item["stale"]
    assert item["indexed_revision"].startswith("checkout-event:")
    assert not index.search_lexical(checkout, "unacceptedlaterbytes")["results"]
    assert all(
        item["path"] != "a.txt"
        for item in index.search_lexical(checkout, "old")["results"]
    )


def test_external_change_receipt_is_immutable_and_secret_transition_removes_document(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    env.store.record_workspace_read(
        session_id="first",
        relative_path="a.txt",
        identity=identify_path(env.repository / "a.txt"),
    )
    (env.repository / "a.txt").write_text("outsideacceptedmarker\n")
    scanner = ExternalWorkspaceScanner(env.store)
    assert scanner.scan_session("first") == 1
    (env.repository / "a.txt").write_text("laterunacceptedmarker\n")
    assert index.process_pending() == 1
    item = index.search_lexical(checkout, "outsideacceptedmarker")["results"][0]
    assert item["stale"] and "laterunacceptedmarker" not in item["text"]
    (env.repository / "a.txt").write_text(
        "AWS_ACCESS_KEY_ID=" + "AKIA" + "X" * 16 + "\n"
    )
    assert scanner.scan_session("first") == 1
    assert index.process_pending() == 1
    assert not index.search_lexical(checkout, "outsideacceptedmarker")["results"]
    with index.store.connection() as con:
        assert all("AKIA" + "X" * 16 not in line for line in con.iterdump())


def test_baseline_excludes_ignored_private_screened_binary_and_links(
    tmp_path: Path, repository_factory: Any, service_factory: Any
) -> None:
    async def scenario() -> None:
        canary = "AKIA" + "Z" * 16
        repo = repository_factory(
            tmp_path,
            {
                "safe.py": "def safemarker(): pass\n",
                ".gitignore": "ignored.txt\n",
                ".env": "PRIVATE_CANARY=neverindexme\n",
                "fixtures/credentials_test.py": "key = '" + canary + "'\n",
            },
        )
        (repo / "ignored.txt").write_text("ignoredcanary")
        (repo / "untracked.txt").write_text("unaccepteddirtycanary")
        service = service_factory(tmp_path)
        service.settings = replace(service.settings, knowledge_index="lexical")
        service.knowledge = KnowledgeIndex(
            service.store,
            service.paths.knowledge_db,
            service.paths.candidate_dir,
            enabled=True,
        )
        service.initialize()
        try:
            registered = await service.handle(_request("repo.add", {"path": str(repo)}))
            checkout = registered["checkout"]["checkout_id"]
            result = service.knowledge.build(checkout)
            assert result["state"] == "ready", result
            assert service.knowledge.search_lexical(checkout, "safemarker")["results"]
            with service.knowledge.store.connection() as con:
                dumped = "\n".join(con.iterdump())
            for forbidden in (
                canary,
                "neverindexme",
                "ignoredcanary",
                "unaccepteddirtycanary",
            ):
                assert forbidden not in dumped
        finally:
            service.close()

    asyncio.run(scenario())


def test_optional_corruption_never_blocks_core_initialization_sessions_or_publication(
    tmp_path: Path, repository_factory: Any, service_factory: Any
) -> None:
    async def scenario() -> None:
        repo = repository_factory(tmp_path, {"source.py": "oldmarker\n"})
        service = service_factory(tmp_path)
        service.paths.ensure()
        service.paths.knowledge_db.write_bytes(b"not a sqlite database")
        service.knowledge = KnowledgeIndex(
            service.store,
            service.paths.knowledge_db,
            service.paths.candidate_dir,
            enabled=True,
        )
        service.initialize()
        try:
            registered = await service.handle(_request("repo.add", {"path": str(repo)}))
            assert service.knowledge.status()["state"] == "unavailable"
            status = await service.handle(_request("db.status", {}))
            assert status["control"]["state"] == "available"
            assert status["knowledge"]["state"] == "unavailable"
            doctor = await service.handle(_request("system.doctor", {}))
            assert any(
                check["name"] == "knowledge_index" and not check["ok"]
                for check in doctor["checks"]
            )
            _register(service, ScriptedProvider("index-corrupt-session", []))
            credentials = await _open_session(
                service, repo, ScriptedProvider("index-corrupt-session", [])
            )
            assert service.store.get_session(credentials["session_id"])
            checked = await service.handle(_request("db.check", {}))
            assert checked["control"]["ok"] and not checked["knowledge"]["ok"]
            repaired = await service.handle(
                _request("index.rebuild", {"path": str(repo)})
            )
            assert repaired["state"] == "ready", repaired
            assert service.knowledge.search_lexical(
                registered["checkout"]["checkout_id"], "oldmarker"
            )["results"]
        finally:
            service.close()

    asyncio.run(scenario())


def test_index_failure_during_publication_does_not_change_coordination(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    before = SharedCoordinationContext(env.store, "first", ("*",), scan_external=False)(
        None
    )
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    after = SharedCoordinationContext(env.store, "first", ("*",), scan_external=False)(
        None
    )
    assert before == after
    index.store.path.unlink()
    index.store.path.mkdir()
    result = env.publisher.publish(
        "unwritable-index-batch", "first", env.files(), ("*",)
    )
    assert result.state == "published"
    assert index.process_pending() == 0
    assert index.status()["state"] == "unavailable"
    assert (env.repository / "a.txt").read_text() == "new a\n"


def test_knowledge_setting_and_command_contract(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert load_settings(path).knowledge_index == "off"
    path.write_text('[knowledge]\nindex="lexical"\n')
    assert load_settings(path).knowledge_index == "lexical"
    for value in ('"remote"', "true", "[]"):
        path.write_text("[knowledge]\nindex=" + value + "\n")
        with pytest.raises(LlmCoordError, match=r"knowledge\.index"):
            load_settings(path)
    index = build_parser().parse_args(["index", "build", "--repo", "."])
    assert index.index_command == "build"
    search = build_parser().parse_args(
        ["search", "lexical", "find symbol", "--explain"]
    )
    assert search.query == "find symbol" and search.explain


def test_failed_generation_preserves_search_and_records_failed_job(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index, checkout = _index(batch_env, tmp_path)
    first = index.build(checkout)
    original_clear = index.store._clear

    def fail_after_clear(con: Any, source: str) -> None:
        original_clear(con, source)
        raise ValueError("injected swap interruption")

    monkeypatch.setattr(index.store, "_clear", fail_after_clear)
    assert index.build(checkout)["state"] == "unavailable"
    with index.store.connection() as con:
        assert (
            con.execute("SELECT active_generation FROM knowledge_sources").fetchone()[0]
            == first["generation"]
        )
        assert (
            con.execute("SELECT state FROM projection_jobs").fetchone()[0] == "failed"
        )
        assert con.execute("SELECT count(*) FROM knowledge_chunks").fetchone()[0] > 0
    monkeypatch.setattr(index.store, "_clear", original_clear)
    index.initialize()
    assert index.status()["state"] == "degraded"
    assert index.search_lexical(checkout, "old")["results"]
    assert index.build(checkout)["state"] == "ready"
    assert index.status()["state"] == "ready"


def test_failed_drop_cannot_resurrect_a_disabled_subscription(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index, checkout = _index(batch_env, tmp_path)
    assert index.build(checkout)["state"] == "ready"

    def fail_drop(checkout: str) -> None:
        raise OSError("optional file cannot be changed")

    monkeypatch.setattr(index.store, "drop", fail_drop)
    assert index.drop(checkout)["state"] == "unavailable"
    index.initialize()
    assert index.search_lexical(checkout, "old")["state"] == "not_built"
    assert not index.search_lexical(checkout, "old")["results"]


def test_rebuild_keeps_newer_git_version_instead_of_old_accepted_artifact(
    batch_env: BatchEnvironment, tmp_path: Path, git_run: Callable[..., str]
) -> None:
    env = batch_env
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    env.publisher.publish("historical-batch", "first", env.files(), ("*",))
    (env.repository / "a.txt").write_text("newergitmarker\n")
    git_run(env.repository, "add", "a.txt")
    git_run(env.repository, "commit", "-m", "newer committed version")
    assert index.build(checkout, rebuild=True)["state"] == "ready"
    result = index.search_lexical(checkout, "newergitmarker")["results"][0]
    assert result["indexed_revision"].startswith("git:") and not result["stale"]
    assert "newergitmarker" in result["text"]


def test_long_unicode_chunks_cite_each_actual_piece_line_span() -> None:
    lines = ["a" * 5000 + "\n", "😀" * 3000 + "\n", "tail\n"]
    result = chunks("source", "source.txt", "identity", "".join(lines))
    assert "".join(chunk.text for chunk in result) == "".join(lines)
    assert len(result) == 3
    assert [(part.start_line, part.end_line) for part in result] == [
        (1, 2),
        (2, 2),
        (2, 3),
    ]
    assert all(len(part.text.encode()) <= MAX_TEXT_BYTES for part in result)
    assert result == chunks("source", "source.txt", "identity", "".join(lines))


def test_isolated_origin_publication_targets_primary_index(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    from test_isolated_sessions import _checkout, _manager

    env = batch_env
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    manager = _manager(env, tmp_path)
    workspace, _ = manager.open(_checkout(env), "index-isolated")
    env.store.open_session(
        session_id="index-isolated",
        checkout_id=checkout,
        resume_token_hash=hashlib.sha256(b"index-isolated").hexdigest(),
        provider="fake",
        model="fake",
        workspace_id=workspace.workspace_id,
        workspace_mode="isolated",
    )
    with env.store.connection() as con:
        con.execute(
            "UPDATE sessions SET state='active' WHERE session_id='index-isolated'"
        )
    assert (
        env.publisher.publish(
            "isolated-index-batch", "index-isolated", env.files(), ("*",)
        ).state
        == "published"
    )
    assert index.process_pending() == 1
    with env.store.connection() as con:
        row = con.execute("SELECT * FROM knowledge_outbox").fetchone()
        assert (
            row["checkout_id"] == checkout and row["batch_id"] == "isolated-index-batch"
        )
    result = index.search_lexical(checkout, "new a")["results"]
    assert any(item["path"] == "a.txt" and not item["stale"] for item in result)
    env.store.close_session("index-isolated")
    manager.cleanup_closed()


def test_cli_rpc_index_lifecycle_is_provider_free(
    tmp_path: Path, repository_factory: Any, service_factory: Any
) -> None:
    async def scenario() -> None:
        repo = repository_factory(tmp_path, {"source.py": "def clirpcmarker(): pass\n"})
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
            for method in ("index.build", "index.rebuild"):
                built = await service.handle(_request(method, {"path": str(repo)}))
                assert built["state"] == "ready", built
            result = await service.handle(
                _request(
                    "search.lexical",
                    {"path": str(repo), "query": "clirpcmarker", "explain": True},
                )
            )
            assert result["results"][0]["explain"]["arm"] == "lexical"
            assert (await service.handle(_request("index.status", {})))["sources"]
            assert (await service.handle(_request("index.drop", {"path": str(repo)})))[
                "state"
            ] == "dropped"
            assert (
                await service.handle(
                    _request(
                        "search.lexical", {"path": str(repo), "query": "clirpcmarker"}
                    )
                )
            )["state"] == "not_built"
        finally:
            service.close()

    asyncio.run(scenario())

    class Client:
        paths = None
        timeout_seconds = 90

        def call(self, method: str, params: Any = None) -> dict[str, Any]:
            return {"method": method, "params": params}

    client = Client()
    for action in ("status", "build", "rebuild", "drop"):
        args = build_parser().parse_args(["index", action, "--repo", str(tmp_path)])
        assert dispatch(args, client)["method"] == "index." + action
    args = build_parser().parse_args(
        ["--json", "search", "lexical", "marker", "--explain"]
    )
    assert dispatch(args, client)["params"]["explain"]


def test_rebuild_repairs_external_content_fts_and_refuses_unsafe_optional_paths(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    index, checkout = _index(batch_env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    with index.store.connection() as con:
        con.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES ('delete-all')")
    assert not index.search_lexical(checkout, "old")["results"]
    rebuilt = index.build(checkout, rebuild=True)
    assert rebuilt["state"] == "ready" and rebuilt["store_recreated"]
    assert index.search_lexical(checkout, "old")["results"]
    original = index.store.path
    target = tmp_path / "user-important-file"
    target.write_bytes(b"preserve unrelated file")
    original.unlink()
    original.symlink_to(target)
    assert index.build(checkout, rebuild=True)["state"] == "unavailable"
    assert target.read_bytes() == b"preserve unrelated file" and original.is_symlink()


def test_busy_optional_store_rebuild_does_not_remove_healthy_database(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    index, checkout = _index(batch_env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    before = index.store.path.stat().st_ino

    def busy() -> None:
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error

    monkeypatch.setattr(index.store, "initialize", busy)
    assert index.build(checkout, rebuild=True)["state"] == "unavailable"
    assert index.store.path.exists() and index.store.path.stat().st_ino == before


def test_interrupted_initial_build_cannot_publish_a_partial_event_only_baseline(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index, checkout = _index(batch_env, tmp_path)
    replace_generation = index.store.replace

    def crash(**kwargs: Any) -> Any:
        raise KeyboardInterrupt("simulated daemon exit before first generation")

    monkeypatch.setattr(index.store, "replace", crash)
    with pytest.raises(KeyboardInterrupt):
        index.build(checkout)
    monkeypatch.setattr(index.store, "replace", replace_generation)
    index.initialize()
    assert index.status()["state"] == "degraded"
    batch_env.publisher.publish("after-crash", "first", batch_env.files(), ("*",))
    assert index.process_pending() == 0
    assert index.search_lexical(checkout, "new")["state"] == "not_built"
    with index.store.connection() as con:
        assert (
            con.execute("SELECT count(*) FROM knowledge_generations").fetchone()[0] == 0
        )
        assert (
            con.execute("SELECT state FROM projection_jobs").fetchone()[0] == "pending"
        )
    assert index.build(checkout, rebuild=True)["state"] == "ready"
    assert index.search_lexical(checkout, "new")["results"]


def test_old_opaque_receipt_does_not_hide_newer_safe_git_baseline(
    batch_env: BatchEnvironment, tmp_path: Path, git_run: Callable[..., str]
) -> None:
    env = batch_env
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    env.store.record_workspace_read(
        session_id="first",
        relative_path="a.txt",
        identity=identify_path(env.repository / "a.txt"),
    )
    (env.repository / "a.txt").write_text("AWS_ACCESS_KEY_ID=" + "AKIA" + "Q" * 16)
    assert ExternalWorkspaceScanner(env.store).scan_session("first") == 1
    assert index.process_pending() == 1
    assert all(
        item["path"] != "a.txt"
        for item in index.search_lexical(checkout, "a.txt")["results"]
    )
    (env.repository / "a.txt").write_text("safecommittedafteropaquemarker\n")
    git_run(env.repository, "add", "a.txt")
    git_run(env.repository, "commit", "-m", "safe version after opaque source")
    assert index.build(checkout, rebuild=True)["state"] == "ready"
    result = index.search_lexical(checkout, "safecommittedafteropaquemarker")[
        "results"
    ][0]
    assert result["indexed_revision"].startswith("git:") and not result["stale"]
