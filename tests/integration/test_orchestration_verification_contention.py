"""Exercise check and manual-publication races between shared sessions.

Model decisions are scripted, but check commands run in real subprocesses and
manual actions run on the daemon's normal worker threads against real Git repos.
External barriers make the interesting interleavings explicit and repeatable.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
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

from llm_cli.coordination.models import ClaimState
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import LlmCoordError


def _editor(name: str, changes: dict[str, str]) -> ScriptedProvider:
    return ScriptedProvider(
        name,
        [
            _tools(
                *(_call("read_file", path=path) for path in changes),
                *(
                    _call("write_file", path=path, content=content)
                    for path, content in changes.items()
                ),
            ),
            _finish(),
        ],
    )


async def _sessions(
    service: DaemonService,
    root: Path,
    *providers: ScriptedProvider,
    review: bool = False,
) -> list[dict[str, str]]:
    _register(service, *providers)
    service.initialize()
    await service.handle(_request("repo.add", {"path": str(root)}))
    sessions = [await _open_session(service, root, p) for p in providers]
    if review:
        for credentials in sessions:
            await service.handle(
                _request("session.set_mode", {**credentials, "mode": "normal"})
            )
    return sessions


async def _configure(service: DaemonService, root: Path, code: str) -> None:
    await service.handle(
        _request(
            "checks.configure",
            {
                "path": str(root),
                "config": {
                    "checks": {
                        "test": {
                            "argv": [sys.executable, "-c", code],
                            "timeout": 20,
                        }
                    }
                },
            },
        )
    )


async def _wait_for(path: Path) -> None:
    async with asyncio.timeout(10):
        # A created marker may still be empty while write_text is in progress.
        while not path.exists() or not path.read_text().endswith("\n"):
            await asyncio.sleep(0.01)


async def _drain(tasks: list[asyncio.Task[None]]) -> None:
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 25)


def _revision(service: DaemonService, session: dict[str, str]) -> int:
    saved = service.store.get_session(session["session_id"])
    assert saved is not None
    workspace = service.store.get_workspace(str(saved.workspace_id))
    assert workspace is not None
    return workspace.workspace_revision


def _assert_released(service: DaemonService, task_id: str) -> None:
    task = service.store.get_task(task_id)
    assert task is not None
    claim = service.store.get_claim(str(task.current_claim_id))
    assert claim is not None and claim.state is ClaimState.RELEASED


@pytest.mark.parametrize("overlapping", [False, True], ids=["disjoint", "overlapping"])
def test_peer_publish_during_required_check_invalidates_evidence(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    git_run: Callable[..., str],
    overlapping: bool,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "base a\n", "b.py": "base b\n"})
        service = service_factory(tmp_path)
        started, release = tmp_path / "started", tmp_path / "release"
        first = _editor("first", {"a.py": "first\n"})
        peer_path = "a.py" if overlapping else "b.py"
        peer = _editor("peer", {peer_path: "peer\n"})
        sessions = await _sessions(service, root, first, peer)
        before = git_run(root, "show-ref"), git_run(root, "write-tree")
        # Only the first task's private candidate pauses; the peer can verify
        # and publish while the first check is inspecting its original snapshot.
        await _configure(
            service,
            root,
            "from pathlib import Path\nimport time\n"
            "if Path('a.py').read_text() == 'first\\n':\n"
            f"    Path({str(started)!r}).write_text(str(Path.cwd()) + '\\n')\n"
            f"    while not Path({str(release)!r}).exists(): time.sleep(.01)\n"
            "    assert Path('b.py').read_text() == 'base b\\n'\n",
        )
        tasks: list[asyncio.Task[None]] = []
        try:
            tasks.append(await _start(service, root, sessions[0], "first", ("*",)))
            await _wait_for(started)
            snapshot = Path(started.read_text().removesuffix("\n"))
            assert snapshot != root and snapshot.exists()
            tasks.append(await _start(service, root, sessions[1], "peer", ("*",)))
            await asyncio.wait_for(tasks[1], 10)
            _assert_completed(service, "peer")
            assert service.workflow.checks("first")[0]["state"] == "running"
            assert (root / peer_path).read_text() == "peer\n"
            release.touch()
            await asyncio.wait_for(tasks[0], 10)
            # Passing a check of the old checkout is insufficient to publish.
            assert service.workflow.checks("first")[0]["state"] == "passed"
            assert service._task_view("first")["state"] == "awaiting_review"
            _assert_released(service, "first")
            assert service.workflow.inspect("first")["paths"] == ["a.py"]
            with pytest.raises(LlmCoordError, match="stale"):
                await service.handle(_request("task.apply", {"task_id": "first"}))
            assert (root / "a.py").read_text() == (
                "peer\n" if overlapping else "base a\n"
            )
            assert _revision(service, sessions[0]) == 1
            assert not snapshot.exists()
            assert (git_run(root, "show-ref"), git_run(root, "write-tree")) == before
        finally:
            release.touch()
            await _drain(tasks)
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_index", [0, 1], ids=["cancel-first", "cancel-second"])
def test_cancel_one_required_check_does_not_stop_peer_check(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    cancel_index: int,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "base a\n", "b.py": "base b\n"})
        service = service_factory(tmp_path)
        names = ("a", "b")
        providers = [_editor(name, {f"{name}.py": f"{name} edit\n"}) for name in names]
        sessions = await _sessions(service, root, *providers)
        await _configure(
            service,
            root,
            "from pathlib import Path\nimport time\n"
            "name = 'a' if Path('a.py').read_text() == 'a edit\\n' else 'b'\n"
            f"barriers = Path({str(tmp_path)!r})\n"
            "(barriers / (name + '.started')).write_text(str(Path.cwd()) + '\\n')\n"
            "while not (barriers / (name + '.release')).exists(): time.sleep(.01)\n",
        )
        tasks: list[asyncio.Task[None]] = []
        try:
            for name, session in zip(names, sessions, strict=True):
                tasks.append(await _start(service, root, session, name, ("*",)))
            await asyncio.gather(*(_wait_for(tmp_path / f"{n}.started") for n in names))
            snapshots = [
                Path((tmp_path / f"{n}.started").read_text().removesuffix("\n"))
                for n in names
            ]
            assert snapshots[0] != snapshots[1]
            assert all(snapshot != root and snapshot.exists() for snapshot in snapshots)
            cancelled, peer = names[cancel_index], names[1 - cancel_index]
            result = await service.handle(
                _request("task.cancel", {"task_id": cancelled})
            )
            assert result["state"] == "stopping"
            await asyncio.wait_for(tasks[cancel_index], 10)
            assert service._task_view(cancelled)["state"] == "cancelled"
            assert service.workflow.checks(cancelled)[0]["state"] == "cancelled"
            assert service.workflow.checks(peer)[0]["state"] == "running"
            assert not snapshots[cancel_index].exists()
            assert snapshots[1 - cancel_index].exists()
            (tmp_path / f"{peer}.release").touch()
            await asyncio.wait_for(tasks[1 - cancel_index], 10)
            _assert_completed(service, peer)
            assert service.workflow.checks(peer)[0]["state"] == "passed"
            assert (root / f"{peer}.py").read_text() == f"{peer} edit\n"
            assert (root / f"{cancelled}.py").read_text() == f"base {cancelled}\n"
            assert service.workflow.inspect(cancelled)["paths"] == [f"{cancelled}.py"]
            _assert_released(service, cancelled)
            assert _revision(service, sessions[0]) == 1
            assert all(not snapshot.exists() for snapshot in snapshots)
        finally:
            for name in names:
                (tmp_path / f"{name}.release").touch()
            await _drain(tasks)
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("overlapping", [False, True], ids=["disjoint", "overlapping"])
@pytest.mark.parametrize(
    "required_checks", [False, True], ids=["unchecked", "verified"]
)
def test_simultaneous_review_applies_publish_whole_batches(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    git_run: Callable[..., str],
    overlapping: bool,
    required_checks: bool,
) -> None:
    async def scenario() -> None:
        initial = {"shared.py": "base\n", "a.py": "base a\n", "b.py": "base b\n"}
        root = repository_factory(tmp_path, initial)
        service = service_factory(tmp_path)
        names = ("a", "b")
        proposals = [
            {
                f"{name}.py": f"{name} private\n",
                **({"shared.py": name} if overlapping else {}),
            }
            for name in names
        ]
        providers = [
            _editor(name, proposal)
            for name, proposal in zip(names, proposals, strict=True)
        ]
        sessions = await _sessions(service, root, *providers, review=True)
        if required_checks:
            await _configure(service, root, "print('verified')")
        before = git_run(root, "show-ref"), git_run(root, "write-tree")
        tasks: list[asyncio.Task[None]] = []
        try:
            for name, session in zip(names, sessions, strict=True):
                tasks.append(await _start(service, root, session, name, ("*",)))
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            assert all(
                service._task_view(n)["state"] == "awaiting_review" for n in names
            )
            if required_checks:
                assert all(
                    service.workflow.checks(n)[0]["state"] == "passed" for n in names
                )
            assert {path: (root / path).read_text() for path in initial} == initial
            results = await asyncio.gather(
                *(
                    service.handle(_request("task.apply", {"task_id": n}))
                    for n in names
                ),
                return_exceptions=True,
            )
            winners = [
                i
                for i, result in enumerate(results)
                if not isinstance(result, BaseException)
            ]
            assert len(winners) == (1 if overlapping or required_checks else 2), results
            for index, name in enumerate(names):
                if index in winners:
                    _assert_completed(service, name)
                    assert (root / f"{name}.py").read_text() == f"{name} private\n"
                else:
                    assert isinstance(results[index], LlmCoordError)
                    assert ("stale" if required_checks else "Conflict") in str(
                        results[index]
                    )
                    assert (root / f"{name}.py").read_text() == f"base {name}\n"
                    assert service._task_view(name)["state"] == "awaiting_review"
                    assert "private" in service.workflow.inspect(name)["diff"]
                _assert_released(service, name)
            assert (root / "shared.py").read_text() == (
                names[winners[0]] if overlapping else "base\n"
            )
            assert _revision(service, sessions[0]) == len(winners)
            assert (git_run(root, "show-ref"), git_run(root, "write-tree")) == before
        finally:
            await _drain(tasks)
            service.close()

    asyncio.run(scenario())


def test_concurrent_apply_and_undo_cannot_overwrite_each_other(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        initial = {
            "shared.py": "base\n",
            "first.py": "base first\n",
            "peer.py": "base peer\n",
        }
        root = repository_factory(tmp_path, initial)
        service = service_factory(tmp_path)
        first = _editor("first", {"shared.py": "first\n", "first.py": "first edit\n"})
        peer = _editor("peer", {"shared.py": "peer\n", "peer.py": "peer edit\n"})
        sessions = await _sessions(service, root, first, peer)
        await service.handle(
            _request("session.set_mode", {**sessions[1], "mode": "normal"})
        )
        tasks: list[asyncio.Task[None]] = []
        try:
            tasks.append(await _start(service, root, sessions[0], "first", ("*",)))
            await asyncio.wait_for(tasks[0], 10)
            _assert_completed(service, "first")
            # The peer observes the first publication as its exact base.
            tasks.append(await _start(service, root, sessions[1], "peer", ("*",)))
            await asyncio.wait_for(tasks[1], 10)
            assert service._task_view("peer")["state"] == "awaiting_review"
            results = await asyncio.gather(
                service.handle(_request("task.undo", {"task_id": "first"})),
                service.handle(_request("task.apply", {"task_id": "peer"})),
                return_exceptions=True,
            )
            failures = [r for r in results if isinstance(r, BaseException)]
            assert len(failures) == 1 and isinstance(failures[0], LlmCoordError), (
                results
            )
            assert "Conflict" in str(failures[0])
            if isinstance(results[0], BaseException):
                expected = {
                    "shared.py": "peer\n",
                    "first.py": "first edit\n",
                    "peer.py": "peer edit\n",
                }
            else:
                expected = initial
            assert {path: (root / path).read_text() for path in initial} == expected
            assert _revision(service, sessions[0]) == 2
        finally:
            await _drain(tasks)
            service.close()

    asyncio.run(scenario())


def test_duplicate_simultaneous_manual_actions_publish_once(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "base\n"})
        service = service_factory(tmp_path)
        provider = _editor("reviewer", {"a.py": "edit\n"})
        sessions = await _sessions(service, root, provider, review=True)
        tasks: list[asyncio.Task[None]] = []
        try:
            tasks.append(await _start(service, root, sessions[0], "task", ("*",)))
            await asyncio.wait_for(tasks[0], 10)
            for revision, (method, content) in enumerate(
                (("task.apply", "edit\n"), ("task.undo", "base\n")), start=1
            ):
                results = await asyncio.gather(
                    *(
                        service.handle(_request(method, {"task_id": "task"}))
                        for _ in range(8)
                    )
                )
                assert len({result["task_id"] for result in results}) == 1
                assert (root / "a.py").read_text() == content
                assert _revision(service, sessions[0]) == revision
                for result in results:
                    _assert_completed(service, result["task_id"])
            with service.store.connection() as connection:
                assert (
                    connection.execute(
                        "SELECT count(*) FROM task_events WHERE event_type "
                        "IN ('workflow.apply','workflow.undo')"
                    ).fetchone()[0]
                    == 2
                )
        finally:
            await _drain(tasks)
            service.close()

    asyncio.run(scenario())
