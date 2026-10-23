#!/usr/bin/env python3
"""Exercise real local daemons with a deterministic, credential-free provider.

Run from an all-extras development environment: python scripts/soak.py
--duration 3600 --output /tmp/soak.json. All repositories, profile state and
physical authority are disposable; no configured user provider is constructed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient
from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult


class InvariantFailure(RuntimeError):
    """The run observed an incorrect result or exceeded a recorded bound."""


@dataclass(frozen=True)
class Bounds:
    database_bytes: int = 128 * 1024 * 1024
    candidate_bytes: int = 128 * 1024 * 1024
    open_files: int = 256
    threads: int = 48
    memory_bytes: int = 768 * 1024 * 1024
    rpc_p99_ms: float = 5000


def positive(value: str) -> float:
    result = float(value)
    if not 0 < result < float("inf"):
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--duration", type=positive, default=1800, help="seconds (default: 1800)"
    )
    result.add_argument("--seed", type=int, default=20261022)
    result.add_argument("--restart-interval", type=positive, default=120)
    result.add_argument("--cycle-interval", type=positive, default=10)
    result.add_argument("--output", type=Path, default=Path("soak-result.json"))
    result.add_argument(
        "--keep", action="store_true", help="retain disposable evidence"
    )
    result.add_argument(
        "--inject-lost-write",
        action="store_true",
        help="negative control: delete one published file",
    )
    return result


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        },
        timeout=30,
    ).stdout.strip()


def wait(predicate: Callable[[], Any], timeout: float = 60) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.05)
    raise InvariantFailure("operation did not settle before its 60-second bound")


def verify_files(repository: Path, expected: Mapping[str, str | None]) -> None:
    for relative, content in expected.items():
        target = repository / relative
        actual = target.read_text() if target.exists() else None
        if actual != content:
            raise InvariantFailure(f"published change lost or corrupted: {relative}")


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def check_bounds(sample: Mapping[str, float | int], bounds: Bounds) -> None:
    for name, maximum in asdict(bounds).items():
        if name not in sample:
            raise InvariantFailure(f"resource metric missing: {name}")
        if sample[name] > maximum:
            raise InvariantFailure(
                f"resource bound exceeded: {name}={sample[name]} > {maximum}"
            )


def _tool(name: str, **arguments: object) -> ToolCallRequest:
    return ToolCallRequest(
        call_id=f"{name}-{arguments.get('path', '')}", name=name, arguments=arguments
    )


class SoakProvider:
    """Durable read/write/finish script, paused after editing for crash replay."""

    name = "soak-scripted"

    def __init__(self, model: str) -> None:
        self.model = model
        self.phase = 0
        self.request: dict[str, Any] = {}

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> SoakProvider:
        if state:
            self.phase = int(state["phase"])
            self.request = dict(state["request"])
        return self

    def snapshot(self) -> Mapping[str, object]:
        return {"phase": self.phase, "request": self.request}

    def send_user(self, text: str) -> ModelTurn:
        self.phase = 0
        self.request, _ = json.JSONDecoder().raw_decode(text.split("SOAK ", 1)[1])
        return ModelTurn(
            text="", tool_calls=(_tool("read_file", path=self.request["path"]),)
        )

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        # A new file may legitimately be absent at the first read. Every later
        # tool result must succeed, including the source read on following turns.
        if self.phase and any(item.is_error for item in results):
            raise InvariantFailure(
                f"scripted tool failed: {[item.content for item in results]}"
            )
        self.phase += 1
        if self.phase == 1:
            return ModelTurn(
                text="",
                tool_calls=(
                    _tool(
                        "write_file",
                        path=self.request["path"],
                        content=self.request["content"],
                    ),
                ),
            )
        if self.phase == 2:
            signals = Path(os.environ["LOUPE_SOAK_ROOT"]) / "signals"
            (signals / (self.request["label"] + ".ready")).write_text(str(os.getpid()))
            wait(lambda: (signals / (self.request["label"] + ".release")).exists())
            return ModelTurn(
                text="",
                tool_calls=(
                    _tool("finish_task", answer="Soak edit complete", summary="soak"),
                ),
            )
        raise InvariantFailure("finish was refused; no scripted retry can hide it")

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        if any(item.is_error for item in results):
            raise InvariantFailure("terminal scripted tool failed")


def daemon_entry(root: Path) -> None:
    """Inject a disposable authority at construction, never via product config."""
    from llm_cli.daemon import main

    original = main.DaemonService

    class Service(original):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["authority_root"] = root / "physical-authority" / "v1"
            super().__init__(*args, **kwargs)
            self.providers.register(
                SoakProvider.name, lambda model: SoakProvider(str(model))
            )

    main.DaemonService = Service
    main.main(["--profile", "soak"])


class Cluster:
    def __init__(self, root: Path, bounds: Bounds) -> None:
        self.root, self.bounds = root, bounds
        self.repository = root / "repository"
        self.repository.mkdir()
        (root / "signals").mkdir()
        (self.repository / "docs").mkdir()
        (self.repository / "docs/base.txt").write_text("base\n")
        (self.repository / ".gitignore").write_text(".env\n")
        (self.repository / ".env").write_text("SOAK_VALUE=private-soak-runtime-value\n")
        git(self.repository, "init", "-b", "main")
        git(self.repository, "config", "user.name", "Loupe soak")
        git(self.repository, "config", "user.email", "soak@example.invalid")
        git(self.repository, "add", "-A")
        git(self.repository, "commit", "-qm", "soak base")
        git(self.repository, "branch", "peer")
        self.env = {
            **os.environ,
            "LOUPE_SOAK_ROOT": str(root),
            "LLM_COORD_CONFIG_HOME": str(root / "config"),
            "LLM_COORD_DATA_HOME": str(root / "data"),
            "LLM_COORD_STATE_HOME": str(root / "state"),
            "LLM_COORD_RUNTIME_DIR": str(root / "run"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        }
        self.paths = AppPaths.resolve("soak", environ=self.env, home=root / "home")
        self.paths.ensure()
        self.client = DaemonClient(self.paths)  # Real default five-second RPC cap.
        self.latencies: list[float] = []
        self.samples: list[dict[str, float | int]] = []
        self.restarts = 0
        self.process: subprocess.Popen[bytes] | None = None
        self.log = (root / "daemon-console.log").open("ab")
        self.daemon_script = root / "frozen-soak.py"
        self.daemon_script.write_bytes(Path(__file__).read_bytes())
        self.start()
        self.rpc("repo.add", path=str(self.repository))

    def rpc(self, method: str, **params: Any) -> Any:
        before = time.monotonic()
        try:
            return self.client.call(method, params, autostart=False)
        finally:
            self.latencies.append(1000 * (time.monotonic() - before))

    def start(self) -> None:
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(self.daemon_script),
                "--_daemon",
                str(self.root),
            ],
            env=self.env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )

        def available() -> bool:
            if self.process is None or self.process.poll() is not None:
                raise InvariantFailure("daemon exited during startup")
            try:
                self.rpc("system.ping")
            except LlmCoordError:
                return False
            return True

        wait(available)

    def crash_restart(self) -> None:
        assert self.process is not None
        self.process.kill()
        self.process.wait(timeout=10)
        self.restarts += 1
        self.start()

    def task_state(self, task_id: str) -> str:
        with sqlite3.connect(self.paths.control_db) as connection:
            row = connection.execute(
                "SELECT CASE WHEN w.status='awaiting_review' THEN w.status "
                "ELSE t.state END FROM tasks t LEFT JOIN task_workflows w "
                "USING(task_id) WHERE t.task_id=?",
                (task_id,),
            ).fetchone()
            return str(row[0]) if row else "missing"

    def settled(self, task_id: str, held: bool) -> None:
        def done() -> bool:
            state = self.task_state(task_id)
            if state in {"failed", "cancelled"}:
                with sqlite3.connect(self.paths.control_db) as connection:
                    events = connection.execute(
                        "SELECT event_type,payload_json FROM task_events "
                        "WHERE task_id=? "
                        "ORDER BY sequence DESC LIMIT 5",
                        (task_id,),
                    ).fetchall()
                raise InvariantFailure(f"task {task_id} {state}: {events}")
            return state in {"completed", "awaiting_review"}

        wait(done)
        expected = "awaiting_review" if held else "completed"
        if self.task_state(task_id) != expected:
            raise InvariantFailure(f"task {task_id} did not reach {expected}")

    def invariants(
        self, expected: Mapping[str, str | None], *, final: bool = False
    ) -> None:
        verify_files(self.repository, expected)
        checks = {
            "workspace_batches": "state IN ('applying','operator_attention')",
            "workspace_publications": (
                "operation_state IN ('prepared','operator_attention')"
            ),
            "isolated_sync_journal": "state='prepared'",
            "isolated_runtime_journal": "1",
            "authority_journals": "state <> 'confirmed'",
            "tasks": "state NOT IN ('completed','cancelled','failed')",
        }
        with sqlite3.connect(self.paths.control_db) as connection:
            for table, condition in checks.items():
                count = connection.execute(
                    f"SELECT count(*) FROM {table} WHERE {condition}"
                ).fetchone()[0]
                if count:
                    raise InvariantFailure(
                        f"unrecovered journal or stuck task: {table}={count}"
                    )
            if final:
                count = connection.execute(
                    "SELECT count(*) FROM workspaces "
                    "WHERE mode='isolated' AND state='active'"
                ).fetchone()[0]
                if count:
                    raise InvariantFailure(
                        f"active private workspaces left over: {count}"
                    )
        for marker_path in (self.root / "physical-authority").rglob("marker.json"):
            if json.loads(marker_path.read_text())["state"] != "clean":
                raise InvariantFailure("ownership marker is not clean")
        if final:
            if (
                len(
                    git(self.repository, "worktree", "list", "--porcelain").split(
                        "worktree "
                    )
                )
                != 2
            ):
                raise InvariantFailure("a private linked worktree remains")
            for name in (
                "worktrees",
                "check-worktrees",
                "runtime",
            ):
                directory = self.paths.data_dir / name
                if directory.exists() and any(
                    path.is_file() for path in directory.rglob("*")
                ):
                    raise InvariantFailure(f"private runtime files remain in {name}")
            # Closed workspaces can retain source-only recovery evidence. That
            # is distinct from a live private tree or a copied runtime value.
            private_root = self.paths.data_dir / "isolated-worktrees"
            for child in private_root.iterdir() if private_root.exists() else ():
                files = [path for path in child.rglob("*") if path.is_file()]
                if not child.name.startswith(".recovery-") and files:
                    raise InvariantFailure(
                        "private worktree or runtime generation remains"
                    )
                if child.name.startswith(".recovery-"):
                    owner = json.loads((child / "owner.json").read_text())
                    for relative in owner["private_paths"]:
                        if (child / "files" / relative).exists():
                            raise InvariantFailure(
                                "private runtime remains in recovery archive"
                            )
                    if any(
                        b"private-soak-runtime-value" in path.read_bytes()
                        for path in files
                    ):
                        raise InvariantFailure(
                            "private runtime value remains in recovery archive"
                        )

    def sample(self) -> None:
        assert self.process is not None
        pid = self.process.pid
        ps = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=True,
        )
        memory = int(ps.stdout.strip()) * 1024
        if sys.platform == "darwin":
            output = subprocess.run(
                ["ps", "-M", "-p", str(pid)], capture_output=True, text=True, check=True
            ).stdout
            threads = max(0, len(output.splitlines()) - 1)
            output = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-Ff"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            open_files = sum(
                line.startswith("f") and line[1:].isdigit()
                for line in output.splitlines()
            )
        else:
            threads = len(list(Path(f"/proc/{pid}/task").iterdir()))
            open_files = len(list(Path(f"/proc/{pid}/fd").iterdir()))
        sample: dict[str, float | int] = {
            "database_bytes": sum(
                path.stat().st_size for path in self.paths.data_dir.glob("*.sqlite3*")
            ),
            "candidate_bytes": sum(
                path.stat().st_size
                for path in self.paths.candidate_dir.rglob("*")
                if path.is_file()
            ),
            "open_files": open_files,
            "threads": threads,
            "memory_bytes": memory,
            "rpc_p99_ms": percentile(self.latencies, 0.99),
        }
        check_bounds(sample, self.bounds)
        self.samples.append(sample)

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log.close()


def run(options: argparse.Namespace) -> dict[str, Any]:
    root = Path(
        tempfile.mkdtemp(
            prefix="loupe-soak-",
            dir="/private/tmp" if sys.platform == "darwin" else None,
        )
    ).resolve()
    os.chmod(root, 0o700)
    bounds = Bounds()
    started = time.monotonic()
    record: dict[str, Any] = {
        "seed": options.seed,
        "requested_seconds": options.duration,
        "bounds": asdict(bounds),
        "status": "failed",
        "root": str(root),
        "started_at": datetime.now(UTC).isoformat(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_sha256": {
            str(path.relative_to(Path(__file__).resolve().parents[1])): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(
                (Path(__file__).resolve().parents[1] / "src/llm_cli").rglob("*.py")
            )
        },
        "python": sys.version,
        "platform": sys.platform,
    }
    cluster: Cluster | None = None
    stop = threading.Event()
    external_errors: list[str] = []
    expected: dict[str, str | None] = {}
    completed = 0
    last_restart = started
    rng = random.Random(options.seed)
    try:
        cluster = Cluster(root, bounds)
        credentials: dict[str, dict[str, str]] = {}
        # Open the long-lived pair before the outside writer starts. Opening a
        # new private checkout deliberately requires a stable creation snapshot;
        # this workload measures concurrent use of already-open conversations.
        for mode in ("shared", "isolated"):
            secret = "disposable-" + mode
            creds = {"session_id": "session-" + mode, "resume_secret": secret}
            opened = cluster.rpc(
                "session.open",
                path=str(cluster.repository),
                session_id=creds["session_id"],
                resume_token_hash=hashlib.sha256(secret.encode()).hexdigest(),
                provider=SoakProvider.name,
                model=mode,
                workspace=mode,
                **({"approved_runtime_files": [".env"]} if mode == "isolated" else {}),
            )
            cluster.rpc("session.ack", **creds, sequence=opened["bootstrap_sequence"])
            credentials[mode] = creds

        def outside_edits() -> None:
            number = 0
            while not stop.wait(0.7):
                try:
                    number += 1
                    (cluster.repository / "outside.txt").write_text(
                        f"outside-{number}\n"
                    )
                except OSError as exc:
                    external_errors.append(str(exc))
                    return

        editor = threading.Thread(
            target=outside_edits, name="soak-outside-editor", daemon=True
        )
        editor.start()
        while time.monotonic() - started < options.duration or completed == 0:
            before = time.monotonic()
            sessions: list[tuple[dict[str, str], str, str, bool]] = []
            for mode in ("shared", "isolated"):
                label = f"cycle-{completed}-{mode}"
                creds = credentials[mode]
                held = (completed + (mode == "isolated")) % 2 == 0
                relative = f"docs/{label}.txt"
                content = (
                    f"published seed {options.seed} cycle {completed} {mode} "
                    f"nonce {rng.randrange(1 << 32)}\n"
                )
                cluster.rpc(
                    "session.set_mode", **creds, mode="normal" if held else "auto"
                )
                cluster.rpc(
                    "task.run",
                    **creds,
                    path=str(cluster.repository),
                    task_id=label,
                    scopes=["docs/"],
                    title="SOAK "
                    + json.dumps(
                        {"label": label, "path": relative, "content": content}
                    ),
                )
                sessions.append((creds, label, content, held))
            for _, label, _, _ in sessions:
                wait(
                    lambda label=label: (root / "signals" / (label + ".ready")).exists()
                )
            # Ordinary commits and same-tree branch switches run while both
            # model executions hold private edits, never overwriting their paths.
            if completed % 5 == 0:
                git(cluster.repository, "add", "-A")
                git(
                    cluster.repository,
                    "commit",
                    "--allow-empty",
                    "-qm",
                    f"outside checkpoint {completed}",
                )
                branch = (
                    "peer"
                    if git(cluster.repository, "branch", "--show-current") == "main"
                    else "main"
                )
                git(cluster.repository, "branch", "-f", branch, "HEAD")
                git(cluster.repository, "switch", "-q", branch)
            if time.monotonic() - last_restart >= options.restart_interval:
                cluster.crash_restart()
                for creds in credentials.values():
                    cluster.rpc("session.resume", **creds)
                last_restart = time.monotonic()
                for _, label, _, _ in sessions:
                    wait(
                        lambda label=label: (
                            (root / "signals" / (label + ".ready")).read_text()
                            == str(cluster.process.pid)
                        )
                    )
            for _, label, _, _ in sessions:
                (root / "signals" / (label + ".release")).touch()
            for _, label, _, held in sessions:
                cluster.settled(label, held)
            for _, label, content, held in sessions:
                if held:
                    result = cluster.rpc("task.apply", task_id=label)
                    if result["state"] != "published":
                        raise InvariantFailure(f"apply did not publish: {result}")
                relative = f"docs/{label}.txt"
                expected[relative] = content
                verify_files(cluster.repository, expected)
                if options.inject_lost_write:
                    (cluster.repository / relative).unlink()
                    verify_files(cluster.repository, expected)
                if completed % 3 == 1:
                    result = cluster.rpc("task.undo", task_id=label)
                    if result["state"] != "published":
                        raise InvariantFailure(f"undo did not publish: {result}")
                    expected[relative] = None
            completed += 1
            cluster.invariants(expected)
            cluster.sample()
            if external_errors:
                raise InvariantFailure(
                    "outside edit worker failed: " + external_errors[0]
                )
            record.update(
                cycles=completed,
                restarts=cluster.restarts,
                elapsed_seconds=time.monotonic() - started,
            )
            options.output.parent.mkdir(parents=True, exist_ok=True)
            options.output.write_text(
                json.dumps({**record, "status": "running"}, indent=2) + "\n"
            )
            print(
                json.dumps(
                    {
                        "cycle": completed,
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                        "restarts": cluster.restarts,
                    }
                ),
                flush=True,
            )
            stop.wait(
                max(
                    0,
                    min(
                        options.cycle_interval - (time.monotonic() - before),
                        options.duration - (time.monotonic() - started),
                    ),
                )
            )
        stop.set()
        editor.join(timeout=5)
        for creds in credentials.values():
            cluster.rpc("session.close", **creds)
        cluster.invariants(expected, final=True)
        cluster.sample()
        record["status"] = "passed"
    except Exception as exc:
        record["failure"] = f"{type(exc).__name__}: {exc}"
    finally:
        stop.set()
        if cluster:
            record.update(
                restarts=cluster.restarts,
                samples=cluster.samples,
                rpc_latency_ms={
                    "count": len(cluster.latencies),
                    "p50": percentile(cluster.latencies, 0.5),
                    "p95": percentile(cluster.latencies, 0.95),
                    "p99": percentile(cluster.latencies, 0.99),
                    "max": max(cluster.latencies, default=0),
                },
            )
            cluster.close()
        record.update(
            cycles=completed,
            elapsed_seconds=time.monotonic() - started,
            finished_at=datetime.now(UTC).isoformat(),
        )
        if record["status"] == "passed" and not options.keep:
            shutil.rmtree(root)
            record["disposable_root_removed"] = not root.exists()
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(record, indent=2) + "\n")
    return record


def main() -> int:
    if sys.argv[1:2] == ["--_daemon"]:
        daemon_entry(Path(sys.argv[2]))
        return 0
    options = parser().parse_args()
    result = run(options)
    print(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key not in {"samples", "bounds", "source_sha256"}
            },
            indent=2,
        )
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
