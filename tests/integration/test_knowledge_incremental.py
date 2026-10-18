"""Changed-source projections preserve unaffected chunks and bounded pass work."""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_knowledge_index import _index
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.agent.source_policy import register_runtime_paths
from llm_cli.knowledge import store as store_module
from llm_cli.knowledge.chunker import MAX_TEXT_BYTES, Chunk, chunks
from llm_cli.workspace.batches import BatchFile
from llm_cli.workspace.identity import REGULAR_MODE, identify_path


def _publish(env: BatchEnvironment, batch: str, text: bytes) -> None:
    result = env.publisher.publish(
        batch,
        "first",
        (
            BatchFile(
                "a.txt", identify_path(env.repository / "a.txt"), text, REGULAR_MODE
            ),
        ),
        ("*",),
    )
    assert result.state == "published"


def _protect_unchanged(index: Any) -> None:
    with index.store.connection() as con:
        for table in ("knowledge_documents", "knowledge_chunks"):
            for operation, reference in (
                ("DELETE", "OLD"),
                ("INSERT", "NEW"),
                ("UPDATE", "OLD"),
            ):
                con.execute(
                    f"CREATE TRIGGER protect_{table}_{operation} BEFORE {operation} "
                    f"ON {table} WHEN {reference}.relative_path!='a.txt' BEGIN "
                    "SELECT RAISE(ABORT,'unchanged source was rewritten'); END"
                )


