"""Real optional stores retain privacy and leave checkout authority untouched."""

from __future__ import annotations

import functools
import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from test_knowledge_graph import _prepare
from test_knowledge_index import _index
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.knowledge.retrieval import MAX_GRAPH_EDGES, MAX_GRAPH_NODES, project_search
from llm_cli.workspace.context import SharedCoordinationContext


def test_real_lexical_graph_evidence_and_coordination_invariance(
    batch_env: BatchEnvironment, tmp_path: Path, git_run: Callable[..., str]
) -> None:
    env = batch_env
    _prepare(env, git_run)
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    context = SharedCoordinationContext(env.store, "first", ("*",), scan_external=False)
    before = context(None)
    with env.store.connection() as con:
        dump = "\n".join(con.iterdump())
    broker = SharedToolBroker(
        env.repository,
        ("*",),
        project_search=functools.partial(
            index.project_search, checkout, env.repository
        ),
    )
    result = broker.invoke("search_project", {"query": "src/pkg/workspace/batches.py"})
    assert not result.is_error
    assert '"stale": false' in result.content
    data = index.project_search(
        checkout, env.repository, "src/pkg/workspace/batches.py"
    )
    assert data["results"] and data["graph"]["edges"]
    assert len(data["graph"]["edges"]) <= MAX_GRAPH_EDGES
    assert len(data["graph"]["nodes"]) <= MAX_GRAPH_NODES
    assert any(edge["type"] == "tests" for edge in data["graph"]["edges"])
    assert before == context(None)
    with env.store.connection() as con:
        assert dump == "\n".join(con.iterdump())
    # Discovery does not acquire a full-file observation for a write/delete.
    assert broker.usage.writes == 0 and not broker._observations
    assert broker.invoke(
        "write_file", {"path": "src/pkg/workspace/batches.py", "content": "replacement"}
    ).is_error


def test_private_task_root_staleness_and_privacy_are_revalidated(
    batch_env: BatchEnvironment, tmp_path: Path, git_run: Callable[..., str]
) -> None:
    env = batch_env
    _prepare(env, git_run)
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    private = tmp_path / "private"
    shutil.copytree(env.repository, private)
    target = private / "src/pkg/workspace/batches.py"
    target.write_text("class Changed: pass\n")
    data = index.project_search(checkout, private, "src/pkg/workspace/batches.py")
    item = next(
        row for row in data["results"] if row["path"] == "src/pkg/workspace/batches.py"
    )
    assert item["stale"] and item["label"] == "stale indexed version"
    assert any(edge["stale"] for edge in data["graph"]["edges"])
    # The primary root is still clean: validating there would leak private data.
    target.write_text("TOKEN = 'sk-" + "a" * 40 + "'\n")
    data = index.project_search(checkout, private, "src/pkg/workspace/batches.py")
    assert not any(
        row["path"] == "src/pkg/workspace/batches.py" for row in data["results"]
    )
    assert not any(
        edge["path"] == "src/pkg/workspace/batches.py"
        for edge in data["graph"].get("edges", [])
    )
    assert "sk-" not in json.dumps(data)
    # A task-specific ignore rule also hides previously indexed source.
    (private / ".gitignore").write_text("src/pkg/consumer.py\n")
    data = index.project_search(checkout, private, "src/pkg/consumer.py")
    assert not any(row["path"] == "src/pkg/consumer.py" for row in data["results"])


@pytest.mark.parametrize("kind", ["missing", "broken", "unbuilt", "dropped"])
def test_missing_broken_and_unbuilt_optional_index_degrades_without_mutation(
    batch_env: BatchEnvironment, tmp_path: Path, kind: str
) -> None:
    index, checkout = _index(batch_env, tmp_path)
    if kind in {"broken", "missing", "dropped"}:
        assert index.build(checkout)["state"] == "ready"
    if kind == "dropped":
        assert index.drop(checkout)["state"] == "dropped"
    elif kind == "missing":
        index.store.path.unlink()
    elif kind == "broken":
        index.store.path.write_bytes(b"not a sqlite database")
    before = index.store.path.read_bytes() if index.store.path.exists() else None
    data = index.project_search(checkout, batch_env.repository, "old")
    assert data["state"] != "ready" and data["notice"]
    assert not data["results"]
    assert (
        index.store.path.read_bytes() if index.store.path.exists() else None
    ) == before
    assert index.error is None  # Advisory lookup never marks a worker unhealthy.


