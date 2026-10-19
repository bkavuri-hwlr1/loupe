"""Execution/thread and journal witnesses for ADR 0010's real authority edges."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import select
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from llm_cli.config.models import Settings
from llm_cli.coordination.models import ClaimRecord, RepositoryRecord, TaskRecord
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.execution import runner as runner_module
from llm_cli.execution.runner import FixtureWrite, FixtureWriteRunner
from llm_cli.git import worktrees as worktrees_module
from llm_cli.git.integrate import read_task_ref
from llm_cli.git.worktrees import list_git_worktrees
from llm_cli.paths import AppPaths
from llm_cli.protocol.envelopes import Request
from llm_cli.workspace import batches as batches_module
from llm_cli.workspace.authority import PhysicalCheckout, process_start_identity
from llm_cli.workspace.batches import BatchFile
from llm_cli.workspace.identity import REGULAR_MODE, identify_path
from llm_cli.workspace.installation import private_read, private_write


class Crash(BaseException):
    """Stop a worker without executing ordinary failure/cleanup handlers."""


def _request(method: str, **params: Any) -> Request:
    if method == "task.run":
        params.setdefault("title", "authority execution boundary")
    return Request.create(
        request_id="authority-boundary",
        method=method,
        params=params,
        profile_id="owner",
    )


def _service(tmp_path: Path, name: str, *, boot: str = "before") -> DaemonService:
    paths = AppPaths.resolve(
        name,
        environ={
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
        },
    )
    result = DaemonService(
        paths,
        Settings(profile_id=name),
        asyncio.Event(),
        boot_id=f"{boot}-{name}",
        authority_root=tmp_path / "physical-authority" / "v1",
    )
    result.initialize()
    return result


async def _open(
    service: DaemonService, repository: Path, session_id: str = "session"
) -> dict[str, str]:
    await service.handle(_request("repo.add", path=str(repository)))
    credentials = {"session_id": session_id, "resume_secret": "resume-" + session_id}
    opened = await service.handle(
        _request(
            "session.open",
            path=str(repository),
            session_id=session_id,
            resume_token_hash=hashlib.sha256(
                credentials["resume_secret"].encode()
            ).hexdigest(),
        )
    )
    await service.handle(
        _request("session.ack", **credentials, sequence=opened["bootstrap_sequence"])
    )
    return credentials


async def _refused(
    service: DaemonService, repository: Path, *, dirty: bool = False
) -> None:
    with pytest.raises(LlmCoordError) as refusal:
        await _open(service, repository, "foreign")
    assert refusal.value.code is (
        ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
        if dirty
        else ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE
    )
    assert refusal.value.details is not None
    assert refusal.value.details["owner_profile"] == "owner"


async def _wait(predicate: Callable[[], bool], timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _marker(service: DaemonService, repository: Path) -> dict[str, Any]:
    physical = PhysicalCheckout.inspect(repository)
    try:
        result = private_read(
            service.physical_authority.root / "checkouts" / physical.key / "marker.json"
        )
        assert result is not None
        return result
    finally:
        physical.close()


@dataclass
class _BlockedFixture:
    original: FixtureWriteRunner
    started: threading.Event
    release: threading.Event
    finished: threading.Event
    failures: list[BaseException]

    def execute(
        self,
        *,
        repository: RepositoryRecord,
        task: TaskRecord,
        claim: ClaimRecord,
        writes: tuple[FixtureWrite, ...],
    ) -> None:
        self.started.set()
        try:
            if not self.release.wait(20):
                raise TimeoutError("worker fixture was not released")
            self.original.execute(
                repository=repository, task=task, claim=claim, writes=writes
            )
        except BaseException as exc:
            self.failures.append(exc)
            raise
        finally:
            self.finished.set()


async def _launch_fixture(
    service: DaemonService, repository: Path, task_id: str
) -> asyncio.Task[None]:
    result = await service.handle(
        _request(
            "task.run",
            path=str(repository),
            task_id=task_id,
            scopes=["file.txt"],
            fixture_writes=["file.txt=worker result\n"],
        )
    )
    assert result["execution"] == "scheduled"
    return service._background_tasks[(task_id, 1)]


@pytest.mark.parametrize("queued", [False, True])
def test_actual_executor_survives_cancelled_waiter_or_shutdown_without_releasing_owner(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    queued: bool,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"file.txt": "base\n"})
        owner, peer = _service(tmp_path, "owner"), _service(tmp_path, "peer")
        gate = _BlockedFixture(
            owner.fixture_runner,
            threading.Event(),
            threading.Event(),
            threading.Event(),
            [],
        )
        owner.fixture_runner = cast(FixtureWriteRunner, gate)
        pool_release = threading.Event()
        occupant = None
        background = None
        try:
            await owner.handle(_request("repo.add", path=str(repository)))
            if queued:
                owner._execution_pool.shutdown(wait=True)
                owner._execution_pool = ThreadPoolExecutor(max_workers=1)
                occupant_started = threading.Event()

                def occupy() -> None:
                    occupant_started.set()
                    assert pool_release.wait(20)

                occupant = owner._execution_pool.submit(occupy)
                assert await asyncio.to_thread(occupant_started.wait, 5)
            background = await _launch_fixture(owner, repository, "worker")
            if queued:
                await asyncio.sleep(
                    0.05
                )  # Submit behind the independently blocked executor job.
                assert not gate.started.is_set()
                assert await owner.drain(timeout_ms=1) == (("worker", 1),)
            else:
                assert await asyncio.to_thread(gate.started.wait, 5)
                background.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await background
            owner.close()  # Clears the registry; it does not stop the actual worker.
            assert owner._background_tasks == {}
            owner.physical_authority.release_idle(now=time.monotonic() + 60, force=True)
            await _refused(peer, repository)
            if queued:
                pool_release.set()
                assert await asyncio.to_thread(gate.started.wait, 5)
                await _refused(peer, repository)
            assert not gate.finished.is_set()
            gate.release.set()
            assert await asyncio.to_thread(gate.finished.wait, 10)
            if background is not None and not background.cancelled():
                await asyncio.wait_for(background, 10)
            await _wait(lambda: not owner.physical_authority._owned)
            assert gate.failures == []
            assert read_task_ref(repository, "worker") is not None
            assert (
                repository / "file.txt"
            ).read_text() == "base\n"  # Legacy fixture publishes its task ref.
            assert not (owner.paths.data_dir / "worktrees" / "worker").exists()
            await _open(peer, repository, "after-worker")
        finally:
            pool_release.set()
            gate.release.set()
            if occupant is not None:
                await asyncio.to_thread(occupant.result, 10)
            if background is not None and not background.done():
                await asyncio.wait_for(background, 10)
            owner.close()
            peer.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["before_start", "submit"])
def test_executor_work_that_never_starts_does_not_leak_ownership(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    failure: str,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"file.txt": "base\n"})
        owner, peer = _service(tmp_path, "owner"), _service(tmp_path, "peer")
        gate = _BlockedFixture(
            owner.fixture_runner,
            threading.Event(),
            threading.Event(),
            threading.Event(),
            [],
        )
        owner.fixture_runner = cast(FixtureWriteRunner, gate)
        try:
            await owner.handle(_request("repo.add", path=str(repository)))
            if failure == "submit":
                owner._execution_pool.shutdown(wait=True)
            background = await _launch_fixture(owner, repository, "never-starts")
            if failure == "before_start":
                background.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await background
                await asyncio.sleep(0)  # Run the schedule completion callback.
            else:
                await asyncio.wait_for(background, 10)
                task = owner.store.get_task("never-starts")
                assert task is not None and task.state == "failed"
            assert not gate.started.is_set()
            assert read_task_ref(repository, "never-starts") is None
            owner.close()
            assert not owner.physical_authority._owned
            await _open(peer, repository, "safe-after-never-started")
        finally:
            gate.release.set()
            owner.close()
            peer.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["partial_files", "database", "third_state"])
def test_actual_batch_dirty_journal_fences_foreign_and_requires_exact_owner_proof(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"}
        )
        owner, peer = _service(tmp_path, "owner"), _service(tmp_path, "peer")
        await _open(owner, repository, "batch-session")
        files = tuple(
            BatchFile(path, identify_path(repository / path), body, REGULAR_MODE)
            for path, body in (("a.txt", b"new a\n"), ("b.txt", b"new b\n"))
        )
        try:
            with monkeypatch.context() as patch:
                replace = batches_module.atomic_replace_regular_file
                apply = owner.workspace_batches._apply_owned
                if boundary == "partial_files":

                    def replace_then_crash(**kwargs: Any) -> None:
                        marker = _marker(owner, repository)
                        assert (
                            marker["state"] == "dirty"
                            and marker["journal_kind"] == "batch"
                        )
                        with owner.store.connection() as connection:
                            row = connection.execute(
                                "SELECT state FROM workspace_batches "
                                "WHERE batch_id='batch-boundary'"
                            ).fetchone()
                            assert row is not None and row[0] == "applying"
                        replace(**kwargs)
                        raise Crash()

                    patch.setattr(
                        batches_module,
                        "atomic_replace_regular_file",
                        replace_then_crash,
                    )
                else:

                    def apply_then_crash(batch_id: str) -> Any:
                        result = apply(batch_id)
                        assert result.state == "published"
                        assert _marker(owner, repository)["state"] == "dirty"
                        raise Crash()

                    patch.setattr(
                        owner.workspace_batches, "_apply_owned", apply_then_crash
                    )
                with pytest.raises(Crash):
                    owner.workspace_batches.publish(
                        "batch-boundary", "batch-session", files, ("*",)
                    )
            if boundary == "third_state":
                (repository / "a.txt").write_text("external third state\n")
            after_crash = tuple(
                (repository / path).read_bytes() for path in ("a.txt", "b.txt")
            )
            assert _marker(owner, repository)["state"] == "dirty"
            if boundary == "partial_files":
                assert after_crash == (b"new a\n", b"old b\n")
            await _refused(peer, repository, dirty=True)
            owner.close()
            await _refused(peer, repository, dirty=True)
            await peer.handle(_request("task.recover"))
            assert (
                tuple((repository / path).read_bytes() for path in ("a.txt", "b.txt"))
                == after_crash
            )
            restarted = _service(tmp_path, "owner", boot="after")
            try:
                if boundary == "third_state":
                    assert _marker(restarted, repository)["state"] != "clean"
                    assert (
                        repository / "a.txt"
                    ).read_text() == "external third state\n"
                    await _refused(peer, repository, dirty=True)
                else:
                    assert _marker(restarted, repository)["state"] == "clean"
                    assert (repository / "a.txt").read_bytes() == b"new a\n"
                    assert (repository / "b.txt").read_bytes() == b"new b\n"
                    with restarted.store.connection() as connection:
                        assert (
                            connection.execute(
                                "SELECT count(*) FROM checkout_events "
                                "WHERE event_type='workspace.batch_published'"
                            ).fetchone()[0]
                            == 1
                        )
                        assert (
                            connection.execute(
                                "SELECT state FROM workspace_batches "
                                "WHERE batch_id='batch-boundary'"
                            ).fetchone()[0]
                            == "published"
                        )
            finally:
                restarted.close()
        finally:
            owner.close()
            peer.close()

    asyncio.run(scenario())


def test_held_apply_retains_proposal_when_foreign_then_recovers_its_actual_batch(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_shared_agent_execution import _finish
    from test_verified_workflow import await_task, edit_provider, setup

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        owner = _service(tmp_path, "owner")
        peer = _service(tmp_path, "peer")
        restarted = None
        try:
            credentials = await setup(
                owner, repository, edit_provider(_finish()), review=True
            )
            await await_task(owner, repository, credentials, "held")
            held = owner.workflow.get("held")
            assert held is not None and held["status"] == "awaiting_review"
            original_proposal = held["proposal_json"]
            owner.close()
            await _open(peer, repository, "peer-period")
            restarted = _service(tmp_path, "owner", boot="apply")
            with pytest.raises(LlmCoordError) as refused:
                await restarted.handle(_request("task.apply", task_id="held"))
            assert refused.value.code is ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE
            untouched = restarted.workflow.get("held")
            assert (
                untouched is not None
                and untouched["proposal_json"] == original_proposal
            )
            assert (
                untouched["status"] == "awaiting_review"
                and untouched["action_task_id"] is None
            )
            assert (repository / "a.py").read_text() == "value = 1\n"
            peer.close()
            replace = batches_module.atomic_replace_regular_file
            with monkeypatch.context() as patch:

                def replace_then_crash(**kwargs: Any) -> None:
                    marker = _marker(restarted, repository)
                    assert (
                        marker["state"] == "dirty" and marker["journal_kind"] == "batch"
                    )
                    replace(**kwargs)
                    raise Crash()

                patch.setattr(
                    batches_module, "atomic_replace_regular_file", replace_then_crash
                )
                with pytest.raises(Crash):
                    await restarted.handle(_request("task.apply", task_id="held"))
            assert (repository / "a.py").read_text() == "value = 2\n"
            assert _marker(restarted, repository)["state"] == "dirty"
            restarted.close()
            peer = _service(tmp_path, "peer", boot="foreign-after-crash")
            await _refused(peer, repository, dirty=True)
            restarted = _service(tmp_path, "owner", boot="recover-held")
            assert _marker(restarted, repository)["state"] == "clean"
            settled = restarted.workflow.get("held")
            assert settled is not None and settled["status"] == "published"
            assert settled["proposal_json"] == original_proposal
            assert (repository / "a.py").read_text() == "value = 2\n"
            assert (await restarted.handle(_request("task.apply", task_id="held")))[
                "state"
            ] == "published"
        finally:
            owner.close()
            peer.close()
            if restarted is not None:
                restarted.close()

    asyncio.run(scenario())


async def _legacy_attempt(
    service: DaemonService,
    repository: Path,
    *,
    session_id: str | None = None,
    legacy_without_receipts: bool = False,
) -> tuple[RepositoryRecord, TaskRecord, ClaimRecord]:
    await service.handle(_request("repo.add", path=str(repository)))
    repository_record = service.store.list_repositories()[0]
    if legacy_without_receipts:
        # Simulate a pre-G2 claim, whose coordinator never recorded authority.
        task = service.store.create_task(
            repository_id=repository_record.repository_id,
            task_id="legacy-task",
            title="legacy publication",
        )
        claim = service.coordinator.request_claim(task.task_id, ["file.txt"])
        return repository_record, task, claim
    credentials = (
        {"session_id": session_id, "resume_secret": "resume-" + session_id}
        if session_id
        else {}
    )
    result = await service.handle(
        _request(
            "task.run",
            path=str(repository),
            task_id="legacy-task",
            scopes=["file.txt"],
            claim_only=True,
            **credentials,
        )
    )
    task = service.store.get_task("legacy-task")
    claim = service.store.get_claim(result["claim"]["claim_id"])
    assert task is not None and claim is not None
    return service._execution_repository(repository_record, task), task, claim


@pytest.mark.parametrize("boundary", ["worktree_registration", "ref_swap"])
def test_real_legacy_git_mutations_mark_dirty_and_foreign_cannot_recover(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"file.txt": "original\n"})
        owner, peer = _service(tmp_path, "owner"), _service(tmp_path, "peer")
        record, task, claim = await _legacy_attempt(owner, repository)
        result_oid = None
        try:
            with monkeypatch.context() as patch:
                if boundary == "worktree_registration":
                    run_git = worktrees_module.run_git

                    def add_then_crash(
                        root: Path, arguments: list[str], **kwargs: Any
                    ) -> Any:
                        if arguments[:2] == ["worktree", "add"]:
                            marker = _marker(owner, repository)
                            assert marker["state"] == "dirty"
                            assert (
                                marker["journal_kind"] == "git:create_managed_worktree"
                            )
                            run_git(root, arguments, **kwargs)
                            raise Crash()
                        return run_git(root, arguments, **kwargs)

                    patch.setattr(worktrees_module, "run_git", add_then_crash)
                else:
                    publish = runner_module.publish_task_ref

                    def publish_then_crash(*args: Any, **kwargs: Any) -> Any:
                        marker = _marker(owner, repository)
                        assert (
                            marker["state"] == "dirty"
                            and marker["journal_kind"] == "legacy_execution"
                        )
                        publish(*args, **kwargs)
                        raise Crash()

                    patch.setattr(runner_module, "publish_task_ref", publish_then_crash)
                with pytest.raises(Crash):
                    owner.fixture_runner.execute(
                        repository=record,
                        task=task,
                        claim=claim,
                        writes=(FixtureWrite("file.txt", "legacy result\n"),),
                    )
            result_oid = read_task_ref(repository, task.task_id)
            assert (result_oid is not None) is (boundary == "ref_swap")
            assert _marker(owner, repository)["state"] == "dirty"
            private = owner.paths.data_dir / "worktrees" / task.task_id
            assert private.exists()
            assert private.resolve() in {
                entry.path for entry in list_git_worktrees(repository)
            }
            await _refused(peer, repository, dirty=True)
            owner.close()
            await peer.handle(_request("task.recover"))
            assert read_task_ref(repository, task.task_id) == result_oid
            assert private.exists()
            restarted = _service(tmp_path, "owner", boot="legacy-recovery")
            try:
                assert restarted.startup_recovery is not None
                if boundary == "ref_swap":
                    assert restarted.startup_recovery.summary == {"confirmed": 1}
                    assert _marker(restarted, repository)["state"] == "clean"
                    intent = restarted.store.latest_publication_intent(task.task_id, 1)
                    assert intent is not None and intent.operation_state == "confirmed"
                    assert not private.exists()
                else:
                    assert restarted.startup_recovery.summary == {
                        "operator_attention": 1
                    }
                    assert _marker(restarted, repository)["state"] != "clean"
                    assert private.exists()
                assert read_task_ref(repository, task.task_id) == result_oid
                assert (repository / "file.txt").read_text() == "original\n"
            finally:
                restarted.close()
        finally:
            owner.close()
            peer.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("published", [False, True])
def test_legacy_session_recovery_uses_exact_checkout_after_another_clone_registration(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    published: bool,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"file.txt": "original\n"})
        clone = tmp_path / "other-clone"
        git_run(repository, "clone", str(repository), str(clone))
        git_run(clone, "remote", "remove", "origin")
        for path in (repository, clone):
            git_run(
                path,
                "remote",
                "add",
                "origin",
                "https://example.invalid/owner/repository.git",
            )
        owner = _service(tmp_path, "owner")
        try:
            credentials = await _open(owner, repository, "legacy-session")
            record, task, claim = await _legacy_attempt(
                owner, repository, session_id=credentials["session_id"]
            )
            publish = runner_module.publish_task_ref
            with monkeypatch.context() as patch:

                def crash(*args: Any, **kwargs: Any) -> Any:
                    if published:
                        publish(*args, **kwargs)
                    raise Crash()

                patch.setattr(runner_module, "publish_task_ref", crash)
                with pytest.raises(Crash):
                    owner.fixture_runner.execute(
                        repository=record,
                        task=task,
                        claim=claim,
                        writes=(FixtureWrite("file.txt", "result\n"),),
                    )
            result = read_task_ref(repository, task.task_id)
            assert (result is not None) is published
            assert read_task_ref(clone, task.task_id) is None
            await owner.handle(_request("repo.add", path=str(clone)))
            assert Path(owner.store.list_repositories()[0].main_worktree_path) == clone
            owner.close()
            restarted = _service(tmp_path, "owner", boot="different-registration")
            try:
                assert restarted.startup_recovery is not None
                assert restarted.startup_recovery.summary == {
                    "confirmed" if published else "failed_safe": 1
                }
                intent = restarted.store.latest_publication_intent(task.task_id, 1)
                assert intent is not None and intent.operation_state == (
                    "confirmed" if published else "failed_safe"
                )
                assert read_task_ref(repository, task.task_id) == result
                assert read_task_ref(clone, task.task_id) is None
                assert (clone / "file.txt").read_text() == "original\n"
                assert _marker(restarted, repository)["state"] == "clean"
                private = restarted.paths.data_dir / "worktrees" / task.task_id
                assert not private.exists()
            finally:
                restarted.close()
        finally:
            owner.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("session_bound", [False, True])
def test_linked_claim_reservation_pins_actual_execution_checkout(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    session_bound: bool,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"file.txt": "base\n"})
        linked = tmp_path / "linked"
        git_run(
            repository, "worktree", "add", "-b", "linked-branch", str(linked), "HEAD"
        )
        owner, peer = _service(tmp_path, "owner"), _service(tmp_path, "peer")
        try:
            await owner.handle(_request("repo.add", path=str(repository)))
            credentials = (
                await _open(owner, linked, "linked-session") if session_bound else {}
            )
            result = await owner.handle(
                _request(
                    "task.run",
                    path=str(linked),
                    task_id="claim-only",
                    scopes=["file.txt"],
                    claim_only=True,
                    **credentials,
                )
            )
            assert result["claim"]["state"] == "active_work"
            target = linked if session_bound else repository
            owner.physical_authority.release_idle(now=time.monotonic() + 60, force=True)
            await _refused(peer, target)
            other = repository if session_bound else linked
            await _open(peer, other, "independent-worktree")
        finally:
            owner.close()
            peer.close()

    asyncio.run(scenario())


def test_sessionless_linked_claim_is_refused_before_grant_when_main_is_foreign_owned(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"file.txt": "base\n"})
        linked = tmp_path / "linked"
        git_run(
            repository, "worktree", "add", "-b", "linked-branch", str(linked), "HEAD"
        )
        owner, peer = _service(tmp_path, "owner"), _service(tmp_path, "peer")
        try:
            await owner.handle(_request("repo.add", path=str(repository)))
            await _open(peer, repository, "owns-main")
            with pytest.raises(LlmCoordError) as refusal:
                await owner.handle(
                    _request(
                        "task.run",
                        path=str(linked),
                        task_id="must-not-grant",
                        scopes=["file.txt"],
                        claim_only=True,
                    )
                )
            assert refusal.value.code is ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE
            assert owner.store.get_task("must-not-grant") is None
        finally:
            owner.close()
            peer.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("published", [False, True])
def test_same_boot_explicit_legacy_recovery_cleans_exact_marker_and_private_worktree(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    published: bool,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"file.txt": "original\n"})
        owner = _service(tmp_path, "owner")
        try:
            record, task, claim = await _legacy_attempt(owner, repository)
            publish = runner_module.publish_task_ref
            with monkeypatch.context() as patch:

                def fail(*args: Any, **kwargs: Any) -> Any:
                    if published:
                        publish(*args, **kwargs)
                    raise RuntimeError("injected publication transport failure")

                patch.setattr(runner_module, "publish_task_ref", fail)
                with pytest.raises(RuntimeError, match="transport failure"):
                    owner.fixture_runner.execute(
                        repository=record,
                        task=task,
                        claim=claim,
                        writes=(FixtureWrite("file.txt", "result\n"),),
                    )
            result = read_task_ref(repository, task.task_id)
            assert (result is not None) is published
            assert _marker(owner, repository)["state"] == "dirty"
            private = owner.paths.data_dir / "worktrees" / task.task_id
            assert private.exists()
            recovery = await owner.handle(_request("task.recover"))
            assert recovery["summary"] == {
                "confirmed" if published else "failed_safe": 1
            }
            assert recovery["blocking_task_ids"] == []
            assert _marker(owner, repository)["state"] == "clean"
            assert not private.exists()
            intent = owner.store.latest_publication_intent(task.task_id, 1)
            assert intent is not None and intent.operation_state == (
                "confirmed" if published else "failed_safe"
            )
            assert read_task_ref(repository, task.task_id) == result
            assert (await owner.handle(_request("task.recover")))["summary"] == {}
        finally:
            owner.close()

    asyncio.run(scenario())


def test_old_sessionless_recovery_refuses_another_clones_only_binding(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"file.txt": "original\n"})
        clone = tmp_path / "other-clone"
        git_run(repository, "clone", str(repository), str(clone))
        git_run(clone, "remote", "remove", "origin")
        for path in (repository, clone):
            git_run(
                path,
                "remote",
                "add",
                "origin",
                "https://example.invalid/owner/repository.git",
            )
        owner = _service(tmp_path, "owner")
        try:
            record, task, claim = await _legacy_attempt(
                owner, repository, legacy_without_receipts=True
            )
            publish = runner_module.publish_task_ref
            authority = owner.store.authority
            # Exercise the real legacy runner with no G2 authority receipts,
            # as an older binary/database would have left it after a crash.
            owner.store.authority = None
            try:
                with monkeypatch.context() as patch:

                    def publish_then_crash(*args: Any, **kwargs: Any) -> Any:
                        publish(*args, **kwargs)
                        raise Crash()

                    patch.setattr(runner_module, "publish_task_ref", publish_then_crash)
                    with pytest.raises(Crash):
                        owner.fixture_runner.execute(
                            repository=record,
                            task=task,
                            claim=claim,
                            writes=(FixtureWrite("file.txt", "result\n"),),
                        )
            finally:
                owner.store.authority = authority
            result = read_task_ref(repository, task.task_id)
            assert result is not None
            with owner.store.connection() as connection:
                assert (
                    connection.execute(
                        "SELECT count(*) FROM authority_journals"
                    ).fetchone()[0]
                    == 0
                )
            await _open(owner, clone, "only-bound-checkout")
            with owner.store.connection() as connection:
                bindings = connection.execute(
                    "SELECT count(*) FROM authority_checkouts"
                ).fetchone()[0]
                assert bindings == 1
            assert Path(owner.store.list_repositories()[0].main_worktree_path) == clone
            owner.close()
            restarted = _service(tmp_path, "owner", boot="old-unbound-recovery")
            try:
                assert restarted.startup_recovery is not None
                assert restarted.startup_recovery.summary == {"operator_attention": 1}
                intent = restarted.store.latest_publication_intent(task.task_id, 1)
                assert intent is not None and intent.operation_state == "prepared"
                retained = restarted.store.get_claim(claim.claim_id)
                assert retained is not None and retained.state == "publishing"
                assert read_task_ref(repository, task.task_id) == result
                assert read_task_ref(clone, task.task_id) is None
                assert (clone / "file.txt").read_text() == "original\n"
                assert (restarted.paths.data_dir / "worktrees" / task.task_id).exists()
            finally:
                restarted.close()
        finally:
            owner.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("third_ref", [False, True])
def test_current_boot_cleanup_failed_retries_without_clearing_an_unverified_ref(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    third_ref: bool,
) -> None:
    from llm_cli.execution import recovery as recovery_module

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"file.txt": "original\n"})
        owner = _service(tmp_path, "owner")
        try:
            record, task, claim = await _legacy_attempt(owner, repository)
            publish = runner_module.publish_task_ref
            with monkeypatch.context() as patch:

                def publish_then_crash(*args: Any, **kwargs: Any) -> Any:
                    publish(*args, **kwargs)
                    raise RuntimeError("publication worker stopped")

                patch.setattr(runner_module, "publish_task_ref", publish_then_crash)
                with pytest.raises(RuntimeError, match="worker stopped"):
                    owner.fixture_runner.execute(
                        repository=record,
                        task=task,
                        claim=claim,
                        writes=(FixtureWrite("file.txt", "result\n"),),
                    )
            result = read_task_ref(repository, task.task_id)
            assert result is not None
            with monkeypatch.context() as patch:

                def cleanup_unavailable(*args: Any, **kwargs: Any) -> None:
                    raise LlmCoordError(
                        ErrorCode.REPOSITORY_UNSAFE, "temporary cleanup refusal"
                    )

                patch.setattr(
                    recovery_module, "remove_managed_worktree", cleanup_unavailable
                )
                recovery = await owner.handle(_request("task.recover"))
            assert recovery["summary"] == {"operator_attention": 1}
            execution = owner.store.get_execution(task.task_id, 1)
            assert execution is not None and execution.boot_id == owner.boot_id
            assert execution.state == "cleanup_failed"
            assert _marker(owner, repository)["state"] == "dirty"
            private = owner.paths.data_dir / "worktrees" / task.task_id
            assert private.exists()
            intent = owner.store.latest_publication_intent(task.task_id, 1)
            assert intent is not None and intent.operation_state == "confirmed"
            if third_ref:
                base = git_run(repository, "rev-parse", "HEAD")
                assert base != result
                git_run(
                    repository,
                    "update-ref",
                    f"refs/llm-coord/tasks/{task.task_id}",
                    base,
                    result,
                )
            retried = await owner.handle(_request("task.recover"))
            if third_ref:
                assert retried["summary"] == {"operator_attention": 1}
                assert _marker(owner, repository)["state"] != "clean"
                assert read_task_ref(repository, task.task_id) == base
                assert private.exists()
            else:
                assert retried["summary"] == {"confirmed": 1}
                assert _marker(owner, repository)["state"] == "clean"
                assert read_task_ref(repository, task.task_id) == result
                assert not private.exists()
                assert (await owner.handle(_request("task.recover")))["summary"] == {}
        finally:
            owner.close()

    asyncio.run(scenario())


def test_completed_legacy_integration_reservation_keeps_original_clone_owned(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"file.txt": "original\n"})
        clone = tmp_path / "other-clone"
        git_run(repository, "clone", str(repository), str(clone))
        git_run(clone, "remote", "remove", "origin")
        for path in (repository, clone):
            git_run(
                path,
                "remote",
                "add",
                "origin",
                "https://example.invalid/owner/repository.git",
            )
        owner, peer = _service(tmp_path, "owner"), _service(tmp_path, "peer")
        try:
            record, task, claim = await _legacy_attempt(owner, repository)
            owner.fixture_runner.execute(
                repository=record,
                task=task,
                claim=claim,
                writes=(FixtureWrite("file.txt", "result\n"),),
            )
            reserved = owner.store.get_claim(claim.claim_id)
            assert reserved is not None and reserved.state == "active_integration"
            assert read_task_ref(repository, task.task_id) is not None
            assert _marker(owner, repository)["state"] == "clean"
            await owner.handle(_request("repo.add", path=str(clone)))
            assert Path(owner.store.list_repositories()[0].main_worktree_path) == clone
            owner.physical_authority.release_idle(now=time.monotonic() + 60, force=True)
            await _refused(peer, repository)
            await _open(peer, clone, "independent-clone")
            retained = owner.store.get_claim(claim.claim_id)
            assert retained is not None and retained.state == "active_integration"
        finally:
            owner.close()
            peer.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("queued", [False, True])
def test_legacy_claim_keeps_its_original_clone_after_another_registration(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    queued: bool,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"file.txt": "original\n"})
        clone = tmp_path / "other-clone"
        git_run(repository, "clone", str(repository), str(clone))
        git_run(clone, "remote", "remove", "origin")
        for path in (repository, clone):
            git_run(
                path,
                "remote",
                "add",
                "origin",
                "https://example.invalid/owner/repository.git",
            )
        owner, peer = _service(tmp_path, "owner"), _service(tmp_path, "peer")
        try:
            _record, task, claim = await _legacy_attempt(owner, repository)
            assert claim.state == "active_work"
            if queued:
                waiting = await owner.handle(
                    _request(
                        "task.run",
                        path=str(repository),
                        task_id="queued-launch",
                        scopes=["file.txt"],
                        fixture_writes=["file.txt=queued result\n"],
                    )
                )
                assert waiting["claim"]["state"] == "queued"
                assert owner.store.get_execution("queued-launch", 1) is None
            await owner.handle(_request("repo.add", path=str(clone)))
            assert Path(owner.store.list_repositories()[0].main_worktree_path) == clone
            owner.physical_authority.release_idle(now=time.monotonic() + 60, force=True)
            await _refused(peer, repository)
            await _open(peer, clone, "owns-independent-clone")
            if queued:
                await owner.handle(
                    _request(
                        "claim.release", claim_id=claim.claim_id, reason="finished"
                    )
                )
                await _wait(
                    lambda: read_task_ref(repository, "queued-launch") is not None
                )
                await owner.drain(timeout_ms=10000)
                assert read_task_ref(clone, "queued-launch") is None
                published = owner.store.get_task("queued-launch")
                assert published is not None
                assert published.state == "ready_for_integration"
                owner.physical_authority.release_idle(
                    now=time.monotonic() + 60, force=True
                )
                await _refused(peer, repository)
            else:
                # An idempotent legacy request from the new repository path
                # must still target its original immutable claim checkout.
                repeated = await owner.handle(
                    _request(
                        "task.run",
                        path=str(clone),
                        task_id=task.task_id,
                        scopes=["file.txt"],
                        claim_only=True,
                    )
                )
                assert repeated["claim"]["claim_id"] == claim.claim_id
                assert repeated["claim"]["state"] == "active_work"
            assert (repository / "file.txt").read_text() == "original\n"
            assert (clone / "file.txt").read_text() == "original\n"
        finally:
            await owner.drain(timeout_ms=10000)
            owner.close()
            peer.close()

    asyncio.run(scenario())


_PROCESS_AUTHORITY = r"""
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from llm_cli.config.models import Settings
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.envelopes import Request
from llm_cli.workspace import authority

