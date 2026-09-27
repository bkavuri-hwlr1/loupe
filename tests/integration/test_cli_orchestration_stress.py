"""Seeded contention across six actual terminals and one real daemon.

Barriers force every worker to stage before publication. Seeds vary launch and
release orders; assertions inspect checkout bytes, retained proposals, claims,
revision counts and Git invariants instead of depending on a particular winner.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from test_cli_process_orchestration import _CONTEXT, _Cluster
from test_cli_process_orchestration import cluster as cluster


def _git_state(cluster: _Cluster, git_run: Callable[..., str]) -> tuple[str, str]:
    return (
        git_run(cluster.repository, "show-ref"),
        git_run(cluster.repository, "write-tree"),
    )


def _assert_quiescent(cluster: _Cluster, publications: int) -> None:
    claims = cluster.rpc("claim.list", path=str(cluster.repository))
    assert all(claim["state"] == "released" for claim in claims), claims
    status = cluster.rpc("workspace.status", path=str(cluster.repository))
    assert status["workspace"]["workspace_revision"] == publications, status


@pytest.mark.parametrize("seed", [3, 17, 91])
@pytest.mark.parametrize("contended", [False, True], ids=["disjoint", "same-file"])
def test_six_cli_instances_repeated_seeded_publication(
    cluster: _Cluster, git_run: Callable[..., str], seed: int, contended: bool
) -> None:
    rng = random.Random(seed)
    names = [f"worker-{i}" for i in range(6)]
    expected = {f"docs/{name}.md": f"base {name}\n" for name in names}
    for path, content in expected.items():
        (cluster.repository / path).write_text(content)
    expected["docs/shared.md"] = "base shared\n"
    before = _git_state(cluster, git_run)
    clients = {name: cluster.chat(name) for name in names}
    assert len({p.pid for p in clients.values()}) == len(names)
    published = 0
    for round_number in range(3):
        order = rng.sample(names, len(names))
        proposals = {}
        labels = {name: f"round-{round_number}-{name}" for name in names}
        for name in order:
            changes = {f"docs/{name}.md": f"seed {seed} round {round_number} {name}\n"}
            if contended:
                changes["docs/shared.md"] = f"shared {seed} {round_number} {name}\n"
            proposals[name] = changes
            cluster.submit(clients[name], labels[name], changes)
        for name in names:
            ready = cluster.ready(labels[name])
            paths = list(proposals[name])
            assert [item.split(_CONTEXT)[0] for item in ready["results_0"]] == [
                expected[path] for path in paths
            ]
        tasks = [cluster.task(labels[name]) for name in names]
        assert len({task["session_id"] for task in tasks}) == 6
        assert all(task["coordination_state"] == "active_work" for task in tasks)
        claims = cluster.rpc("claim.list", path=str(cluster.repository))
        current = {task["current_claim_id"] for task in tasks}
        assert all(
            claim["scheduling_mode"] == "optimistic" and not claim["blocking_claim_ids"]
            for claim in claims
            if claim["claim_id"] in current
        )
        assert all(
            (cluster.repository / path).read_text() == content
            for path, content in expected.items()
        )
        for name in rng.sample(names, len(names)):
            cluster.release(labels[name])
        outcomes = {name: cluster.settled(labels[name]) for name in names}
        winners = [name for name in names if outcomes[name]["state"] == "completed"]
        assert len(winners) == (1 if contended else 6), outcomes
        for name in names:
            task = outcomes[name]
            if name in winners:
                expected.update(proposals[name])
            else:
                assert task["state"] == "failed", task
                events = cluster.rpc("task.events", task_id=task["task_id"], limit=500)
                assert any(
                    event["payload"].get("failure_code") == "PATH_BASE_MISMATCH"
                    for event in events
                ), events
                proposal = cluster.rpc("task.diff", task_id=task["task_id"])
                assert set(proposal["paths"]) == set(proposals[name])
                assert all(
                    "+" + value.strip() in proposal["diff"]
                    for value in proposals[name].values()
                )
        published += len(winners)
        assert all(
            (cluster.repository / path).read_text() == content
            for path, content in expected.items()
        )
        _assert_quiescent(cluster, published)
    # Every still-open terminal must read all peers' most recent publications.
    for name in names:
        cluster.submit(
            clients[name], f"read-{name}", dict.fromkeys(expected, ""), read_only=True
        )
    for name in names:
        assert cluster.settled(f"read-{name}")["state"] == "completed"
        evidence = json.loads(
            (cluster.signals / f"read-{name}.observed.json").read_text()
        )
        assert [item.split(_CONTEXT)[0] for item in evidence["results_0"]] == list(
            expected.values()
        )
        cluster.send(clients[name], "/exit")
        assert clients[name].wait(timeout=10) == 0
    _assert_quiescent(cluster, published)
    assert _git_state(cluster, git_run) == before


@pytest.mark.parametrize("change", ["replace", "delete", "symlink"])
def test_external_edit_never_gets_overwritten_or_partially_published(
    cluster: _Cluster, change: str, git_run: Callable[..., str]
) -> None:
    before = _git_state(cluster, git_run)
    a, b = cluster.chat("a"), cluster.chat("b")
    cluster.submit(
        a, "a", {"docs/shared.md": "private shared\n", "docs/a.md": "private a\n"}
    )
    cluster.submit(b, "b", {"docs/b.md": "unaffected peer\n"})
    cluster.ready("a")
    cluster.ready("b")
    target = cluster.repository / "docs/shared.md"
    outside = cluster.root / "external.txt"
    outside.write_text("external editor\n")
    if change == "replace":
        replacement = cluster.repository / "docs/replacement.tmp"
        replacement.write_text("external editor\n")
        replacement.replace(target)
    else:
        target.unlink()
        if change == "symlink":
            target.symlink_to(outside)
    cluster.release("a")
    cluster.release("b")
    failed = cluster.settled("a")
    assert failed["state"] == "failed", failed
    assert cluster.settled("b")["state"] == "completed"
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    assert (cluster.repository / "docs/b.md").read_text() == "unaffected peer\n"
    assert outside.read_text() == "external editor\n"
    if change == "delete":
        assert not target.exists()
    else:
        assert target.read_text() == "external editor\n"
        assert target.is_symlink() is (change == "symlink")
    retained = cluster.rpc("task.diff", task_id=failed["task_id"])
    assert set(retained["paths"]) == {"docs/shared.md", "docs/a.md"}
    _assert_quiescent(cluster, 1)
    assert _git_state(cluster, git_run) == before


@pytest.mark.parametrize("seed", [11, 37, 73])
def test_cancellation_racing_publication_is_atomic_and_peer_survives(
    cluster: _Cluster, seed: int, git_run: Callable[..., str]
) -> None:
    rng = random.Random(seed)
    a, b = cluster.chat("a"), cluster.chat("b")
    before = _git_state(cluster, git_run)
    expected_a = "base a\n"
    expected_shared = "base shared\n"
    publications = 0
    for turn in range(4):
        label = f"race-{turn}"
        peer = f"peer-{turn}"
        next_a, next_shared = f"a {turn}\n", f"shared {turn}\n"
        cluster.submit(a, label, {"docs/a.md": next_a, "docs/shared.md": next_shared})
        cluster.submit(b, peer, {"docs/b.md": f"peer {turn}\n"})
        cluster.ready(label)
        cluster.ready(peer)
        task_id = cluster.task(label)["task_id"]
        barrier = Barrier(2)

        def cancel(barrier: Barrier = barrier, task_id: str = task_id) -> dict:
            barrier.wait(timeout=5)
            return cluster.rpc("task.cancel", task_id=task_id)

        def publish(barrier: Barrier = barrier, label: str = label) -> None:
            barrier.wait(timeout=5)
            cluster.release(label)

        actions = rng.sample([cancel, publish], 2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(action) for action in actions]
            for future in futures:
                future.result(timeout=10)
        cluster.release(peer)
        outcome = cluster.settled(label)
        assert outcome["state"] in {"cancelled", "completed"}, outcome
        if outcome["state"] == "completed":
            expected_a, expected_shared = next_a, next_shared
            publications += 1
        else:
            proposal = cluster.rpc("task.diff", task_id=task_id)
            assert set(proposal["paths"]) == {"docs/a.md", "docs/shared.md"}
            assert proposal["status"] == "cancelled"
        assert cluster.settled(peer)["state"] == "completed"
        publications += 1
        assert (cluster.repository / "docs/a.md").read_text() == expected_a
        assert (cluster.repository / "docs/shared.md").read_text() == expected_shared
        assert (cluster.repository / "docs/b.md").read_text() == f"peer {turn}\n"
        _assert_quiescent(cluster, publications)
    assert _git_state(cluster, git_run) == before