def test_retrieval_never_initializes_missing_parent_or_database(tmp_path: Path) -> None:
    path = tmp_path / "absent" / "knowledge.sqlite3"
    result = project_search(path, "checkout", tmp_path, "code")
    assert result["notice"] and not path.parent.exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_daemon_setting_wires_scripted_task_surface(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., object],
    enabled: bool,
) -> None:
    import asyncio
    from dataclasses import replace

    from test_shared_agent_execution import (
        ScriptedProvider,
        _assert_completed,
        _call,
        _finish,
        _open_session,
        _register,
        _request,
        _start,
        _tools,
    )

    from llm_cli.daemon.service import DaemonService

    async def scenario() -> None:
        initial = service_factory(tmp_path)
        service = DaemonService(
            initial.paths,
            replace(initial.settings, knowledge_index="lexical" if enabled else "off"),
            asyncio.Event(),
            boot_id="optional-retrieval",
            authority_root=tmp_path / "authority" / "v1",
        )
        repository = repository_factory(
            tmp_path, {"source.py": "def discover_me(): pass\n"}
        )
        provider = ScriptedProvider(
            "retrieval-script",
            [
                _tools(_call("search_project", query="discover_me")),
                _finish("Source discovered."),
            ],
        )
        _register(service, provider)
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repository)}))
            credentials = await _open_session(service, repository, provider)
            if enabled:
                built = await service.handle(
                    _request("index.build", {"path": str(repository)})
                )
                assert built["state"] == "ready"
            background = await _start(
                service, repository, credentials, "retrieve", ("*",)
            )
            await asyncio.wait_for(background, timeout=10)
            _assert_completed(service, "retrieve")
            assert ("search_project" in provider.offered_tools) is enabled
            result = provider.results[0][0]
            assert result.is_error is not enabled
            if enabled:
                assert "discover_me" in result.content
                assert "coordination" in str(provider.history[0]).lower()
                if "Coordination update" in result.content:
                    assert result.content.index(
                        "Coordination update"
                    ) < result.content.index("[optional_retrieval]")
            else:
                assert not service.paths.knowledge_db.exists()
        finally:
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["write_file", "delete_file", "rename_file"])
def test_pending_overlay_and_helper_label_indexed_evidence_stale_without_new_authority(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Callable[..., str],
    operation: str,
) -> None:
    import copy

    from llm_cli.agent.limits import ExecutionLimits

    env = batch_env
    _prepare(env, git_run)
    index, checkout = _index(env, tmp_path)
    assert index.build(checkout)["state"] == "ready"
    path = "src/pkg/workspace/batches.py"
    broker = SharedToolBroker(
        env.repository,
        ("*",),
        project_search=functools.partial(
            index.project_search, checkout, env.repository
        ),
    )
    assert not broker.invoke("read_file", {"path": path}).is_error
    if operation == "write_file":
        arguments = {"path": path, "content": "class PendingOnlyMarker: pass\n"}
    elif operation == "delete_file":
        arguments = {"path": path}
    else:
        arguments = {"source": path, "destination": "src/pkg/renamed.py"}
    staged = broker.invoke(operation, arguments)
    assert not staged.is_error, staged
    context = SharedCoordinationContext(env.store, "first", ("*",), scan_external=False)
    before = context(None)
    with env.store.connection() as con:
        dump = "\n".join(con.iterdump())
    original = index.project_search(checkout, env.repository, path)
    assert any(not row["stale"] and row["path"] == path for row in original["results"])
    for surface in (broker, broker.read_only_view(ExecutionLimits())):
        observations = copy.deepcopy(surface._observations)
        writes = surface.usage.writes
        outcome = surface.invoke("search_project", {"query": path})
        assert not outcome.is_error
        assert "PendingOnlyMarker" not in outcome.content
        data = json.loads(outcome.content.split("\n", 2)[2].rsplit("\n", 1)[0])
        hit = next(row for row in data["results"] if row["path"] == path)
        assert hit["stale"] and "pending task edit" in hit["label"]
        matching = [
            edge
            for edge in data["graph"]["edges"]
            if edge["path"] == path or edge["target"] == "file:" + path
        ]
        assert matching and all(edge["stale"] for edge in matching)
        assert surface._observations == observations and surface.usage.writes == writes
    assert original == index.project_search(checkout, env.repository, path)
    assert before == context(None)
    with env.store.connection() as con:
        assert dump == "\n".join(con.iterdump())
