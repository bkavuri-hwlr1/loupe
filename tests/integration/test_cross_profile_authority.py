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

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient


def _wait(predicate: Callable[[], Any], *, timeout: float = 45) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("cross-profile reproduction did not reach its barrier")


def _daemon_entry() -> None:
    from authority_helpers import install_test_authority

    install_test_authority()
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
    credentials, candidate = _stage(first, repository)
    second.call("repo.add", path=str(repository))
    with pytest.raises(LlmCoordError) as refusal:
        second.call(
            "session.open",
            session_id=second.name,
            path=str(repository),
            workspace="shared",
            resume_token_hash=hashlib.sha256(b"peer-secret").hexdigest(),
        )
    assert refusal.value.code is ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE
    assert refusal.value.details is not None
    assert refusal.value.details["owner_profile"] == first.name
    assert refusal.value.details["process_start"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        publication = pool.submit(
            first.call,
            "workspace.publish_candidate",
            **credentials,
            candidate_id=candidate,
        )
        try:
            evidence = json.loads(
                _wait(
                    lambda: (
                        (signals / f"{first.name}.ready").read_text()
                        if (signals / f"{first.name}.ready").exists()
                        else None
                    )
                )
            )
            assert evidence["observed"] == "same starting version\n"
            assert evidence["pid"] == first.process.pid
            assert _journal_state(first, candidate) == "prepared"
            observed = second.call(
                "workspace.observe", path=str(repository), relative_path="shared.txt"
            )
            assert observed["content"] == "same starting version\n"
            assert observed["cooperative_read_barrier"] is False
            assert observed["edit_capability"] is False
            assert observed["authority"]["state"] == "foreign_recovery"
            canary = "private-runtime-canary-78124"
            (repository / ".env").write_text("DISPLAY_NAME=" + canary)
            cli_environment = {
                **os.environ,
                "LLM_COORD_CONFIG_HOME": str(second.paths.config_dir.parent),
                "LLM_COORD_DATA_HOME": str(second.paths.data_dir.parents[2]),
                "LLM_COORD_STATE_HOME": str(second.paths.state_dir.parents[2]),
                "LLM_COORD_RUNTIME_DIR": str(second.paths.runtime_dir.parents[1]),
                "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
            }
            command = [
                sys.executable,
                "-m",
                "llm_cli",
                "--profile",
                second.name,
                "--json",
                "workspace",
                "observe",
            ]
            cli = subprocess.run(
                [*command, "shared.txt", "--repo", str(repository)],
                capture_output=True,
                text=True,
                check=False,
                env=cli_environment,
                timeout=10,
            )
            assert cli.returncode == 0, cli.stderr + cli.stdout
            cli_observed = json.loads(cli.stdout)
            assert cli_observed["edit_capability"] is False
            assert cli_observed["cooperative_read_barrier"] is False
            assert cli_observed["authority"]["owner_profile"] == first.name
            protected = subprocess.run(
                [*command, ".env", "--repo", str(repository)],
                capture_output=True,
                text=True,
                check=False,
                env=cli_environment,
                timeout=10,
            )
            assert protected.returncode == 65
            assert json.loads(protected.stdout)["error"]["code"] == "REPOSITORY_UNSAFE"
            assert (
                canary
                not in protected.stdout + protected.stderr + cli.stdout + cli.stderr
            )
            with pytest.raises(LlmCoordError) as dirty_refusal:
                second.call(
                    "session.open",
                    session_id=second.name,
                    path=str(repository),
                    workspace="shared",
                    resume_token_hash=hashlib.sha256(b"peer-secret").hexdigest(),
                )
            assert (
                dirty_refusal.value.code is ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
            )
            assert second.process.poll() is None
        finally:
            (signals / f"{first.name}.release").touch()
        assert publication.result(timeout=60)["outcome"] == "published"
    assert _journal_state(first, candidate) == "confirmed"
    assert (repository / "shared.txt").read_text() == f"published by {first.name}\n"
    with sqlite3.connect(second.paths.control_db) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workspace_read_observations"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM workspace_candidates").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workspace_publications"
            ).fetchone()[0]
            == 0
        )
    assert (
        git_run(repository, "rev-parse", "HEAD"),
        git_run(repository, "write-tree"),
    ) == before


