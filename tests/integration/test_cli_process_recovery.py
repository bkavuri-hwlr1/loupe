"""Crash real daemons and wrappers while multiple CLI tasks own private work."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from test_cli_process_orchestration import _BarrierProvider, _Cluster, _wait

from llm_cli.errors import LlmCoordError
from llm_cli.providers.base import ModelTurn


class _RecoveryProvider(_BarrierProvider):
    """Persist the scripted model's turn cursor exactly like a real provider."""

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> _RecoveryProvider:
        super().session(system=system, tools=tools, state=state)
        if state is not None:
            self.phase = int(state["phase"])
            self.request = dict(state["request"])
            self.evidence = dict(state["evidence"])
            marker = self.signals / f"{self.request['label']}.restored.json"
            temporary = marker.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"phase": self.phase, "pid": os.getpid()})
            )
            temporary.replace(marker)
        return self

    def snapshot(self) -> Mapping[str, object]:
        return {
            "phase": self.phase,
            "request": self.request,
            "evidence": self.evidence,
        }

    def send_user(self, text: str) -> ModelTurn:
        self.phase = 0
        return super().send_user(text)


def _daemon_entry() -> None:
    from llm_cli.daemon import main

    original = main.DaemonService

    class Service(original):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.providers.register(
                "process-test", lambda model: _RecoveryProvider(model)
            )
            settle = self.coordinator.settle_shared_execution
            signals = Path(os.environ["ORCHESTRATION_SIGNALS"])

            def pause_after_commit(
                execution_id: str, *, outcome: str, failure_code: str | None = None
            ) -> Any:
                trigger = signals / "pause-settlement"
                if outcome == "published" and trigger.exists():
                    trigger.unlink()
                    (signals / "committed.json").write_text(
                        json.dumps({"execution_id": execution_id})
                    )
                    _wait(lambda: (signals / "settle.release").exists(), timeout=30)
                return settle(
                    execution_id, outcome=outcome, failure_code=failure_code
                )

            self.coordinator.settle_shared_execution = pause_after_commit

    main.DaemonService = Service
    main.main(["--profile", "test"])


_DAEMON_ARGS = [
    "-c",
    "from test_cli_process_recovery import _daemon_entry; _daemon_entry()",
]


class _RecoveryCluster(_Cluster):
    def _spawn(self, name: str, args: list[str]) -> subprocess.Popen[str]:
        return super()._spawn(name, _DAEMON_ARGS if name == "daemon" else args)

    def crash(self) -> None:
        self.daemon.kill()
        assert self.daemon.wait(timeout=5) < 0

    def restart(self) -> None:
        assert self.daemon.poll() is not None
        self.daemon = self._spawn(f"daemon-{len(self.processes)}", _DAEMON_ARGS)

        def available() -> bool:
            assert self.daemon.poll() is None
            try:
                self.rpc("system.ping")
            except LlmCoordError:
                return False
            return True

        _wait(available)

    def restored(self, label: str) -> dict[str, Any]:
        path = self.signals / f"{label}.restored.json"
        _wait(path.exists)
        result = json.loads(path.read_text())
        assert result["pid"] == self.daemon.pid
        return result

    def revision(self, session_id: str) -> int:
        return self.rpc("session.show", session_id=session_id)["session"][
            "conversation_revision"
        ]

    def close(self) -> None:
        (self.signals / "settle.release").touch()
        super().close()


