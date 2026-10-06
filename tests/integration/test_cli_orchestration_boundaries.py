"""Multiple CLI viewers and wrappers preserve one authoritative task history."""

from __future__ import annotations

import json

import pytest
from test_cli_process_orchestration import _Cluster, _wait
from test_cli_process_orchestration import cluster as cluster


@pytest.mark.parametrize("publish", ["auto", "review"])
def test_json_watchers_replay_identical_events_and_exit_when_work_settles(
    cluster: _Cluster, publish: str
) -> None:
    a, b = cluster.chat("a", publish=publish), cluster.chat("b")
    cluster.submit(a, "a", {"docs/a.md": "proposed a\n"})
    cluster.submit(b, "b", {"docs/b.md": "published b\n"})
    cluster.ready("a")
    cluster.ready("b")
    task_id = cluster.task("a")["task_id"]
    arguments = [
        "-m",
        "llm_cli",
        "--profile",
        "test",
        "--json",
        "task",
        "watch",
        task_id,
    ]
    viewers = [cluster._spawn(f"watch-{i}", arguments) for i in range(2)]
    for i in range(2):
        _wait(lambda i=i: (cluster.root / f"watch-{i}.log").stat().st_size > 0)
    cluster.release("a")
    cluster.release("b")
    assert cluster.settled("a")["state"] == (
        "completed" if publish == "auto" else "awaiting_review"
    )
    assert cluster.settled("b")["state"] == "completed"
    for viewer in viewers:
        assert viewer.wait(timeout=5) == 0
    streams = [
        [
            json.loads(line)
            for line in (cluster.root / f"watch-{i}.log").read_text().splitlines()
        ]
        for i in range(2)
    ]
    assert streams[0] == streams[1]
    sequences = [event["sequence"] for event in streams[0]]
    assert sequences == sorted(set(sequences))
    assert len(sequences) > 5
    # Reattachment from a durable cursor produces exactly the remaining suffix.
    cursor = sequences[len(sequences) // 2]
    replay = cluster._spawn("replay", [*arguments, "--after", str(cursor)])
    assert replay.wait(timeout=5) == 0
    suffix = [
        json.loads(line)
        for line in (cluster.root / "replay.log").read_text().splitlines()
    ]
    assert suffix == [event for event in streams[0] if event["sequence"] > cursor]
    assert (cluster.repository / "docs/b.md").read_text() == "published b\n"
    assert (cluster.repository / "docs/a.md").read_text() == (
        "proposed a\n" if publish == "auto" else "base a\n"
    )


def test_two_wrappers_resuming_one_session_cannot_launch_duplicate_active_work(
    cluster: _Cluster,
) -> None:
    first = cluster.chat("first")
    cluster.submit(first, "a", {"docs/a.md": "one task only\n"})
    cluster.ready("a")
    task = cluster.task("a")
    second = cluster.chat("second", resume=task["session_id"])
    cluster.submit(second, "duplicate", {"docs/shared.md": "must not publish\n"})
    # The refusal and the /attach hint are separate writes; wait for the hint.
    _wait(
        lambda: (
            f"/attach {task['task_id']}" in (cluster.root / "second.log").read_text()
        )
    )
    assert (
        "finish the session's current task" in (cluster.root / "second.log").read_text()
    )
    tasks = cluster.rpc("task.list")
    assert [t["task_id"] for t in tasks] == [task["task_id"]]
    cluster.send(second, "/attach")
    _wait(
        lambda: "Getting ready" in (cluster.root / "second.log").read_text()
    )
    assert "staged docs/a.md" not in (cluster.root / "second.log").read_text()
    cluster.release("a")
    assert cluster.settled("a")["state"] == "completed"
    cluster.submit(second, "followup", {"docs/shared.md": "after previous task\n"})
    cluster.ready("followup")
    cluster.release("followup")
    assert cluster.settled("followup")["state"] == "completed"
    assert cluster.task("followup")["session_id"] == task["session_id"]
    assert (cluster.repository / "docs/a.md").read_text() == "one task only\n"
    assert (
        cluster.repository / "docs/shared.md"
    ).read_text() == "after previous task\n"