def test_clean_idle_handoff_rechecks_retained_candidate_bases(
    profiles: tuple[Path, Path, list[_Daemon]],
) -> None:
    repository, signals, daemons = profiles
    first, second = daemons
    credentials, candidate = _stage(first, repository)
    retained_base = _candidate_base(first, candidate)
    from llm_cli.storage.control import ControlStore
    from llm_cli.workspace.context import SharedCoordinationContext

    context = SharedCoordinationContext(
        ControlStore(first.paths.control_db),
        first.name,
        ("shared.txt",),
        scan_external=False,
    )
    previous_context = context(None)
    second.call("repo.add", path=str(repository))

    def acquire_after_idle() -> dict[str, Any] | None:
        try:
            return second.call(
                "session.open",
                session_id=second.name,
                path=str(repository),
                workspace="shared",
                resume_token_hash=hashlib.sha256(b"second-resume").hexdigest(),
            )
        except LlmCoordError as exc:
            assert exc.code is ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE
            return None

    started = time.monotonic()
    opened = _wait(acquire_after_idle, timeout=45)
    assert time.monotonic() - started >= 29
    assert first.process.poll() is None
    peer_credentials = {"session_id": second.name, "resume_secret": "second-resume"}
    second.call(
        "session.ack", **peer_credentials, sequence=opened["bootstrap_sequence"]
    )
    read = second.call(
        "workspace.read_file", **peer_credentials, relative_path="shared.txt"
    )
    assert read["content"] == "same starting version\n"
    staged = second.call(
        "workspace.stage_file",
        **peer_credentials,
        candidate_id="after-handoff",
        relative_path="shared.txt",
        base=read["identity"],
        content="new owner result\n",
    )
    (signals / f"{second.name}.release").touch()
    assert (
        second.call(
            "workspace.publish_candidate",
            **peer_credentials,
            candidate_id=staged["candidate"]["candidate_id"],
        )["outcome"]
        == "published"
    )
    assert _candidate_base(first, candidate) == retained_base
    with sqlite3.connect(second.paths.control_db) as connection:
        notice = connection.execute(
            "SELECT payload_json FROM checkout_events WHERE "
            "event_type='checkout.authority_handoff'"
        ).fetchone()
        assert notice is not None and json.loads(notice[0])["history_gap"] is True
    second.call("system.shutdown")
    second.process.wait(timeout=15)
    result = first.call(
        "workspace.publish_candidate", **credentials, candidate_id=candidate
    )
    assert result["outcome"] == "diverged"
    refreshed = context(previous_context.sequence)
    assert refreshed.text is not None
    assert "ownership_history_gap" in json.loads(refreshed.text)
    assert (repository / "shared.txt").read_text() == "new owner result\n"


def test_crashed_owner_dirty_journal_is_recovered_only_by_original_profile(
    profiles: tuple[Path, Path, list[_Daemon]],
    tmp_path: Path,
) -> None:
    repository, signals, daemons = profiles
    first, second = daemons
    credentials, candidate = _stage(first, repository)
    second.call("repo.add", path=str(repository))
    with ThreadPoolExecutor(max_workers=1) as pool:
        publication = pool.submit(
            first.call,
            "workspace.publish_candidate",
            **credentials,
            candidate_id=candidate,
        )
        _wait(lambda: (signals / f"{first.name}.ready").exists())
        assert _journal_state(first, candidate) == "prepared"
        first.process.kill()
        first.process.wait(timeout=10)
        with pytest.raises(LlmCoordError):
            publication.result(timeout=10)
    with pytest.raises(LlmCoordError) as refused:
        second.call(
            "session.open",
            session_id=second.name,
            path=str(repository),
            workspace="shared",
            resume_token_hash=hashlib.sha256(b"peer-after-crash").hexdigest(),
        )
    assert refused.value.code is ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
    assert (repository / "shared.txt").read_text() == "same starting version\n"
    (signals / f"{first.name}.release").touch()
    environment = {
        **os.environ,
        "LLM_COORD_CONFIG_HOME": str(first.paths.config_dir.parent),
        "LLM_COORD_DATA_HOME": str(first.paths.data_dir.parents[2]),
        "LLM_COORD_STATE_HOME": str(first.paths.state_dir.parents[2]),
        "LLM_COORD_RUNTIME_DIR": str(first.paths.runtime_dir.parents[1]),
        "AUTHORITY_REPRO_PROFILE": first.name,
        "AUTHORITY_REPRO_REPOSITORY": str(repository),
        "AUTHORITY_REPRO_SIGNALS": str(signals),
        "PYTHONPATH": os.pathsep.join(
            (str(Path(__file__).parent), str(Path(__file__).parents[2] / "src"))
        ),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    with first.log.open("ab") as output:
        first.process = subprocess.Popen(
            first.process.args,
            cwd=tmp_path,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    _wait(first.ready)
    assert _journal_state(first, candidate) == "rolled_back"
    first.call("session.resume", **credentials)
    assert (
        first.call(
            "workspace.publish_candidate", **credentials, candidate_id=candidate
        )["outcome"]
        == "published"
    )
    assert (repository / "shared.txt").read_text() == f"published by {first.name}\n"
