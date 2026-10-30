"""Offline lexical/semantic comparison; no chat calls or product configuration.

The optional local ONNX dependencies are imported only by the explicit run.
Unit scoring/fusion tests need no model, network, or optional dependencies.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import platform
import random
import re
import statistics
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any


def recall_at_k(expected: Sequence[str], ranked: Sequence[str], k: int) -> float:
    if type(k) is not int or k < 1 or not expected:
        raise ValueError("recall needs expected files and a positive cutoff")
    relevant = set(expected)
    unique = list(dict.fromkeys(ranked))
    return len(relevant.intersection(unique[:k])) / len(relevant)


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]], *, constant: int = 60
) -> list[str]:
    if type(constant) is not int or constant < 1:
        raise ValueError("rank fusion needs a positive constant")
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, path in enumerate(dict.fromkeys(ranking), 1):
            scores[path] = scores.get(path, 0.0) + 1.0 / (constant + rank)
    return sorted(scores, key=lambda path: (-scores[path], path))


def gate_decision(
    rows: Sequence[dict[str, Any]], gate: dict[str, Any]
) -> dict[str, Any]:
    """Apply the predeclared corpus-specific gate without dropping failed queries."""
    repositories = sorted({row["repository"] for row in rows})
    grouped = {
        repository: [row for row in rows if row["repository"] == repository]
        for repository in repositories
    }
    per_repository = {
        repository: {
            "queries": len(subset),
            "delta_at_10": statistics.mean(
                row["recall"]["hybrid"]["10"] - row["recall"]["lexical"]["10"]
                for row in subset
            ),
            "delta_at_5": statistics.mean(
                row["recall"]["hybrid"]["5"] - row["recall"]["lexical"]["5"]
                for row in subset
            ),
        }
        for repository, subset in grouped.items()
    }
    deltas = {
        repository: [
            row["recall"]["hybrid"]["10"] - row["recall"]["lexical"]["10"]
            for row in subset
        ]
        for repository, subset in grouped.items()
    }
    randomizer = random.Random(gate["bootstrap_seed"])
    bootstrap = (
        sorted(
            statistics.mean(
                statistics.mean(randomizer.choices(values, k=len(values)))
                for values in deltas.values()
            )
            for _ in range(gate["bootstrap_samples"])
        )
        if deltas
        else []
    )
    lower = bootstrap[int(len(bootstrap) * 0.025)] if bootstrap else None
    upper = (
        bootstrap[min(len(bootstrap) - 1, int(len(bootstrap) * 0.975))]
        if bootstrap
        else None
    )
    macro_delta = (
        statistics.mean(record["delta_at_10"] for record in per_repository.values())
        if per_repository
        else None
    )
    opened = bool(
        len(repositories) >= 2
        and macro_delta is not None
        and macro_delta >= gate["minimum_macro_delta_at_10"]
        and lower is not None
        and lower > 0
        and all(
            record["queries"] >= gate["minimum_queries_per_repository"]
            and record["delta_at_10"] >= gate["minimum_repository_delta_at_10"]
            and record["delta_at_5"] >= 0
            for record in per_repository.values()
        )
    )
    return {
        "decision": "go" if opened else "no-go",
        "macro_delta_at_10": macro_delta,
        "paired_stratified_bootstrap_95_percent": [lower, upper],
        "repositories": per_repository,
        "thresholds": gate,
    }


def _git(root: Path, *arguments: str, data: bytes | None = None) -> bytes:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(root), *arguments],
        input=data,
        capture_output=True,
        env=environment,
        check=True,
        timeout=120,
    ).stdout


def corpus(
    root: Path, revision: str, prefix: str
) -> tuple[dict[str, tuple[str, bytes]], dict[str, Any]]:
    """Read only bounded, pinned regular Python blobs through one batch process."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise ValueError("the corpus must name a full immutable Git revision")
    from llm_cli.agent.source_policy import contains_secret_material

    entries = []
    byte_count = 0
    excluded = 0
    for raw in _git(root, "ls-tree", "-r", "-l", "-z", revision).split(b"\0"):
        if not raw:
            continue
        head, path_bytes = raw.split(b"\t", 1)
        mode, kind, oid, size = head.decode("ascii").split()
        path = path_bytes.decode("utf-8")
        if (
            not path.startswith(prefix)
            or not path.endswith(".py")
            or kind != "blob"
            or mode not in {"100644", "100755"}
        ):
            continue
        if (
            Path(path).is_absolute()
            or ".." in Path(path).parts
            or int(size) > 1024 * 1024
        ):
            excluded += 1
            continue
        entries.append((path, mode, oid, int(size)))
        byte_count += int(size)
    if len(entries) > 20000 or byte_count > 64 * 1024 * 1024:
        raise ValueError("experiment corpus exceeds the unchanged lexical bounds")
    output = _git(
        root,
        "cat-file",
        "--batch",
        data="".join(oid + "\n" for _, _, oid, _ in entries).encode(),
    )
    cursor = 0
    documents = {}
    for path, mode, oid, size in entries:
        end = output.index(b"\n", cursor)
        header = output[cursor:end].decode().split()
        if header != [oid, "blob", str(size)]:
            raise ValueError("Git batch result does not match its pinned manifest")
        body = output[end + 1 : end + 1 + size]
        cursor = end + size + 2
        if output[cursor - 1 : cursor] != b"\n" or len(body) != size:
            raise ValueError("Git batch body is incomplete")
        try:
            body.decode("utf-8")
        except UnicodeError:
            excluded += 1
            continue
        if contains_secret_material(body):
            excluded += 1
            continue
        documents[path] = (mode, body)
    if cursor != len(output):
        raise ValueError("Git batch returned bytes outside the pinned blob manifest")
    manifest = hashlib.sha256()
    for path, (mode, body) in sorted(documents.items()):
        manifest.update(path.encode() + b"\0" + mode.encode() + b"\0")
        manifest.update(hashlib.sha256(body).digest())
    return documents, {
        "manifest_sha256": manifest.hexdigest(),
        "files": len(documents),
        "bytes": sum(len(body) for _, body in documents.values()),
        "lines": sum(len(body.splitlines()) for _, body in documents.values()),
        "excluded": excluded,
    }


