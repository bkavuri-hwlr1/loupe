"""A deterministic two-daemon witness for ADR 0010's missing authority gate.

Only scheduling immediately before the real atomic rename is instrumented.
The daemons, profile databases, sockets, session bootstrap, candidate staging,
publication journals, filesystem replacement and confirmations are production.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient


class _CrossProfilePublicationViolation(AssertionError):
    """Raised only after the test has proved the specific missing guarantee."""


def _wait(predicate: Callable[[], Any], *, timeout: float = 45) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("cross-profile reproduction did not reach its barrier")


def _daemon_entry() -> None:
    """Run the ordinary daemon, gating its final target-file rename for this test."""
    from llm_cli.daemon.main import main

    profile = os.environ["AUTHORITY_REPRO_PROFILE"]
    root = Path(os.environ["AUTHORITY_REPRO_REPOSITORY"])
    signals = Path(os.environ["AUTHORITY_REPRO_SIGNALS"])
    replace = os.replace
    seen = False

    def gated_replace(source: Any, destination: Any, **kwargs: Any) -> None:
        nonlocal seen
        parent = kwargs.get("dst_dir_fd")
        if not seen and destination == "shared.txt" and parent is not None:
            opened = os.fstat(parent)
            expected = root.stat()
            assert (opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino)
            assert Path(source).parent == root / ".git" / "llm-coord-stage"
            seen = True
            (signals / f"{profile}.ready").write_text(
                json.dumps(
                    {
                        "profile": profile,
                        "pid": os.getpid(),
                        "observed": (root / "shared.txt").read_text(),
                    }
                )
            )
            _wait(lambda: (signals / f"{profile}.release").exists())
        replace(source, destination, **kwargs)

    os.replace = gated_replace
    main(["--profile", profile])


@dataclass
class _Daemon:
    name: str
    process: subprocess.Popen[bytes]
    client: DaemonClient
    paths: AppPaths
    log: Path

    def call(self, method: str, **params: Any) -> Any:
        return self.client.call(method, params, autostart=False)

    def ready(self) -> bool:
        assert self.process.poll() is None, self.log.read_text()
        try:
            return bool(self.call("system.ping"))
        except LlmCoordError:
            return False


@pytest.fixture
def profiles(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> Iterator[tuple[Path, Path, list[_Daemon]]]:
    repository = repository_factory(tmp_path, {"shared.txt": "same starting version\n"})
    signals = tmp_path / "signals"
    signals.mkdir()
    daemons: list[_Daemon] = []
    with TemporaryDirectory(prefix="lc-authority-", dir="/tmp") as runtime:
        environment = {
            **os.environ,
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(Path(runtime).resolve()),
            "AUTHORITY_REPRO_REPOSITORY": str(repository),
            "AUTHORITY_REPRO_SIGNALS": str(signals),
            "PYTHONPATH": os.pathsep.join(
                (str(Path(__file__).parent), str(Path(__file__).parents[2] / "src"))
            ),
            "PYTHONUNBUFFERED": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        try:
            for name in ("authority-a", "authority-b"):
                log = tmp_path / f"{name}.log"
                with log.open("wb") as output:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            "from test_cross_profile_authority import _daemon_entry; "
                            "_daemon_entry()",
                        ],
                        cwd=tmp_path,
                        env={**environment, "AUTHORITY_REPRO_PROFILE": name},
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                    )
                paths = AppPaths.resolve(name, environ=environment)
                daemon = _Daemon(
                    name, process, DaemonClient(paths, timeout_seconds=60), paths, log
                )
                daemons.append(daemon)
                _wait(daemon.ready)
            yield repository, signals, daemons
        finally:
            for daemon in daemons:
                (signals / f"{daemon.name}.release").touch()
                if daemon.process.poll() is None:
                    daemon.process.terminate()
                try:
                    daemon.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    daemon.process.kill()
                    daemon.process.wait(timeout=10)


def _stage(daemon: _Daemon, repository: Path) -> tuple[dict[str, str], str]:
    daemon.call("repo.add", path=str(repository))
    secret = f"resume-{daemon.name}"
    credentials = {"session_id": daemon.name, "resume_secret": secret}
    opened = daemon.call(
        "session.open",
        session_id=daemon.name,
        path=str(repository),
        workspace="shared",
        resume_token_hash=hashlib.sha256(secret.encode()).hexdigest(),
    )
    daemon.call("session.ack", **credentials, sequence=opened["bootstrap_sequence"])
    read = daemon.call("workspace.read_file", **credentials, relative_path="shared.txt")
    assert read["content"] == "same starting version\n"
    candidate = daemon.call(
        "workspace.stage_file",
        **credentials,
        candidate_id=f"candidate-{daemon.name}",
        relative_path="shared.txt",
        base=read["identity"],
        content=f"published by {daemon.name}\n",
    )["candidate"]
    return credentials, candidate["candidate_id"]


def _journal_state(daemon: _Daemon, candidate: str) -> str:
    assert daemon.paths.control_db.is_file()
    with contextlib.closing(
        sqlite3.connect(f"file:{daemon.paths.control_db}?mode=ro", uri=True)
    ) as connection:
        row = connection.execute(
            "SELECT operation_state FROM workspace_publications WHERE candidate_id=?",
            (candidate,),
        ).fetchone()
    assert row is not None, "publication never prepared its production journal"
    return str(row[0])


def _candidate_base(daemon: _Daemon, candidate: str) -> tuple[str, str, str]:
    with contextlib.closing(
        sqlite3.connect(f"file:{daemon.paths.control_db}?mode=ro", uri=True)
    ) as connection:
        row = connection.execute(
            "SELECT base_kind,base_mode,base_digest FROM workspace_candidates "
            "WHERE candidate_id=?",
            (candidate,),
        ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1]), str(row[2])


@pytest.mark.xfail(
    strict=True,
    raises=_CrossProfilePublicationViolation,
    reason=(
        "G1/ADR 0010: profile-local barriers permit two same-base publications; "
        "G2 fixes this"
    ),
)
def test_two_profiles_cannot_confirm_overlapping_same_base_publications(
    profiles: tuple[Path, Path, list[_Daemon]], git_run: Callable[..., str]
) -> None:
    repository, signals, daemons = profiles
    first, second = daemons
    assert first.process.pid != second.process.pid
    assert first.paths.control_db != second.paths.control_db
    assert first.paths.socket != second.paths.socket
    before = (
        git_run(repository, "rev-parse", "HEAD"),
        git_run(repository, "write-tree"),
    )
    staged = [_stage(daemon, repository) for daemon in daemons]
    assert _candidate_base(first, staged[0][1]) == _candidate_base(second, staged[1][1])
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                daemon.call,
                "workspace.publish_candidate",
                **credentials,
                candidate_id=candidate,
            )
            for daemon, (credentials, candidate) in zip(daemons, staged, strict=True)
        ]
        try:
            evidence = [
                json.loads(
                    _wait(
                        lambda path=signals / f"{daemon.name}.ready": (
                            path.read_text() if path.exists() else None
                        )
                    )
                )
                for daemon in daemons
            ]
            assert all(
                item["observed"] == "same starting version\n" for item in evidence
            )
            assert {item["pid"] for item in evidence} == {
                daemon.process.pid for daemon in daemons
            }
            assert all(
                _journal_state(daemon, candidate) == "prepared"
                for daemon, (_, candidate) in zip(daemons, staged, strict=True)
            )
            (signals / f"{first.name}.release").touch()
            first_result = futures[0].result(timeout=60)
            assert first_result["outcome"] == "published", first_result
            assert (
                repository / "shared.txt"
            ).read_text() == f"published by {first.name}\n"
            (signals / f"{second.name}.release").touch()
            second_result = futures[1].result(timeout=60)
            assert second_result["outcome"] == "published", second_result
        finally:
            for daemon in daemons:
                (signals / f"{daemon.name}.release").touch()
    assert all(
        _journal_state(daemon, candidate) == "confirmed"
        for daemon, (_, candidate) in zip(daemons, staged, strict=True)
    )
    assert (repository / "shared.txt").read_text() == f"published by {second.name}\n"
    assert (
        git_run(repository, "rev-parse", "HEAD"),
        git_run(repository, "write-tree"),
    ) == before
    raise _CrossProfilePublicationViolation(
        "Two independent profile daemons confirmed different results from one base; "
        "the second rename overwrote the first while both journals claim success."
    )
