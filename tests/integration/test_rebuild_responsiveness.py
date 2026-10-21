"""A private installer must not hold the daemon or publication barriers."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

import pytest
from authority_helpers import __file__ as authority_bootstrap

from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.execution.sandbox import available_sandbox
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient
from llm_cli.protocol.envelopes import Request

pytestmark = pytest.mark.skipif(
    available_sandbox() is None, reason="real command sandbox required"
)

_PYTHON = str(Path(sys.executable).resolve())
_INSTALL = """from pathlib import Path
import time
root = Path('.venv')
(root / 'started').write_text('installer is waiting')
while not (root / 'release').exists():
    time.sleep(.02)
(root / 'ready').write_text('rebuilt dependency')
"""


@contextlib.contextmanager
def _daemon(tmp_path: Path) -> Iterator[tuple[AppPaths, subprocess.Popen[bytes]]]:
    with TemporaryDirectory(prefix="lc-rebuild-rpc-", dir="/tmp") as runtime:
        environment = {
            **os.environ,
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(Path(runtime).resolve()),
            "PYTHONUNBUFFERED": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        paths = AppPaths.resolve("rebuild-responsive", environ=environment)
        paths.config_file.parent.mkdir(parents=True)
        paths.config_file.write_text(
            '[isolated.rebuild]\n".venv" = { argv = '
            + json.dumps([_PYTHON, "install.py"])
            + ", timeout_seconds = 45 }\n"
        )
        log = tmp_path / "daemon-output.log"
        with log.open("wb") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(authority_bootstrap),
                    "--profile",
                    paths.profile_id,
                ],
                cwd=tmp_path,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            deadline = time.monotonic() + 30
            while not paths.socket.exists():
                assert process.poll() is None, log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.02)
            yield paths, process
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def _private_path(paths: AppPaths, session_id: str) -> Path | None:
    with contextlib.closing(
        sqlite3.connect(paths.control_db.as_uri() + "?mode=ro", uri=True)
    ) as connection:
        row = connection.execute(
            "SELECT w.canonical_path FROM workspaces w "
            "JOIN isolated_workspaces i USING(workspace_id) WHERE i.session_id=?",
            (session_id,),
        ).fetchone()
    return Path(row[0]) if row is not None else None


def _open(client: DaemonClient, repository: Path, name: str, mode: str) -> Any:
    return client.call(
        "session.open",
        {
            "path": str(repository),
            "session_id": name,
            "resume_token_hash": hashlib.sha256(name.encode()).hexdigest(),
            "workspace": mode,
        },
        autostart=False,
    )


@pytest.mark.parametrize(
    "probe", ["repo.list", "workspace.publish_candidate", "duplicate-open"]
)
def test_real_rpc_and_publication_progress_while_private_installer_waits(
    tmp_path: Path, repository_factory: Callable[..., Path], probe: str
) -> None:
    repository = repository_factory(
        tmp_path,
        {
            "a.txt": "original source\n",
            ".gitignore": ".venv/\n",
            "install.py": _INSTALL,
        },
    )
    with _daemon(tmp_path) as (paths, process):
        setup = DaemonClient(paths, timeout_seconds=60)
        setup.call("repo.add", {"path": str(repository)}, autostart=False)
        opened = _open(setup, repository, "shared-publisher", "shared")
        credentials = {
            "session_id": "shared-publisher",
            "resume_secret": "shared-publisher",
        }
        setup.call(
            "session.ack",
            {**credentials, "sequence": opened["bootstrap_sequence"]},
            autostart=False,
        )
        read = setup.call(
            "workspace.read_file",
            {**credentials, "relative_path": "a.txt"},
            autostart=False,
        )
        setup.call(
            "workspace.stage_file",
            {
                **credentials,
                "candidate_id": "publish-during-rebuild",
                "relative_path": "a.txt",
                "base": read["identity"],
                "content": "published while installer waits\n",
            },
            autostart=False,
        )
        # The barrier lives inside the ignored dependency root, so the
        # production sandbox allows it without additional writable paths.
        private = None
        with ThreadPoolExecutor(max_workers=1) as pool:
            opening = pool.submit(_open, setup, repository, "slow-rebuild", "isolated")
            try:
                deadline = time.monotonic() + 30
                while True:
                    private = _private_path(paths, "slow-rebuild")
                    if private is not None and (private / ".venv/started").exists():
                        break
                    assert process.poll() is None
                    assert not opening.done(), opening.result()
                    assert time.monotonic() < deadline, (
                        "installer never reached barrier"
                    )
                    time.sleep(0.02)
                # Use the ordinary five-second RPC budget. The installer cannot
                # complete until the finally block releases it; a serialized
                # request therefore deterministically times out on the old code.
                ordinary = DaemonClient(paths)
                assert ordinary.timeout_seconds == 5
                params = (
                    {**credentials, "candidate_id": "publish-during-rebuild"}
                    if probe == "workspace.publish_candidate"
                    else {}
                )
                if probe == "duplicate-open":
                    with pytest.raises(
                        LlmCoordError, match="setup is already running"
                    ) as refusal:
                        _open(ordinary, repository, "slow-rebuild", "isolated")
                    assert refusal.value.code is ErrorCode.REPOSITORY_UNSAFE
                    # A duplicate must leave the original owner usable and
                    # must not turn a live installer into a recovery problem.
                    result = ordinary.call("repo.list", autostart=False)
                    status = ordinary.call(
                        "workspace.status", {"path": str(repository)}, autostart=False
                    )
                    assert status["authority"]["state"] == "owned"
                    with contextlib.closing(
                        sqlite3.connect(paths.control_db)
                    ) as connection:
                        rows = connection.execute(
                            "SELECT i.stage,w.state FROM isolated_workspaces i "
                            "JOIN workspaces w USING(workspace_id) "
                            "WHERE i.session_id=?",
                            ("slow-rebuild",),
                        ).fetchall()
                        assert len(rows) == 1
                        assert rows[0][0] == "rebuilding"
                        assert rows[0][1] != "quarantined"
                        assert (
                            connection.execute(
                                "SELECT count(*) FROM authority_journals "
                                "WHERE state!='confirmed'"
                            ).fetchone()[0]
                            == 0
                        )
                    listed = subprocess.run(
                        [
                            "git",
                            "-C",
                            str(repository),
                            "worktree",
                            "list",
                            "--porcelain",
                        ],
                        text=True,
                        capture_output=True,
                        check=True,
                        timeout=5,
                    ).stdout
                    assert (
                        len(
                            [
                                line
                                for line in listed.splitlines()
                                if line.startswith("worktree ")
                            ]
                        )
                        == 2
                    )
                else:
                    result = ordinary.call(probe, params, autostart=False)
                assert not opening.done()
                assert not (private / ".venv/ready").exists()
                if probe != "workspace.publish_candidate":
                    assert len(result) == 1
                else:
                    assert result["outcome"] == "published"
                    assert (repository / "a.txt").read_text() == (
                        "published while installer waits\n"
                    )
            finally:
                if private is not None:
                    with contextlib.suppress(FileNotFoundError):
                        (private / ".venv/release").touch()
                # Keep ownership until the real session.open has completed;
                # teardown must not race a still-running installer thread.
                opened_isolated = opening.result(timeout=60)
        assert opened_isolated["workspace_notices"] == [
            "Dependency rebuilt privately: .venv"
        ]
        private = Path(opened_isolated["workspace"]["canonical_path"])
        assert (private / ".venv/ready").read_text() == "rebuilt dependency"
        assert (private / "a.txt").read_text() == "original source\n"
        with contextlib.closing(sqlite3.connect(paths.control_db)) as connection:
            assert (
                connection.execute(
                    "SELECT stage FROM isolated_workspaces WHERE session_id=?",
                    ("slow-rebuild",),
                ).fetchone()[0]
                == "ready"
            )
        assert not (
            Path(opened_isolated["workspace"]["git_dir"]) / "loupe-rebuild-home"
        ).exists()


def test_pending_open_keeps_checkout_binding_until_daemon_registration(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[[Path], DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        first_repository = repository_factory(
            tmp_path / "first", {"a.txt": "first checkout source\n"}
        )
        second_repository = repository_factory(
            tmp_path / "second", {"b.txt": "second checkout source\n"}
        )
        service = service_factory(tmp_path)
        service.providers.register(
            "scripted", lambda model: SimpleNamespace(name="scripted", model="fake")
        )
        service.initialize()
        manager_open = service.isolated_workspaces.open
        ready = threading.Event()
        release = threading.Event()
        attempts = 0
        attempt_lock = threading.Lock()

        def delayed_return(*args: Any, **kwargs: Any) -> Any:
            nonlocal attempts
            with attempt_lock:
                attempts += 1
                first = attempts == 1
            result = manager_open(*args, **kwargs)
            if first:
                # Model a scheduling pause after the manager's own opening
                # guard ends, before the daemon records the durable session.
                ready.set()
                assert release.wait(30), "original opening was never released"
            return result

        def request(method: str, params: dict[str, Any]) -> Request:
            return Request.create(
                request_id=method, method=method, params=params, profile_id="test"
            )

        def opening(repository: Path, token: str) -> Request:
            return request(
                "session.open",
                {
                    "path": str(repository),
                    "session_id": "pending-registration",
                    "resume_token_hash": hashlib.sha256(token.encode()).hexdigest(),
                    "workspace": "isolated",
                    "provider": "scripted",
                    "model": "fake",
                },
            )

        original: asyncio.Task[Any] | None = None
        try:
            for repository in (first_repository, second_repository):
                await service.handle(request("repo.add", {"path": str(repository)}))
            monkeypatch.setattr(service.isolated_workspaces, "open", delayed_return)
            original = asyncio.create_task(
                service.handle(opening(first_repository, "original-opening-token"))
            )
            try:
                assert await asyncio.to_thread(ready.wait, 10), (
                    "original manager never reached ready"
                )
                with service.store.connection() as connection:
                    assert (
                        connection.execute("SELECT count(*) FROM sessions").fetchone()[
                            0
                        ]
                        == 0
                    )
                with pytest.raises(
                    LlmCoordError, match="setup is already running"
                ) as refusal:
                    await asyncio.wait_for(
                        service.handle(
                            opening(second_repository, "conflicting-opening-token")
                        ),
                        timeout=5,
                    )
                assert refusal.value.code is ErrorCode.REPOSITORY_UNSAFE
                assert not original.done()
                with service.store.connection() as connection:
                    assert (
                        connection.execute("SELECT count(*) FROM sessions").fetchone()[
                            0
                        ]
                        == 0
                    )
                    assert (
                        connection.execute(
                            "SELECT count(*) FROM isolated_workspaces"
                        ).fetchone()[0]
                        == 1
                    )
            finally:
                release.set()
                # Always let the real manager/registration finish before
                # closing authority, including on the deliberately failing code.
                outcomes = await asyncio.wait_for(
                    asyncio.gather(original, return_exceptions=True), timeout=30
                )
            assert not isinstance(outcomes[0], BaseException), outcomes[0]
            assert outcomes[0]["session"]["session_id"] == "pending-registration"
            with service.store.connection() as connection:
                row = connection.execute(
                    "SELECT s.checkout_id,w.checkout_id AS workspace_checkout,"
                    "s.resume_token_hash,w.canonical_path,"
                    "c.canonical_path AS primary_path "
                    "FROM sessions s JOIN workspaces w USING(workspace_id) "
                    "JOIN checkouts c ON c.checkout_id=s.checkout_id "
                    "WHERE s.session_id=?",
                    ("pending-registration",),
                ).fetchone()
                assert row is not None
                assert row["checkout_id"] == row["workspace_checkout"]
                assert row["primary_path"] == str(first_repository)
                assert (
                    row["resume_token_hash"]
                    == hashlib.sha256(b"original-opening-token").hexdigest()
                )
                assert (
                    connection.execute("SELECT count(*) FROM sessions").fetchone()[0]
                    == 1
                )
            assert (Path(row["canonical_path"]) / "a.txt").read_text() == (
                "first checkout source\n"
            )
            assert not (Path(row["canonical_path"]) / "b.txt").exists()
        finally:
            release.set()
            if original is not None and not original.done():
                await asyncio.gather(original, return_exceptions=True)
            service.close()

    asyncio.run(scenario())


def test_manager_rejects_reused_session_from_another_checkout_before_mutation(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repositories = (
            repository_factory(tmp_path / "first", {"a.txt": "first checkout\n"}),
            repository_factory(tmp_path / "second", {"b.txt": "second checkout\n"}),
        )
        service = service_factory(tmp_path)
        service.initialize()
        try:
            checkouts = []
            for repository in repositories:
                added = await service.handle(
                    Request.create(
                        request_id="add-" + repository.parent.name,
                        method="repo.add",
                        params={"path": str(repository)},
                        profile_id="test",
                    )
                )
                checkout = service.store.get_checkout(added["checkout"]["checkout_id"])
                assert checkout is not None
                checkouts.append(checkout)
            manager = service.isolated_workspaces
            workspace, _ = await asyncio.to_thread(
                manager.open, checkouts[0], "manager-checkout-binding"
            )
            with service.store.connection() as connection:
                before = [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT * FROM authority_journals ORDER BY rowid"
                    )
                ]
            with pytest.raises(LlmCoordError, match="checkout") as refusal:
                await asyncio.to_thread(
                    manager.open, checkouts[1], "manager-checkout-binding"
                )
            assert refusal.value.code is ErrorCode.REPOSITORY_UNSAFE
            assert manager.validate(workspace.workspace_id).stage == "ready"
            with service.store.connection() as connection:
                assert before == [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT * FROM authority_journals ORDER BY rowid"
                    )
                ]
                assert (
                    connection.execute(
                        "SELECT count(*) FROM authority_journals "
                        "WHERE state!='confirmed'"
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM isolated_workspaces"
                    ).fetchone()[0]
                    == 1
                )
            with service.physical_authority.pin(repositories[1]):
                service.physical_authority.validate_current()
        finally:
            service.close()

    asyncio.run(scenario())