@pytest.fixture
def recovery_cluster(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> Iterator[_RecoveryCluster]:
    repository = repository_factory(
        tmp_path, {f"docs/{name}.md": f"base {name}\n" for name in ("a", "b", "shared")}
    )
    with TemporaryDirectory(prefix="mfi-rec-", dir="/tmp") as runtime:
        group = _RecoveryCluster(tmp_path, repository, runtime)
        try:
            yield group
        finally:
            group.close()


def _start_pair(cluster: _RecoveryCluster) -> tuple[subprocess.Popen[str], ...]:
    pair = cluster.chat("a"), cluster.chat("b")
    for label, process in zip(("a", "b"), pair, strict=True):
        cluster.submit(process, label, {f"docs/{label}.md": f"new {label}\n"})
        cluster.ready(label)
    return pair


def test_abrupt_cli_disconnect_preserves_own_task_and_peer(
    recovery_cluster: _RecoveryCluster,
) -> None:
    cluster = recovery_cluster
    first, second = _start_pair(cluster)
    session_id = cluster.task("a")["session_id"]
    first.kill()
    assert first.wait(timeout=5) < 0
    assert cluster.daemon.poll() is None and second.poll() is None
    cluster.release("b")
    assert cluster.settled("b")["state"] == "completed"
    cluster.release("a")
    assert cluster.settled("a")["state"] == "completed"
    resumed = cluster.chat("resumed", resume=session_id)
    cluster.submit(
        resumed, "followup", {"docs/a.md": "", "docs/b.md": ""}, read_only=True
    )
    _wait(lambda: (cluster.signals / "followup.observed.json").exists())
    assert cluster.settled("followup")["state"] == "completed"
    evidence = json.loads((cluster.signals / "followup.observed.json").read_text())
    assert evidence["restored"] is True
    assert evidence["results_0"][0].startswith("new a\n")
    assert evidence["results_0"][1].startswith("new b\n")
    assert cluster.revision(session_id) == 2


@pytest.mark.parametrize("restarts", [1, 2])
def test_daemon_crash_resumes_two_private_batches_once(
    recovery_cluster: _RecoveryCluster,
    restarts: int,
    git_run: Callable[..., str],
) -> None:
    cluster = recovery_cluster
    before = [
        git_run(cluster.repository, *args) for args in (("show-ref",), ("write-tree",))
    ]
    _start_pair(cluster)
    originals = {name: cluster.task(name) for name in ("a", "b")}
    for _ in range(restarts):
        cluster.crash()
        for marker in cluster.signals.glob("*.restored.json"):
            marker.unlink()
        cluster.restart()
        for name in ("a", "b"):
            assert cluster.restored(name)["phase"] == 1
            assert cluster.task(name)["attempt"] == originals[name]["attempt"]
            assert (
                cluster.repository / f"docs/{name}.md"
            ).read_text() == f"base {name}\n"
    for name in ("b", "a"):
        cluster.release(name)
        assert cluster.settled(name)["state"] == "completed"
        assert cluster.revision(originals[name]["session_id"]) == 1
        assert (cluster.repository / f"docs/{name}.md").read_text() == f"new {name}\n"
    for _ in range(2):
        cluster.rpc("task.recover")
    assert all(cluster.revision(t["session_id"]) == 1 for t in originals.values())
    assert before == [
        git_run(cluster.repository, *args) for args in (("show-ref",), ("write-tree",))
    ]


def test_external_edit_during_outage_retains_entire_original_proposal(
    recovery_cluster: _RecoveryCluster,
) -> None:
    cluster = recovery_cluster
    first, second = cluster.chat("a"), cluster.chat("b")
    cluster.submit(
        first, "a", {"docs/shared.md": "stale a\n", "docs/a.md": "private a\n"}
    )
    cluster.submit(second, "b", {"docs/b.md": "new b\n"})
    cluster.ready("a")
    cluster.ready("b")
    cluster.crash()
    (cluster.repository / "docs/shared.md").write_text("external edit while down\n")
    cluster.restart()
    cluster.restored("a")
    cluster.restored("b")
    cluster.release("b")
    assert cluster.settled("b")["state"] == "completed"
    cluster.release("a")
    failed = cluster.settled("a")
    assert failed["state"] == "failed"
    assert (
        cluster.repository / "docs/shared.md"
    ).read_text() == "external edit while down\n"
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    assert (cluster.repository / "docs/b.md").read_text() == "new b\n"
    proposal = cluster.rpc("task.diff", task_id=failed["task_id"])
    assert set(proposal["paths"]) == {"docs/a.md", "docs/shared.md"}
    assert "-base shared" in proposal["diff"] and "+stale a" in proposal["diff"]
    assert cluster.revision(failed["session_id"]) == 0


def test_persisted_stop_survives_daemon_crash_without_stopping_peer(
    recovery_cluster: _RecoveryCluster,
) -> None:
    cluster = recovery_cluster
    _start_pair(cluster)
    original = cluster.task("a")
    assert "+new a" in cluster.rpc("task.diff", task_id=original["task_id"])["diff"]
    cluster.rpc("task.cancel", task_id=original["task_id"])
    cluster.crash()
    cluster.restart()
    assert cluster.settled("a")["state"] == "cancelled"
    cluster.restored("b")
    cluster.release("b")
    assert cluster.settled("b")["state"] == "completed"
    cluster.release("a")
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    assert (cluster.repository / "docs/b.md").read_text() == "new b\n"
    assert not (cluster.signals / "a.restored.json").exists()
    proposal = cluster.rpc("task.diff", task_id=original["task_id"])
    assert proposal["status"] == "cancelled" and "+new a" in proposal["diff"]
    assert "-base a" in proposal["diff"]
    assert (
        cluster.rpc("task.apply", task_id=original["task_id"])["state"] == "published"
    )
    assert (cluster.repository / "docs/a.md").read_text() == "new a\n"
    assert (cluster.repository / "docs/b.md").read_text() == "new b\n"


@pytest.mark.parametrize("external_after_commit", [False, True])
def test_committed_batch_recovers_settlement_once_while_peer_resumes(
    recovery_cluster: _RecoveryCluster, external_after_commit: bool
) -> None:
    cluster = recovery_cluster
    _start_pair(cluster)
    originals = {name: cluster.task(name) for name in ("a", "b")}
    (cluster.signals / "pause-settlement").touch()
    cluster.release("a")
    _wait(lambda: (cluster.signals / "committed.json").exists())
    assert (cluster.repository / "docs/a.md").read_text() == "new a\n"
    assert (cluster.repository / "docs/b.md").read_text() == "base b\n"
    cluster.crash()
    if external_after_commit:
        (cluster.repository / "docs/a.md").write_text("later external edit\n")
    cluster.restart()
    assert cluster.settled("a")["state"] == "completed"
    assert cluster.revision(originals["a"]["session_id"]) == 1
    cluster.restored("b")
    cluster.release("b")
    assert cluster.settled("b")["state"] == "completed"
    for _ in range(2):
        cluster.rpc("task.recover")
    cluster.crash()
    cluster.restart()
    assert all(cluster.revision(t["session_id"]) == 1 for t in originals.values())
    assert not (cluster.signals / "a.restored.json").exists()
    assert (cluster.repository / "docs/a.md").read_text() == (
        "later external edit\n" if external_after_commit else "new a\n"
    )
    assert (cluster.repository / "docs/b.md").read_text() == "new b\n"


@pytest.mark.parametrize("corruption", ["invalid_shape", "outside_scope"])
def test_bad_cancelled_checkpoint_is_isolated_and_repairable(
    recovery_cluster: _RecoveryCluster, corruption: str
) -> None:
    cluster = recovery_cluster
    _start_pair(cluster)
    original = cluster.task("a")
    cluster.rpc("task.cancel", task_id=original["task_id"])
    cluster.crash()
    with sqlite3.connect(cluster.paths.control_db) as connection:
        execution_id, raw = connection.execute(
            "SELECT c.execution_id,c.checkpoint_json FROM execution_checkpoints c "
            "JOIN task_executions e ON e.execution_id=c.execution_id "
            "WHERE e.task_id=?",
            (original["task_id"],),
        ).fetchone()
        corrupted = json.loads(raw)
        if corruption == "invalid_shape":
            corrupted["tool_usage"] = "invalid"
        else:
            corrupted["tool_usage"]["shared_workspace_state"]["files"][0]["path"] = (
                "outside-scope.md"
            )
        connection.execute(
            "UPDATE execution_checkpoints SET checkpoint_json=? WHERE execution_id=?",
            (json.dumps(corrupted), execution_id),
        )
    cluster.restart()
    outcomes = cluster.rpc("system.ping")["startup_recovery"]["outcomes"]
    assert next(
        item for item in outcomes if item["task_id"] == original["task_id"]
    )["resolution"] == "operator_attention"
    cluster.restored("b")
    cluster.release("b")
    assert cluster.settled("b")["state"] == "completed"
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    assert not (cluster.repository / "outside-scope.md").exists()
    assert not (cluster.signals / "a.restored.json").exists()
    cluster.crash()
    with sqlite3.connect(cluster.paths.control_db) as connection:
        connection.execute(
            "UPDATE execution_checkpoints SET checkpoint_json=? WHERE execution_id=?",
            (raw, execution_id),
        )
    cluster.restart()
    assert cluster.settled("a")["state"] == "cancelled"
    proposal = cluster.rpc("task.diff", task_id=original["task_id"])
    assert "+new a" in proposal["diff"]
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    assert cluster.revision(cluster.task("b")["session_id"]) == 1