account, repository, profile, mode = sys.argv[1:]
# A test account home substitutes for getpwuid, not for authority-root selection.
authority.pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir=account)
paths = AppPaths.resolve(profile)
service = DaemonService(
    paths, Settings(profile_id=profile), asyncio.Event(),
    boot_id=f"{profile}-{os.getpid()}"
)
service.initialize()

def request(method, **params):
    return Request.create(
        request_id="process-boundary", method=method, params=params, profile_id=profile
    )

async def main():
    try:
        await service.handle(request("repo.add", path=repository))
        await service.handle(request(
            "session.open", path=repository, session_id="process-session",
            resume_token_hash=hashlib.sha256(b"resume").hexdigest(),
        ))
        code = "acquired"
    except LlmCoordError as exc:
        code = exc.code.value
    print(json.dumps({"root": str(service.physical_authority.root), "code": code,
                      "pid": os.getpid()}), flush=True)
    if mode == "hold":
        sys.stdin.readline()

try:
    asyncio.run(main())
finally:
    service.close()
"""


def test_process_environment_overrides_and_reused_pid_metadata_cannot_split_authority(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    repository = repository_factory(tmp_path, {"file.txt": "ordinary source\n"})
    account = tmp_path / "account"
    account.mkdir(mode=0o700)
    expected_root = account / ".llm-coord/physical-authority/v1"

    def arguments(profile: str, mode: str) -> list[str]:
        return [
            sys.executable,
            "-c",
            _PROCESS_AUTHORITY,
            str(account),
            str(repository),
            profile,
            mode,
        ]

    def environment(profile: str) -> dict[str, str]:
        values = os.environ.copy()
        values["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        values["PYTHONDONTWRITEBYTECODE"] = "1"
        for name in (
            "HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
            "LLM_COORD_CONFIG_HOME",
            "LLM_COORD_DATA_HOME",
            "LLM_COORD_STATE_HOME",
            "LLM_COORD_RUNTIME_DIR",
            "LLM_COORD_AUTHORITY_ROOT",
        ):
            values[name] = str(tmp_path / profile / name.lower())
        return values

    owner = subprocess.Popen(
        arguments("owner", "hold"),
        env=environment("owner"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        assert select.select([owner.stdout], [], [], 10)[0], "owner did not initialize"
        line = owner.stdout.readline()
        assert line, owner.communicate(timeout=10)
        opened = json.loads(line)
        assert opened == {
            "root": str(expected_root), "code": "acquired", "pid": owner.pid
        }
        physical = PhysicalCheckout.inspect(repository)
        try:
            owner_path = expected_root / "checkouts" / physical.key / "owner.json"
        finally:
            physical.close()
        metadata = private_read(owner_path)
        assert metadata is not None
        assert metadata["pid"] == owner.pid
        assert metadata["process_start"] == process_start_identity(owner.pid)
        # Emulate a PID reused by another live process. Neither a mismatched
        # start identity nor live metadata is permission to steal a held flock.
        private_write(
            owner_path,
            {**metadata, "pid": os.getpid(), "process_start": "previous-process-start"},
        )
        refused = subprocess.run(
            arguments("peer", "once"),
            env=environment("peer"),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        observed = json.loads(refused.stdout)
        assert observed["root"] == str(expected_root)
        assert observed["code"] == ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE.value
        owner.communicate("\n", timeout=10)
        assert owner.returncode == 0
        acquired = subprocess.run(
            arguments("peer", "once"),
            env=environment("peer"),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        observed = json.loads(acquired.stdout)
        assert observed["root"] == str(expected_root)
        assert observed["code"] == "acquired"
    finally:
        if owner.poll() is None:
            owner.communicate("\n", timeout=10)


@pytest.mark.parametrize("abandoned", [False, True])
def test_same_profile_pin_waits_for_live_git_mutation_and_revalidates_its_outcome(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    abandoned: bool,
) -> None:
    repository = repository_factory(tmp_path, {"file.txt": "ordinary source\n"})
    owner, peer = _service(tmp_path, "owner"), _service(tmp_path, "peer")
    asyncio.run(_open(owner, repository))
    base = git_run(repository, "rev-parse", "HEAD")
    entered, release, probe = threading.Event(), threading.Event(), threading.Event()
    nested_finished, waiter_started = threading.Event(), threading.Event()
    waiter_finished = threading.Event()
    mutation_failures: list[BaseException] = []
    waiter_failures: list[BaseException] = []
    run_git = worktrees_module.run_git

    def paused_add(root: Path, arguments: list[str], **kwargs: Any) -> Any:
        if arguments[:2] != ["worktree", "add"]:
            return run_git(root, arguments, **kwargs)
        assert _marker(owner, repository)["state"] == "dirty"
        entered.set()
        assert probe.wait(10)
        # A waiting acquisition must not hold the authority's global lock:
        # the live operation still needs it for nested pins and validation.
        with owner.physical_authority.pin(repository) as pin:
            pin.validate()
        # Nested proof must reuse the live parent event, even though this Git
        # journal has no terminal domain evidence for finish_recovery to use.
        assert owner.physical_authority.finish_recovery(repository) is False
        nested_finished.set()
        assert release.wait(10)
        result = run_git(root, arguments, **kwargs)
        if abandoned:
            raise Crash()
        return result

    def mutate() -> None:
        try:
            with owner.physical_authority.pin(repository):
                worktrees_module.create_managed_worktree(
                    repository,
                    managed_root=owner.paths.data_dir / "checks",
                    task_id="live-check-snapshot",
                    base_oid=base,
                )
        except BaseException as exc:
            mutation_failures.append(exc)

    def acquire() -> None:
        waiter_started.set()
        try:
            with owner.physical_authority.pin(repository) as pin:
                pin.validate()
        except BaseException as exc:
            waiter_failures.append(exc)
        finally:
            waiter_finished.set()

    mutation = threading.Thread(target=mutate)
    waiter = threading.Thread(target=acquire)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(worktrees_module, "run_git", paused_add)
            mutation.start()
            assert entered.wait(5)
            waiter.start()
            assert waiter_started.wait(5)
            assert not waiter_finished.wait(0.1)
            asyncio.run(_refused(peer, repository, dirty=True))
            probe.set()
            assert nested_finished.wait(5)
            assert not waiter_finished.is_set()
            release.set()
            mutation.join(10)
            waiter.join(10)
            assert not mutation.is_alive() and not waiter.is_alive()
        if abandoned:
            assert len(mutation_failures) == 1
            assert isinstance(mutation_failures[0], Crash)
            assert len(waiter_failures) == 1
            failure = waiter_failures[0]
            assert isinstance(failure, LlmCoordError)
            assert failure.code is ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
            assert _marker(owner, repository)["state"] == "dirty"
            with (
                pytest.raises(LlmCoordError, match="exact recovery"),
                owner.physical_authority.mutation(
                    repository, "different-journal", "unrelated", recovery=True
                ),
            ):
                pytest.fail("a mismatched recovery journal was admitted")
            asyncio.run(_refused(peer, repository, dirty=True))
        else:
            assert mutation_failures == [] and waiter_failures == []
            assert _marker(owner, repository)["state"] == "clean"
            with owner.physical_authority.pin(repository):
                worktrees_module.remove_managed_worktree(
                    repository,
                    managed_root=owner.paths.data_dir / "checks",
                    registered_path=owner.paths.data_dir / "checks/live-check-snapshot",
                )
    finally:
        probe.set()
        release.set()
        if mutation.ident is not None:
            mutation.join(10)
        if waiter.ident is not None:
            waiter.join(10)
        owner.close()
        peer.close()



def test_isolated_open_waits_before_marking_dirty_when_publication_holds_its_lock(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = repository_factory(tmp_path, {"file.txt": "ordinary source\n"})
    owner = _service(tmp_path, "owner")
    asyncio.run(_open(owner, repository, "shared-publisher"))
    publication_lock = owner.workspace_batches.lock
    lock_requested = threading.Event()
    completed = threading.Event()
    failures: list[BaseException] = []
    responses: list[dict[str, Any]] = []
    caller = threading.get_ident()

    class ObservedLock:
        def __enter__(self) -> None:
            if threading.get_ident() != caller:
                lock_requested.set()
            publication_lock.acquire()

        def __exit__(self, *args: Any) -> None:
            publication_lock.release()

    def open_isolated() -> None:
        try:
            responses.append(
                asyncio.run(
                    owner.handle(
                        _request(
                            "session.open",
                            path=str(repository),
                            session_id="isolated-during-publication",
                            resume_token_hash=hashlib.sha256(b"resume").hexdigest(),
                            workspace="isolated",
                        )
                    )
                )
            )
        except BaseException as exc:
            failures.append(exc)
        finally:
            completed.set()

    opener = threading.Thread(target=open_isolated)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(owner.isolated_workspaces, "lock", ObservedLock())
            with publication_lock:
                opener.start()
                assert lock_requested.wait(5)
                assert not completed.is_set()
                # The opener reached the blocked publication lock. It must not
                # dirty authority before acquiring it and block this publisher.
                assert _marker(owner, repository)["state"] == "clean"
                result = owner.workspace_batches.publish(
                    "before-isolated-open",
                    "shared-publisher",
                    (
                        BatchFile(
                            "file.txt",
                            identify_path(repository / "file.txt"),
                            b"published while opening\n",
                            REGULAR_MODE,
                        ),
                    ),
                    ("*",),
                )
                assert result.state == "published"
            opener.join(10)
            assert not opener.is_alive()
        assert failures == []
        assert len(responses) == 1
        workspace = responses[0]["workspace"]
        private = Path(workspace["canonical_path"])
        assert private != repository
        assert (private / "file.txt").read_text() == "published while opening\n"
        assert _marker(owner, repository)["state"] == "clean"
    finally:
        if opener.ident is not None:
            opener.join(10)
        owner.close()
