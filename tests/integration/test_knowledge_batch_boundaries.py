"""A batched Git reader keeps the baseline's byte and privacy boundaries."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.knowledge import indexer
from llm_cli.knowledge.indexer import KnowledgeIndex


def _index(env: BatchEnvironment, tmp_path: Path) -> tuple[KnowledgeIndex, str]:
    workspace = env.store.get_workspace(env.workspace_id)
    assert workspace
    index = KnowledgeIndex(
        env.store,
        tmp_path / "knowledge.sqlite3",
        env.publisher.candidate_dir,
        enabled=True,
    )
    index.initialize()
    return index, workspace.checkout_id


def _malformed(kind: str, payload: bytes) -> bytes:
    header, separator, body = payload.partition(b"\n")
    assert separator
    parts = header.split()
    assert len(parts) == 3
    if kind == "wrong_oid":
        parts[0] = b"0" * len(parts[0])
    elif kind == "wrong_type":
        parts[1] = b"tree"
    elif kind == "wrong_size":
        parts[2] = str(int(parts[2]) + 1).encode()
    elif kind == "negative_size":
        parts[2] = b"-1"
    elif kind == "truncated_header":
        return parts[0] + b" blob"
    elif kind == "truncated_body":
        return header + b"\n" + body[: int(parts[2]) - 1]
    elif kind == "missing_separator":
        size = int(parts[2])
        return header + b"\n" + body[:size] + b"X" + body[size + 1 :]
    elif kind == "trailing_data":
        return payload + b"unexpected private source bytes"
    elif kind == "oversized_response":
        return payload + b"X" * (len(payload) + 1_024)
    else:
        raise AssertionError(kind)
    return b" ".join(parts) + b"\n" + body


@pytest.mark.parametrize(
    "malformation",
    [
        "wrong_oid",
        "wrong_type",
        "wrong_size",
        "negative_size",
        "truncated_header",
        "truncated_body",
        "missing_separator",
        "trailing_data",
        "oversized_response",
    ],
)
def test_malformed_batch_cannot_replace_a_healthy_generation(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformation: str,
) -> None:
    index, checkout = _index(batch_env, tmp_path)
    built = index.build(checkout)
    assert built["state"] == "ready", built
    before = index.store.source(checkout)
    assert before
    documents = index.store.documents(str(before["source_id"]))
    run_git = indexer.run_git
    batched_reads = 0

    def malformed_git(
        root: Path, arguments: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        nonlocal batched_reads
        result = run_git(root, arguments, **kwargs)
        if list(arguments) == ["cat-file", "--batch"]:
            batched_reads += 1
            assert isinstance(result.stdout, bytes)
            result.stdout = _malformed(malformation, result.stdout)
        return result

    monkeypatch.setattr(indexer, "run_git", malformed_git)
    result = index.build(checkout, rebuild=True)
    assert batched_reads == 1
    assert result["state"] == "unavailable", result
    assert "unexpected private source bytes" not in str(result)
    after = index.store.source(checkout)
    assert after and after["active_generation"] == before["active_generation"]
    assert index.store.documents(str(before["source_id"])) == documents


def test_total_byte_limit_is_checked_before_reading_any_blob(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index, checkout = _index(batch_env, tmp_path)
    monkeypatch.setattr(indexer, "MAX_BYTES", 10)
    run_git = indexer.run_git

    def bounded_git(
        root: Path, arguments: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        assert arguments[0] != "cat-file", "over-budget blobs must not be read"
        return run_git(root, arguments, **kwargs)

    monkeypatch.setattr(indexer, "run_git", bounded_git)
    result = index.build(checkout)
    assert result["state"] == "unavailable", result
    with index.store.connection() as con:
        assert (
            con.execute("SELECT count(*) FROM knowledge_documents").fetchone()[0] == 0
        )


def test_batched_read_skips_large_ignored_and_private_paths_and_screens_secrets(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = batch_env
    canary = "AKIA" + "Z" * 16
    files = {
        ".gitignore": "ignored.py\n",
        ".env": "PRIVATE_ENV_CANARY\n",
        "ignored.py": "IGNORED_FILE_CANARY\n",
        "fixtures/credentials_test.py": "key = '" + canary + "'\n",
        "node_modules/installed.js": "DEPENDENCY_FILE_CANARY\n",
        "oversized.py": "OVERSIZED_CANARY" * 10,
        "safe.py": "safe_batch_marker\n",
    }
    for path, content in files.items():
        target = env.repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git_run(env.repository, "add", "-f", *files)
    git_run(env.repository, "commit", "-m", "tracked baseline boundary fixtures")
    forbidden_oids = {
        git_run(env.repository, "rev-parse", "HEAD:" + path).encode()
        for path in (".env", "ignored.py", "node_modules/installed.js", "oversized.py")
    }
    index, checkout = _index(env, tmp_path)
    monkeypatch.setattr(indexer, "MAX_FILE", 100)
    run_git = indexer.run_git
    requests: list[bytes] = []

    def observed_git(
        root: Path, arguments: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        if list(arguments) == ["cat-file", "--batch"]:
            request = kwargs["input_data"]
            assert isinstance(request, bytes)
            requests.append(request)
        return run_git(root, arguments, **kwargs)

    monkeypatch.setattr(indexer, "run_git", observed_git)
    built = index.build(checkout)
    assert built["state"] == "ready", built
    assert len(requests) == 1
    assert not forbidden_oids.intersection(requests[0].splitlines())
    assert index.search_lexical(checkout, "safe_batch_marker")["results"]
    with index.store.connection() as con:
        dumped = "\n".join(con.iterdump())
    for forbidden in (
        canary,
        "PRIVATE_ENV_CANARY",
        "IGNORED_FILE_CANARY",
        "DEPENDENCY_FILE_CANARY",
        "OVERSIZED_CANARY",
    ):
        assert forbidden not in dumped


def test_batch_payload_parsing_observes_the_build_deadline(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index, _ = _index(batch_env, tmp_path)
    run_git = indexer.run_git
    expired = False

    def remaining() -> float:
        if expired:
            raise ValueError("lexical baseline exceeded its time budget")
        return 30

    def expires_after_read(
        root: Path, arguments: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        nonlocal expired
        assert kwargs["remaining"] is remaining
        result = run_git(root, arguments, **kwargs)
        if list(arguments) == ["cat-file", "--batch"]:
            expired = True
        return result

    monkeypatch.setattr(indexer, "run_git", expires_after_read)
    oid = git_run(batch_env.repository, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="time budget"):
        index._baseline(batch_env.repository, oid, remaining)
    assert expired