def _model_files(config: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Verify the pinned public file identities before loading executable graphs."""
    result = {}
    for name, expected in config["model_files"].items():
        if name not in {"tokenizer.json", "onnx/model.onnx"}:
            raise ValueError("unexpected local model file")
        path = directory / name
        if path.stat().st_size != expected["bytes"]:
            raise ValueError("local model file has an unexpected size")
        digest = hashlib.sha256()
        git_digest = hashlib.sha1()  # Git object identity, not authentication.
        git_digest.update(f"blob {expected['bytes']}\0".encode())
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
                git_digest.update(block)
        identity = {"sha256": digest.hexdigest(), "bytes": expected["bytes"]}
        if "sha256" in expected and identity["sha256"] != expected["sha256"]:
            raise ValueError("local model content differs from its pinned revision")
        if "git_blob" in expected and git_digest.hexdigest() != expected["git_blob"]:
            raise ValueError("local tokenizer differs from its pinned Git blob")
        result[name] = identity
    if set(result) != {"tokenizer.json", "onnx/model.onnx"}:
        raise ValueError("both pinned local model files are required")
    return result


def _source_symbols(node: ast.AST, prefix: str = "") -> Iterator[tuple[str, Any]]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = prefix + child.name
            yield name, child
            yield from _source_symbols(child, name + ".")


def _ground_truth(
    query: dict[str, Any], source: dict[str, tuple[str, bytes]]
) -> list[dict[str, Any]]:
    """Validate source anchors without importing or executing either corpus."""
    evidence = []
    for anchor in query["evidence"]:
        path, separator, symbol = anchor.partition("::")
        if not separator or path not in query["expected"]:
            raise ValueError("ground truth must point into an expected answer file")
        tree = ast.parse(source[path][1].decode("utf-8"))
        found = [node for name, node in _source_symbols(tree) if name == symbol]
        if len(found) != 1:
            raise ValueError("ground-truth source symbol is missing or ambiguous")
        evidence.append(
            {
                "path": path,
                "symbol": symbol,
                "start_line": found[0].lineno,
                "end_line": found[0].end_lineno,
                "file_sha256": hashlib.sha256(source[path][1]).hexdigest(),
            }
        )
    if {item["path"] for item in evidence} != set(query["expected"]):
        raise ValueError("every expected answer file needs an inspected source anchor")
    return evidence


class LocalEncoder:
    def __init__(self, model: Path, maximum: int) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        self.tokenizer = Tokenizer.from_file(str(model / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=maximum)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model / "onnx/model.onnx"),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.inputs = {item.name for item in self.session.get_inputs()}
        self.truncated = 0

    def encode(self, texts: Sequence[str]):
        np = self.np
        batches = []
        for offset in range(0, len(texts), 32):
            encoded = self.tokenizer.encode_batch(list(texts[offset : offset + 32]))
            self.truncated += sum(bool(item.overflowing) for item in encoded)
            values = {
                "input_ids": np.asarray([item.ids for item in encoded], dtype=np.int64),
                "attention_mask": np.asarray(
                    [item.attention_mask for item in encoded], dtype=np.int64
                ),
                "token_type_ids": np.asarray(
                    [item.type_ids for item in encoded], dtype=np.int64
                ),
            }
            vectors = self.session.run(
                None,
                {name: value for name, value in values.items() if name in self.inputs},
            )[0]
            if vectors.shape != (*values["input_ids"].shape, 384):
                raise ValueError(
                    "local model output is not token embeddings of dimension384"
                )
            mask = values["attention_mask"][:, :, None].astype(np.float32)
            pooled = (vectors * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1e-9)
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            if not np.all(np.isfinite(pooled)) or np.any(norms <= 0):
                raise ValueError("embedding output is not finite and nonzero")
            batches.append((pooled / norms).astype(np.float32))
        return (
            np.concatenate(batches) if batches else np.empty((0, 384), dtype=np.float32)
        )


def evaluate(
    config: dict[str, Any], roots: dict[str, Path], model: Path
) -> dict[str, Any]:
    from llm_cli.knowledge.chunker import chunks
    from llm_cli.knowledge.search import search
    from llm_cli.knowledge.store import Document, KnowledgeStore
    from llm_cli.workspace.identity import ObjectKind, content_identity

    pool = config["file_result_limit"]
    chunk_pool = config["chunk_candidate_pool"]
    if type(pool) is not int or not 10 <= pool <= 20:
        raise ValueError(
            "file result limit must stay inside the product's twenty-file limit"
        )
    if chunk_pool != pool * 4:
        raise ValueError("chunk pool must mirror product lexical search oversampling")
    if set(roots) != set(config["repositories"]):
        raise ValueError("supply exactly the configured repositories")
    rows: list[dict[str, Any]] = []
    measurements: dict[str, Any] = {}
    prepared = {}
    if len({query["id"] for query in config["queries"]}) != len(config["queries"]):
        raise ValueError("query IDs must be unique")
    for label, specification in config["repositories"].items():
        queries = [query for query in config["queries"] if query["repository"] == label]
        if len(queries) < config["gate"]["minimum_queries_per_repository"]:
            raise ValueError("the predeclared per-repository query count is incomplete")
        started = time.monotonic()
        source, census = corpus(
            roots[label], specification["revision"], specification["prefix"]
        )
        for query in queries:
            if not query["expected"] or not set(query["expected"]) <= source.keys():
                raise ValueError(
                    "an expected file is absent from the eligible pinned corpus"
                )
        evidence = {query["id"]: _ground_truth(query, source) for query in queries}
        source_seconds = time.monotonic() - started
        prepared[label] = (source, census, evidence, queries, source_seconds)
    if any(
        prepared["django"][1][unit] <= prepared["loupe"][1][unit]
        for unit in ("files", "bytes", "lines")
    ):
        raise ValueError(
            "the second public corpus must be larger by files, bytes and lines"
        )
    started = time.monotonic()
    model_files = _model_files(config, model)
    encoder = LocalEncoder(model, config["max_wordpieces"])
    model_load_seconds = time.monotonic() - started
    for label, specification in config["repositories"].items():
        source, census, evidence, queries, source_seconds = prepared.pop(label)
        source_id = hashlib.sha256(
            (label + specification["revision"]).encode()
        ).hexdigest()
        checkout = "offline-" + label
        with tempfile.TemporaryDirectory(prefix="loupe-semantic-eval-") as temporary:
            directory = Path(temporary).resolve()
            snapshot = directory / "source"
            snapshot.mkdir()
            _git(
                directory, "init", "--quiet", "--initial-branch", "main", str(snapshot)
            )
            documents = {}
            text_chunks: list[str] = []
            chunk_paths: list[str] = []
            for path, (mode, body) in sorted(source.items()):
                destination = snapshot / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(body)
                destination.chmod(0o755 if mode == "100755" else 0o644)
                identity = content_identity(ObjectKind.REGULAR, mode, body)
                text = body.decode("utf-8")
                documents[path] = Document(
                    path, identity, mode, "git:" + specification["revision"], text
                )
                for part in chunks(source_id, path, identity, text):
                    text_chunks.append(part.text)
                    chunk_paths.append(path)
            store = KnowledgeStore(directory / "knowledge.sqlite3")
            store.initialize()
            started = time.monotonic()
            store.replace(
                source_id=source_id,
                checkout_id=checkout,
                root=snapshot,
                base_oid=specification["revision"],
                sequence=0,
                outbox_id=0,
                documents=documents,
                job_id="offline-build",
            )
            lexical_seconds = time.monotonic() - started
            started, cpu = time.monotonic(), time.process_time()
            before = encoder.truncated
            vectors = encoder.encode(text_chunks)
            truncated_chunks = encoder.truncated - before
            embedding_seconds = time.monotonic() - started
            embedding_cpu_seconds = time.process_time() - cpu
            started, cpu = time.monotonic(), time.process_time()
            before = encoder.truncated
            query_vectors = encoder.encode([query["question"] for query in queries])
            query_embedding_seconds = time.monotonic() - started
            query_embedding_cpu_seconds = time.process_time() - cpu
            truncated_queries = encoder.truncated - before
            lexical_query_seconds = 0.0
            exact_query_seconds = 0.0
            for query, query_vector in zip(queries, query_vectors, strict=True):
                started = time.monotonic()
                lexical_result = search(store, checkout, query["question"], limit=pool)
                if lexical_result.get("state") != "ready":
                    raise ValueError("the real lexical index is not ready")
                lexical = list(
                    dict.fromkeys(item["path"] for item in lexical_result["results"])
                )
                lexical_query_seconds += time.monotonic() - started
                started = time.monotonic()
                scores = vectors @ query_vector
                indices = encoder.np.argsort(-scores, kind="stable")[:chunk_pool]
                semantic = list(
                    dict.fromkeys(chunk_paths[int(index)] for index in indices)
                )[:pool]
                exact_query_seconds += time.monotonic() - started
                hybrid = reciprocal_rank_fusion(
                    [lexical, semantic], constant=config["rrf_constant"]
                )
                ranked = {"lexical": lexical, "semantic": semantic, "hybrid": hybrid}
                rows.append(
                    {
                        "id": query["id"],
                        "repository": label,
                        "question": query["question"],
                        "expected": query["expected"],
                        "evidence": evidence[query["id"]],
                        "rankings": {arm: paths[:10] for arm, paths in ranked.items()},
                        "recall": {
                            arm: {
                                str(k): recall_at_k(query["expected"], paths, k)
                                for k in (5, 10)
                            }
                            for arm, paths in ranked.items()
                        },
                    }
                )
            measurements[label] = {
                "repository": specification["url"],
                "revision": specification["revision"],
                "prefix": specification["prefix"],
                **census,
                "chunks": len(text_chunks),
                "queries": len(queries),
                "source_seconds": source_seconds,
                "lexical_build_seconds": lexical_seconds,
                "embedding_seconds": embedding_seconds,
                "embedding_cpu_seconds": embedding_cpu_seconds,
                "truncated_chunks": truncated_chunks,
                "query_embedding_seconds": query_embedding_seconds,
                "query_embedding_cpu_seconds": query_embedding_cpu_seconds,
                "truncated_queries": truncated_queries,
                "lexical_query_seconds": lexical_query_seconds,
                "exact_query_seconds": exact_query_seconds,
                "vector_bytes": int(vectors.nbytes),
                "recall": {
                    arm: {
                        str(k): statistics.mean(
                            row["recall"][arm][str(k)]
                            for row in rows
                            if row["repository"] == label
                        )
                        for k in (5, 10)
                    }
                    for arm in ("lexical", "semantic", "hybrid")
                },
            }
    return {
        "version": config["version"],
        "model": {
            "id": config["model_id"],
            "revision": config["model_revision"],
            "files": model_files,
            "dimensions": 384,
            "normalization": "L2",
            "pooling": "attention-masked mean",
            "task_type": "generic sentence similarity",
            "max_wordpieces": config["max_wordpieces"],
            "load_seconds": model_load_seconds,
            "provider": "local ONNX CPU",
        },
        "hosted_embedding_requests": 0,
        "model_api_tokens": 0,
        "runtime": {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
            "packages": {
                name: importlib.metadata.version(name)
                for name in ("numpy", "onnxruntime", "tokenizers")
            },
        },
        "chunk_candidate_pool_per_arm": chunk_pool,
        "file_result_limit_per_arm": pool,
        "rrf_constant": config["rrf_constant"],
        "repositories": measurements,
        "queries": rows,
        "f4_gate": gate_decision(rows, config["gate"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--queries",
        type=Path,
        default=Path(__file__).with_name("semantic_retrieval.toml"),
    )
    parser.add_argument("--repo", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    arguments = parser.parse_args()
    if arguments.out.exists():
        parser.error("refusing to replace an earlier measurement; choose a new output")
    roots = {}
    for value in arguments.repo:
        name, separator, path = value.partition("=")
        if not separator or name in roots:
            parser.error("each --repo must uniquely name NAME=PATH")
        roots[name] = Path(path).resolve(strict=True)
    query_bytes = arguments.queries.read_bytes()
    query_hash = hashlib.sha256(query_bytes).hexdigest()
    config = tomllib.loads(query_bytes.decode("utf-8"))
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    document = evaluate(config, roots, arguments.model_dir.resolve(strict=True))
    document["query_file_sha256"] = query_hash
    document["script_sha256"] = script_hash
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=arguments.out.parent,
        prefix="." + arguments.out.name + ".",
        suffix=".pending",
        delete=False,
    ) as handle:
        pending = Path(handle.name)
        try:
            handle.write(json.dumps(document, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            pending.unlink(missing_ok=True)
            raise
    try:
        # Another run can finish during evaluation. Never replace its evidence.
        os.link(pending, arguments.out)
    finally:
        pending.unlink(missing_ok=True)
    print(
        json.dumps(
            {"repositories": document["repositories"], "f4_gate": document["f4_gate"]},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
