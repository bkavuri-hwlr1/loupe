"""Two real chat processes coordinate through a third, daemon process.

Only model responses are scripted. CLI parsing, sessions, Unix RPC, event streams,
tool execution, publication and persisted recovery state use production code.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient
from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult

_SETTLED = {"completed", "failed", "cancelled", "awaiting_review"}
_CONTEXT = "\n\n[Coordination update: advisory checkout facts]\n"


def _wait(predicate: Callable[[], Any], *, timeout: float = 20) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for CLI orchestration")


def _turn(name: str, **arguments: object) -> ToolCallRequest:
    return ToolCallRequest(
        name=name, call_id=name + str(arguments), arguments=arguments
    )


class _BarrierProvider:
    """Pause after private edits so both terminals have observed the same base."""

    name = "process-test"

    def __init__(self, model: str) -> None:
        self.model = model
        self.signals = Path(os.environ["ORCHESTRATION_SIGNALS"])
        self.restored = False
        self.phase = 0
        self.request: dict[str, Any] = {}
        self.evidence: dict[str, Any] = {}
        # Conflicted files being read, by call id, to resolve them.
        self.reading: dict[str, str] = {}

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> _BarrierProvider:
        self.restored = state is not None
        return self

    def snapshot(self) -> Mapping[str, object]:
        return {"last_request": self.request}

    def _record(self, suffix: str) -> None:
        path = self.signals / f"{self.request['label']}.{suffix}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.evidence))
        temporary.replace(path)

    def send_user(self, text: str) -> ModelTurn:
        self.request, _ = json.JSONDecoder().raw_decode(text.split("ORCHESTRATION ")[1])
        self.evidence = {"opening": text, "restored": self.restored}
        return ModelTurn(
            text="Reading shared files",
            tool_calls=tuple(_turn("read_file", path=p) for p in self.request["files"]),
        )

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        if self.phase >= 3:
            return self._after_refusal(results)
        assert all(not item.is_error for item in results), results
        self.evidence[f"results_{self.phase}"] = [r.content for r in results]
        self.phase += 1
        if self.phase == 1:
            if self.request.get("read_only"):
                self._record("observed")
                return ModelTurn(
                    text="",
                    tool_calls=(
                        _turn(
                            "finish_task",
                            answer="Read the requested files.",
                            summary="read",
                        ),
                    ),
                )
            return ModelTurn(
                text="Preparing private edits",
                tool_calls=tuple(
                    _turn("write_file", path=p, content=c)
                    for p, c in self.request["files"].items()
                ),
            )
        if self.phase == 2:
            self._record("ready")
            _wait(
                lambda: (self.signals / f"{self.request['label']}.release").exists(),
                timeout=150,
            )
            # Another tool boundary delivers metadata for the peer publication.
            return ModelTurn(text="", tool_calls=(_turn("list_files", path="docs"),))
        self._record("observed")
        return ModelTurn(
            text="",
            tool_calls=(
                _turn(
                    "finish_task",
                    answer="Prepared the requested edits.",
                    summary="edited",
                ),
            ),
        )

    def _after_refusal(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        """Answer a refused finish the way an agent would.

        Conflicts are resolved by keeping both sides, the published one first;
        anything else, such as an edit set aside, is accepted.
        """

        self.phase += 1
        reads = {r.call_id: r for r in results if r.call_id in self.reading}
        if reads:
            writes = tuple(
                ToolCallRequest(
                    name="write_file",
                    call_id=f"resolve-write-{self.phase}-{index}",
                    arguments={
                        "path": self.reading[call_id],
                        "content": _both(read.content.split(_CONTEXT)[0]),
                    },
                )
                for index, (call_id, read) in enumerate(reads.items())
            )
            self.reading = {}
            return ModelTurn(text="Resolving the conflicts", tool_calls=writes)
        refused = [r.content for r in results if r.is_error]
        self.evidence.setdefault("refusals", []).extend(refused)
        marker = "Resolve the conflict markers in "
        conflict = next((text for text in refused if marker in text), None)
        if conflict is not None:
            paths = conflict.split(marker)[1].split(" before ")[0].split(", ")
            self.reading = {
                f"resolve-read-{self.phase}-{index}": path
                for index, path in enumerate(paths)
            }
            return ModelTurn(
                text="Reading the conflicts",
                tool_calls=tuple(
                    ToolCallRequest(
                        name="read_file", call_id=call_id, arguments={"path": path}
                    )
                    for call_id, path in self.reading.items()
                ),
            )
        return ModelTurn(
            text="",
            tool_calls=(
                ToolCallRequest(
                    name="finish_task",
                    call_id=f"refinish-{self.phase}",
                    arguments={
                        "answer": "Prepared the requested edits.",
                        "summary": "edited",
                    },
                ),
            ),
        )

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        self.evidence["terminal_results"] = [result.content for result in results]


def _both(text: str) -> str:
    """Resolve every conflict by keeping the published side, then ours."""

    lines, ours, theirs, part = [], [], [], None
    for line in text.splitlines(keepends=True):
        if line.startswith("<<<<<<< "):
            part = ours
        elif line.startswith("||||||| "):
            part = []
        elif line.startswith("=======") and part is not None:
            part = theirs
        elif line.startswith(">>>>>>> "):
            lines += theirs + ours
            ours, theirs, part = [], [], None
        elif part is not None:
            part.append(line)
        else:
            lines.append(line)
    return "".join(lines)


def _daemon_entry() -> None:
    from authority_helpers import install_test_authority
    install_test_authority()
    """Install the test model at the provider seam, then run the normal daemon."""
    from llm_cli.daemon import main

    original = main.DaemonService

    class Service(original):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.providers.register(
                "process-test", lambda model: _BarrierProvider(model)
            )

    main.DaemonService = Service
    main.main(["--profile", "test"])


class _Cluster:
    def __init__(self, root: Path, repository: Path, runtime: str) -> None:
        self.root, self.repository = root, repository
        self.signals = root / "signals"
        self.signals.mkdir()
        self.env = {
            **os.environ,
            "LLM_COORD_CONFIG_HOME": str(root / "config"),
            "LLM_COORD_DATA_HOME": str(root / "data"),
            "LLM_COORD_STATE_HOME": str(root / "state"),
            "LLM_COORD_RUNTIME_DIR": str(Path(runtime).resolve()),
            "ORCHESTRATION_SIGNALS": str(self.signals),
            "PYTHONPATH": os.pathsep.join(
                (str(Path(__file__).parent), str(Path(__file__).parents[2] / "src"))
            ),
            "PYTHONUNBUFFERED": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        self.paths = AppPaths.resolve("test", environ=self.env)
        # These tests assert correctness across real processes, not RPC latency.
        # A stalled CI runner must not spend the product's five-second UI budget
        # while polling task.list (the client seen in the macOS CI traceback).
        self.client = DaemonClient(self.paths, timeout_seconds=60.0)
        self.stack = ExitStack()
        self.processes: list[subprocess.Popen[str]] = []
        self.process_names: dict[int, str] = {}
        self.submissions: dict[str, subprocess.Popen[str]] = {}
        self.daemon = self._spawn(
            "daemon",
            [
                "-c",
                "from test_cli_process_orchestration import _daemon_entry; "
                "_daemon_entry()",
            ],
        )
        try:
            _wait(lambda: self.paths.socket.exists() or self.daemon.poll() is not None)
            assert self.daemon.poll() is None, (root / "daemon.log").read_text()
            self.rpc("system.ping")
        except BaseException:
            self.close()
            raise

    def _spawn(self, name: str, args: list[str]) -> subprocess.Popen[str]:
        from authority_helpers import isolated_cli_arguments

        args = isolated_cli_arguments(args)
        log = self.stack.enter_context((self.root / f"{name}.log").open("w"))
        process = subprocess.Popen(
            [sys.executable, *args],
            env=self.env,
            cwd=self.repository,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.processes.append(process)
        self.process_names[process.pid] = name
        return process

    def chat(
        self, name: str, *, resume: str | None = None, publish: str = "auto"
    ) -> subprocess.Popen[str]:
        return self._spawn(
            name,
            [
                "-m",
                "llm_cli",
                "--profile",
                "test",
                "chat",
                "--plain",
                "--repo",
                str(self.repository),
                "--scope",
                "docs/",
                "--publish",
                publish,
                *(
                    ["--resume", resume]
                    if resume
                    else ["--provider", "process-test", "--model", name]
                ),
            ],
        )

    def send(self, process: subprocess.Popen[str], text: str) -> None:
        assert process.poll() is None
        assert process.stdin is not None
        process.stdin.write(text + "\n")
        process.stdin.flush()

    def submit(
        self,
        process: subprocess.Popen[str],
        label: str,
        files: dict[str, str],
        **kwargs: Any,
    ) -> None:
        self.submissions[label] = process
        self.send(
            process,
            "ORCHESTRATION " + json.dumps({"label": label, "files": files, **kwargs}),
        )

    def rpc(self, method: str, **params: Any) -> Any:
        return self.client.call(method, params, autostart=False)

    def diagnostics(self) -> str:
        """Read existing evidence without another potentially blocked RPC."""
        evidence: dict[str, Any] = {
            "signals": sorted(p.name for p in self.signals.iterdir())
        }
        evidence["processes"] = {
            self.process_names[process.pid]: {
                "pid": process.pid,
                "exit": process.poll(),
            }
            for process in self.processes
        }
        evidence["logs"] = {}
        for name in self.process_names.values():
            path = self.root / f"{name}.log"
            if path.exists():
                with path.open("rb") as log:
                    log.seek(max(0, path.stat().st_size - 8_192))
                    evidence["logs"][name] = log.read().decode(errors="replace")
        try:
            with closing(sqlite3.connect(
                self.paths.control_db.as_uri() + "?mode=ro", timeout=0.1, uri=True
            )) as con:
                con.row_factory = sqlite3.Row
                evidence["tasks"] = [
                    dict(row)
                    for row in con.execute(
                        "SELECT task_id,title,state,failure_code FROM tasks "
                        "ORDER BY created_at DESC LIMIT 12"
                    )
                ]
                evidence["events"] = [
                    dict(row)
                    for row in con.execute(
                        "SELECT task_id,sequence,event_type,payload_json "
                        "FROM task_events "
                        "ORDER BY sequence DESC LIMIT 24"
                    )
                ]
        except sqlite3.Error as exc:
            evidence["database_error"] = str(exc)
        return json.dumps(evidence, indent=2)

    def ready_all(
        self, labels: Sequence[str], *, timeout: float = 120
    ) -> dict[str, dict[str, Any]]:
        """All participants share one deadline; a dead child fails immediately."""
        deadline = time.monotonic() + timeout
        pending = set(labels)
        while pending:
            pending = {
                label
                for label in pending
                if not (self.signals / f"{label}.ready.json").exists()
            }
            if not pending:
                break
            exited = self.daemon.poll() is not None or any(
                self.submissions[label].poll() is not None for label in pending
            )
            if exited or time.monotonic() >= deadline:
                raise AssertionError(
                    f"CLI preparation did not reach its barrier: {sorted(pending)}\n"
                    + self.diagnostics()
                )
            time.sleep(0.02)
        return {
            label: json.loads((self.signals / f"{label}.ready.json").read_text())
            for label in labels
        }

    def ready(self, label: str) -> dict[str, Any]:
        return self.ready_all((label,))[label]

    def release(self, label: str) -> None:
        (self.signals / f"{label}.release").touch()

    def task(self, label: str) -> dict[str, Any]:
        return _wait(
            lambda: next(
                (
                    t
                    for t in self.rpc("task.list")
                    if f'"label": "{label}"' in t["title"]
                ),
                None,
            )
        )

    def settled(self, label: str) -> dict[str, Any]:
        return _wait(
            lambda: task if (task := self.task(label))["state"] in _SETTLED else None
        )

    def close(self) -> None:
        for label in self.submissions:
            self.release(label)
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdin:
                process.stdin.close()
        self.stack.close()


@pytest.fixture
def cluster(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> Iterator[_Cluster]:
    repository = repository_factory(
        tmp_path, {f"docs/{name}.md": f"base {name}\n" for name in ("a", "b", "shared")}
    )
    # macOS limits Unix socket names to 104 bytes, shorter than pytest paths.
    with TemporaryDirectory(prefix="mfi-", dir="/tmp") as runtime:
        group = _Cluster(tmp_path, repository, runtime)
        try:
            yield group
        finally:
            group.close()


def _concurrent(
    cluster: _Cluster, first: subprocess.Popen[str], second: subprocess.Popen[str]
) -> None:
    assert first.pid != second.pid != cluster.daemon.pid
    assert first.poll() is None and second.poll() is None
    tasks = [cluster.task(name) for name in ("a", "b")]
    assert all(task["coordination_state"] == "active_work" for task in tasks), tasks
    assert all(task["state"] not in _SETTLED for task in tasks), tasks
    assert tasks[0]["session_id"] != tasks[1]["session_id"]
    claims = cluster.rpc("claim.list", path=str(cluster.repository))
    active = [
        c for c in claims if c["claim_id"] in {t["current_claim_id"] for t in tasks}
    ]
    assert len(active) == 2
    assert all(
        c["scheduling_mode"] == "optimistic" and not c["blocking_claim_ids"]
        for c in active
    )
    assert active[0]["workspace_id"] == active[1]["workspace_id"]


def test_two_cli_processes_publish_disjoint_edits_and_resume(
    cluster: _Cluster, git_run: Callable[..., str]
) -> None:
    before = [
        git_run(cluster.repository, *args) for args in (("show-ref",), ("write-tree",))
    ]
    a, b = cluster.chat("a"), cluster.chat("b")
    cluster.submit(a, "a", {"docs/a.md": "new a\n"})
    cluster.submit(b, "b", {"docs/b.md": "new b\n"})
    cluster.ready("a")
    cluster.ready("b")
    _concurrent(cluster, a, b)
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    assert (cluster.repository / "docs/b.md").read_text() == "base b\n"
    cluster.release("a")
    assert cluster.settled("a")["state"] == "completed"
    cluster.release("b")
    assert cluster.settled("b")["state"] == "completed"
    observed = json.loads((cluster.signals / "b.observed.json").read_text())
    packet = json.loads(
        observed["results_2"][-1]
        .split(_CONTEXT)[1]
        .split("\n[End coordination update]")[0]
    )
    assert any(
        c.get("workspace_revision") == 1
        and c["paths"] == [{"path": "docs/a.md", "overlaps_scope": True}]
        for c in packet["changes"]
    )

    session_id = cluster.task("a")["session_id"]
    cluster.send(a, "/detach")
    assert a.wait(timeout=10) == 0
    resumed = cluster.chat("resumed", resume=session_id)
    cluster.submit(
        resumed, "followup", {"docs/a.md": "", "docs/b.md": ""}, read_only=True
    )
    _wait(lambda: (cluster.signals / "followup.observed.json").exists())
    followup = json.loads((cluster.signals / "followup.observed.json").read_text())
    assert followup["restored"] is True
    assert [v.split(_CONTEXT)[0] for v in followup["results_0"]] == [
        "new a\n",
        "new b\n",
    ]
    assert cluster.settled("followup")["state"] == "completed"
    assert cluster.task("followup")["session_id"] == session_id
    assert before == [
        git_run(cluster.repository, *args) for args in (("show-ref",), ("write-tree",))
    ]
    for process in (resumed, b):
        cluster.send(process, "/exit")
        assert process.wait(timeout=10) == 0


@pytest.mark.parametrize("winner", ["a", "b"])
def test_two_cli_processes_merge_a_conflicting_batch(
    cluster: _Cluster, winner: str, git_run: Callable[..., str]
) -> None:
    before = [
        git_run(cluster.repository, *args) for args in (("show-ref",), ("write-tree",))
    ]
    a, b = cluster.chat("a"), cluster.chat("b")
    loser_name = "b" if winner == "a" else "a"
    loser_process = b if winner == "a" else a
    for name, process in (("a", a), ("b", b)):
        cluster.submit(
            process,
            name,
            {"docs/shared.md": f"from {name}\n", f"docs/{name}.md": f"new {name}\n"},
        )
    cluster.ready("a")
    cluster.ready("b")
    _concurrent(cluster, a, b)
    cluster.release(winner)
    assert cluster.settled(winner)["state"] == "completed"
    cluster.release(loser_name)
    # The loser's edits were merged onto the winner's. Its shared line
    # conflicted, so it resolved that, keeping both, and then published.
    loser = cluster.settled(loser_name)
    assert loser["state"] == "completed", loser
    assert (cluster.repository / "docs/shared.md").read_text() == (
        f"from {winner}\nfrom {loser_name}\n"
    )
    assert (cluster.repository / f"docs/{winner}.md").read_text() == f"new {winner}\n"
    assert (
        cluster.repository / f"docs/{loser_name}.md"
    ).read_text() == f"new {loser_name}\n"
    cluster.send(loser_process, "/exit")
    assert loser_process.wait(timeout=10) == 0
    output = (cluster.root / f"{loser_name}.log").read_text()
    assert "docs/shared.md: conflict with newer changes" in output
    assert before == [
        git_run(cluster.repository, *args) for args in (("show-ref",), ("write-tree",))
    ]


def test_second_cli_can_stop_a_peer_without_stopping_its_own_work(
    cluster: _Cluster,
) -> None:
    a, b = cluster.chat("a"), cluster.chat("b")
    cluster.submit(a, "a", {"docs/a.md": "cancel me\n"})
    cluster.ready("a")
    task_id = cluster.task("a")["task_id"]
    cluster.send(b, f"/stop {task_id}")
    _wait(lambda: "stopping" in (cluster.root / "b.log").read_text())
    cluster.submit(b, "b", {"docs/b.md": "peer survived\n"})
    cluster.ready("b")
    cluster.release("b")
    assert cluster.settled("b")["state"] == "completed"
    cluster.release("a")
    assert cluster.settled("a")["state"] == "cancelled"
    assert (cluster.repository / "docs/a.md").read_text() == "base a\n"
    assert (cluster.repository / "docs/b.md").read_text() == "peer survived\n"
    proposal = cluster.rpc("task.diff", task_id=task_id)
    assert proposal["status"] == "cancelled" and "+cancel me" in proposal["diff"]