def test_incremental_replaces_only_changed_rows_and_cites_active_generation(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index, checkout = _index(batch_env, tmp_path)
    initial = index.build(checkout)
    assert initial["state"] == "ready", initial
    with index.store.connection() as con:
        before = tuple(
            con.execute(
                "SELECT rowid,* FROM knowledge_chunks WHERE relative_path='docs/b.txt'"
            ).fetchone()
        )
    _protect_unchanged(index)
    calls: list[str] = []
    original = store_module.chunks

    def record(source: str, path: str, identity: str, text: str) -> tuple[Chunk, ...]:
        calls.append(path)
        return original(source, path, identity, text)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("an ordinary pass must not load/rebuild the whole source")

    with monkeypatch.context() as patch:
        patch.setattr(index.store, "documents", forbidden)
        patch.setattr(index.store, "paths", forbidden)
        patch.setattr(index.store, "replace", forbidden)
        patch.setattr(store_module, "chunks", record)
        _publish(batch_env, "delta-one", b"newacceptedmarker\n")
        assert index.process_pending() == 1
    assert calls == ["a.txt"]
    with index.store.connection() as con:
        after = tuple(
            con.execute(
                "SELECT rowid,* FROM knowledge_chunks WHERE relative_path='docs/b.txt'"
            ).fetchone()
        )
        assert after == before
        con.execute(
            "INSERT INTO knowledge_fts(knowledge_fts,rank) VALUES ('integrity-check',1)"
        )
    unchanged = index.search_lexical(checkout, "docs/b.txt")
    assert unchanged["generation"] != initial["generation"]
    assert unchanged["results"][0]["generation"] == unchanged["generation"]
    assert unchanged["results"][0]["chunk_id"] == before[2]
    assert index.search_lexical(checkout, "newacceptedmarker")["results"]
    assert not any(
        item["path"] == "a.txt"
        for item in index.search_lexical(checkout, "old")["results"]
    )


def test_incremental_failure_rolls_back_generation_rows_and_cursor_then_retries(
    batch_env: BatchEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index, checkout = _index(batch_env, tmp_path)
    initial = index.build(checkout)
    assert initial["state"] == "ready"
    _publish(batch_env, "rollback-delta", b"afterrollbackmarker\n")
    original = index.store._insert_document

    def fail(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise ValueError("injected delta interruption")

    with monkeypatch.context() as patch:
        patch.setattr(index.store, "_insert_document", fail)
        assert index.process_pending() == 0
    with index.store.connection() as con:
        assert (
            con.execute("SELECT active_generation FROM knowledge_sources").fetchone()[0]
            == initial["generation"]
        )
        assert (
            con.execute("SELECT outbox_id FROM projection_cursors").fetchone()[0] == 0
        )
        assert (
            con.execute("SELECT count(*) FROM knowledge_documents").fetchone()[0] == 2
        )
        assert (
            con.execute(
                "SELECT text FROM knowledge_documents WHERE relative_path='a.txt'"
            ).fetchone()[0]
            == "old a\n"
        )
        con.execute(
            "INSERT INTO knowledge_fts(knowledge_fts,rank) VALUES ('integrity-check',1)"
        )
    assert index.process_pending() == 1
    assert index.search_lexical(checkout, "afterrollbackmarker")["results"]


@pytest.mark.parametrize(
    "policy",
    [
        "new_ignore",
        "global_excludes",
        "runtime",
        "restart",
        "local_edit",
        "local_config",
    ],
)
def test_policy_changes_remove_unchanged_private_documents_without_rechunking(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    policy: str,
    monkeypatch: pytest.MonkeyPatch,
    git_run: Callable[..., str],
) -> None:
    env = batch_env
    local_rules = tmp_path / "local-ignore"
    local_rules.write_text("")
    if policy == "local_edit":
        git_run(env.repository, "config", "core.excludesFile", str(local_rules))
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    if policy in {"local_edit", "local_config"}:
        local_rules.write_text("docs/b.txt\n")
        if policy == "local_config":
            git_run(env.repository, "config", "core.excludesFile", str(local_rules))
    elif policy == "runtime":
        register_runtime_paths(env.repository, ("docs",))
    elif policy == "global_excludes":
        rules = tmp_path / "global-ignore"
        rules.write_text("docs/b.txt\n")
        monkeypatch.setattr(
            "llm_cli.knowledge.indexer._user_excludes_file", lambda *args: str(rules)
        )
        monkeypatch.setattr(
            "llm_cli.agent.source_policy._user_excludes_file", lambda *args: str(rules)
        )
    else:
        (env.repository / "docs/.gitignore").write_text("b.txt\n")
        if policy == "restart":
            index, _ = _index(env, tmp_path)
    _publish(env, "policy-" + policy, b"policytrigger\n")
    assert index.process_pending() == 1
    with index.store.connection() as con:
        assert (
            con.execute(
                "SELECT count(*) FROM knowledge_documents "
                "WHERE relative_path='docs/b.txt'"
            ).fetchone()[0]
            == 0
        )
        con.execute(
            "INSERT INTO knowledge_fts(knowledge_fts,rank) VALUES ('integrity-check',1)"
        )
    assert all(
        item["path"] != "docs/b.txt"
        for item in index.search_lexical(checkout, "docs/b.txt")["results"]
    )


def test_one_publication_work_stays_bounded_at_one_and_ten_thousand_files(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timing plus structural guards catch a hidden full-source pass reliably."""
    env = batch_env
    text = "".join(f"def function_{line}(): return {line}\n" for line in range(40))
    source = env.repository / "src"
    source.mkdir()
    measurements: dict[int, float] = {}
    for count in (1_000, 10_000):
        for number in range(0 if count == 1_000 else 1_000, count):
            (source / f"file_{number}.py").write_text(text)
        git_run(env.repository, "add", "src")
        git_run(env.repository, "commit", "-m", f"scale {count}")
        index, checkout = _index(env, tmp_path)
        baseline_started = time.perf_counter()
        built = index.build(checkout)
        baseline_elapsed = time.perf_counter() - baseline_started
        assert built["state"] == "ready", built
        assert baseline_elapsed < 30, (count, baseline_elapsed)
        assert built["documents"] == count + 2
        with index.store.connection() as con:
            assert (
                con.execute(
                    "SELECT count(*) FROM knowledge_graph_nodes WHERE kind='function'"
                ).fetchone()[0]
                == count * 40
            )
        print(
            f"dense graph baseline: {count} files, {count * 40} functions "
            f"in {baseline_elapsed:.3f}s"
        )
        elapsed: list[float] = []

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("warmed publication scanned all source documents or paths")

        with monkeypatch.context() as patch:
            patch.setattr(index.store, "documents", forbidden)
            patch.setattr(index.store, "paths", forbidden)
            patch.setattr(index.store, "replace", forbidden)
            for repeat in range(5):
                _publish(
                    env,
                    f"scale-{count}-{repeat}",
                    f"scale{count}marker{repeat}\n".encode(),
                )
                started = time.perf_counter()
                assert index.process_pending() == 1
                elapsed.append(time.perf_counter() - started)
        measurements[count] = statistics.median(elapsed)
        assert max(elapsed) < 5, elapsed
        assert index.search_lexical(checkout, f"scale{count}marker4")["results"]
    # Allow scheduler noise and differing filesystems while excluding the old
    # near-tenfold full re-chunk/reinsert cost. Structural guards are stricter.
    assert measurements[10_000] < measurements[1_000] * 3 + 0.1, measurements
    print("incremental publication seconds:", measurements)


def _reference_chunks(text: str) -> list[tuple[int, int, str]]:
    """Original byte/code-point packing is the compatibility oracle."""
    result: list[tuple[int, int, str]] = []
    lines = text.splitlines(keepends=True)
    start = 0
    while start < len(lines):
        end = min(len(lines), start + 60)
        piece, size, first, last = "", 0, start + 1, start + 1
        for offset, line in enumerate(lines[start:end], start + 1):
            for character in line:
                encoded = len(character.encode())
                if size + encoded > MAX_TEXT_BYTES:
                    result.append((first, last, piece))
                    piece, size, first = "", 0, offset
                if not piece:
                    first = offset
                piece += character
                size += encoded
                last = offset
        if piece:
            result.append((first, last, piece))
        if end == len(lines):
            break
        start = max(start + 1, end - 10)
    return result


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain\n" * 125,
        "😀" * 8_300 + "\r\n" + "é" * 5_000,
        "a" * 8_191 + "😀" + "\n" + "b" * 8_192 + "\n",
        "\r\n\n\u2028\u0085" * 70,
        "\n".join(str(i) for i in range(111)),
    ],
)
def test_line_chunker_preserves_codepoint_packing_and_line_windows(text: str) -> None:
    actual = chunks("source", "file.py", "identity", text)
    assert [
        (chunk.start_line, chunk.end_line, chunk.text) for chunk in actual
    ] == _reference_chunks(text)
    assert all(len(chunk.text.encode()) <= MAX_TEXT_BYTES for chunk in actual)
    assert actual == chunks("source", "file.py", "identity", text)


@pytest.mark.parametrize("configuration", ["common", "worktree"])
def test_linked_worktree_policy_changes_revalidate_unchanged_documents(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Callable[..., str],
    configuration: str,
) -> None:
    from dataclasses import replace

    env = batch_env
    linked = tmp_path / "linked"
    git_run(env.repository, "worktree", "add", "--detach", str(linked), "HEAD")
    repository = env.store.list_repositories()[0]
    checkout = env.store.ensure_checkout(
        repository=repository,
        canonical_path=str(linked),
        git_common_dir=str(env.repository / ".git"),
    )
    workspace = env.store.ensure_shared_workspace(checkout)
    env.store.open_session(
        session_id="linked",
        checkout_id=checkout.checkout_id,
        resume_token_hash="0" * 64,
        provider="fake",
        model="fake",
        workspace_id=workspace.workspace_id,
    )
    with env.store.connection() as con:
        con.execute("UPDATE sessions SET state='active' WHERE session_id='linked'")
    index, checkout_id = _index(
        replace(env, repository=linked, workspace_id=workspace.workspace_id), tmp_path
    )
    assert index.build(checkout_id)["state"] == "ready"
    rules = tmp_path / "linked-excludes"
    rules.write_text("docs/b.txt\n")
    if configuration == "worktree":
        git_run(linked, "config", "extensions.worktreeConfig", "true")
        git_run(linked, "config", "--worktree", "core.excludesFile", str(rules))
    else:
        git_run(linked, "config", "core.excludesFile", str(rules))
    result = env.publisher.publish(
        "linked-policy-" + configuration,
        "linked",
        (
            BatchFile(
                "a.txt",
                identify_path(linked / "a.txt"),
                b"linkedmarker\n",
                REGULAR_MODE,
            ),
        ),
        ("*",),
    )
    assert result.state == "published"
    assert index.process_pending() == 1
    with index.store.connection() as con:
        assert (
            con.execute(
                "SELECT count(*) FROM knowledge_documents "
                "WHERE relative_path='docs/b.txt'"
            ).fetchone()[0]
            == 0
        )
    assert index.search_lexical(checkout_id, "linkedmarker")["results"]


def test_included_ignorecase_change_revalidates_unchanged_documents(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Callable[..., str],
) -> None:
    env = batch_env
    included = tmp_path / "included-git-config"
    included.write_text("[core]\nignoreCase = false\n")
    git_run(env.repository, "config", "--add", "include.path", str(included))
    (env.repository / ".gitignore").write_text("DOCS/B.TXT\n")
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    with index.store.connection() as con:
        assert (
            con.execute(
                "SELECT count(*) FROM knowledge_documents "
                "WHERE relative_path='docs/b.txt'"
            ).fetchone()[0]
            == 1
        )
    included.write_text("[core]\nignoreCase = true\n")
    _publish(env, "included-policy", b"includedpolicymarker\n")
    assert index.process_pending() == 1
    with index.store.connection() as con:
        assert (
            con.execute(
                "SELECT count(*) FROM knowledge_documents "
                "WHERE relative_path='docs/b.txt'"
            ).fetchone()[0]
            == 0
        )
    assert index.search_lexical(checkout, "includedpolicymarker")["results"]
