"""Watchers narrow real daemon scans without letting any change be skipped."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from llm_cli.daemon.service import DaemonService
from llm_cli.workspace import external, isolated
from llm_cli.workspace.context import SharedCoordinationContext
from llm_cli.workspace.identity import read_identified_path

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" and not sys.platform.startswith("linux"),
    reason="FSEvents or inotify is required",
)

FILES = {f"src/{number:03}.py": f"value = {number}\n" for number in range(120)}


async def _rpc(service: DaemonService, method: str, **params: Any) -> Any:
    from llm_cli.protocol.envelopes import Request

    return await service.handle(
        Request.create(
            request_id="watched-" + method.replace(".", "-"),
            method=method,
            params=params,
            profile_id="test",
        )
    )


async def _open(
    service: DaemonService, repository: Path, session_id: str, workspace: str
) -> dict[str, Any]:
    service.providers.register(
        "unused-scripted",
        lambda model: SimpleNamespace(name="unused-scripted", model=model),
    )
    await _rpc(service, "repo.add", path=str(repository))
    opened: dict[str, Any] = await _rpc(
        service,
        "session.open",
        path=str(repository),
        session_id=session_id,
        resume_token_hash=hashlib.sha256(session_id.encode()).hexdigest(),
        provider="unused-scripted",
        model="unused",
        workspace=workspace,
    )
    await _rpc(
        service,
        "session.ack",
        session_id=session_id,
        resume_secret=session_id,
        sequence=opened["bootstrap_sequence"],
    )
    return opened


def _isolated(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service: DaemonService,
) -> tuple[Path, Path, Callable[[], Any]]:
    repository = repository_factory(tmp_path, FILES).resolve()
    service.initialize()
    opened = asyncio.run(_open(service, repository, "isolated", "isolated"))
    workspace_id = opened["session"]["workspace_id"]
    service.shared_runner._prepare_isolated(workspace_id)

    def synchronize() -> Any:
        return service.shared_runner.isolated_sync.synchronize(workspace_id)

    # The first safe point is a full one and settles the watcher cursor.
    synchronize()
    return repository, Path(opened["workspace"]["canonical_path"]), synchronize


def _count(
    monkeypatch: pytest.MonkeyPatch, owner: Any, name: str
) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []
    original = getattr(owner, name)

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, counted)
    return calls


def test_safe_point_rereads_only_what_the_watcher_names(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_factory(tmp_path)
    try:
        repository, private, synchronize = _isolated(
            tmp_path, repository_factory, service
        )
        listed = _count(monkeypatch, isolated, "_source_paths")
        read = _count(monkeypatch, isolated, "_capture_path")
        (repository / "src/007.py").write_text("value = 'edited'\n")
        report = synchronize()
        assert report.applied == ("src/007.py",)
        assert (private / "src/007.py").read_text() == "value = 'edited'\n"
        # No source listing and one read, instead of 120 metadata checks.
        assert listed == []
        assert [args[1] for args in read] == ["src/007.py"]
    finally:
        service.close()


def test_saves_creations_and_deletions_reach_the_private_worktree(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    service = service_factory(tmp_path)
    try:
        repository, private, synchronize = _isolated(
            tmp_path, repository_factory, service
        )
        temporary = repository / "src" / ".001.py.swp"
        temporary.write_text("value = 'saved atomically'\n")
        temporary.replace(repository / "src/001.py")
        (repository / "pkg" / "deep").mkdir(parents=True)
        (repository / "pkg/deep/new.py").write_text("created = True\n")
        (repository / "src/002.py").unlink()
        report = synchronize()
        assert set(report.applied) >= {"src/001.py", "pkg/deep/new.py", "src/002.py"}
        assert (private / "src/001.py").read_text() == "value = 'saved atomically'\n"
        assert (private / "pkg/deep/new.py").read_text() == "created = True\n"
        assert not (private / "src/002.py").exists()
        assert not (private / "src/.001.py.swp").exists()
    finally:
        service.close()


def test_git_checkout_and_directory_rename_are_applied_exactly(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    git_run: Callable[..., str],
) -> None:
    service = service_factory(tmp_path)
    try:
        repository, private, synchronize = _isolated(
            tmp_path, repository_factory, service
        )
        git_run(repository, "checkout", "-q", "-b", "other")
        for number in range(0, 40, 2):
            (repository / f"src/{number:03}.py").write_text(f"other = {number}\n")
        for number in range(40, 45):
            (repository / f"src/{number:03}.py").unlink()
        (repository / "gen").mkdir()
        for number in range(5):
            (repository / f"gen/{number}.py").write_text(f"generated = {number}\n")
        git_run(repository, "add", "-A")
        git_run(repository, "commit", "-q", "-m", "other")
        git_run(repository, "checkout", "-q", "main")
        synchronize()
        git_run(repository, "checkout", "-q", "other")
        synchronize()
        (repository / "gen").rename(repository / "generated")
        synchronize()
        tracked = set(git_run(repository, "ls-files").splitlines())
        untracked = {"generated/0.py"} | {f"generated/{n}.py" for n in range(1, 5)}
        for path in tracked | untracked:
            source = repository / path
            if source.exists():
                assert (private / path).read_bytes() == source.read_bytes(), path
        assert not (private / "gen").exists()
        assert not (private / "src/040.py").exists()
    finally:
        service.close()


def test_lost_events_resync_in_full_and_tell_every_session(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    service = service_factory(tmp_path)
    try:
        repository, private, synchronize = _isolated(
            tmp_path, repository_factory, service
        )
        # A missed event that nothing reports: the narrowed scan trusts the
        # watcher, which is why every loss has to be reported.
        service.watchers._record = lambda *args, **kwargs: None  # type: ignore[method-assign]
        (repository / "src/011.py").write_text("value = 'missed'\n")
        assert "src/011.py" not in synchronize().applied
        del service.watchers._record
        service.watchers.inject_loss(repository, "forced overflow")
        assert synchronize().applied == ("src/011.py",)
        assert (private / "src/011.py").read_text() == "value = 'missed'\n"

        asyncio.run(service._watched_changes())
        with service.store.connection() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM checkout_events "
                "WHERE event_type='checkout.resync_required'"
            ).fetchall()
        assert [json.loads(row[0]) for row in rows] == [
            {"code": "CONTEXT_RESYNC_REQUIRED", "reason": "forced overflow"}
        ]
        packet = SharedCoordinationContext(
            service.store, "isolated", ("*",), scan_external=False
        )(None)
        assert packet.text is not None
        assert json.loads(packet.text)["context_resync"]["code"] == (
            "CONTEXT_RESYNC_REQUIRED"
        )
    finally:
        service.close()


def _shared(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service: DaemonService,
    *,
    repository: Path | None = None,
) -> Path:
    if repository is None:
        repository = repository_factory(tmp_path, FILES).resolve()
    service.initialize()
    asyncio.run(_open(service, repository, "reader", "shared"))
    for path in FILES:
        identity, _ = read_identified_path(repository / path)
        service.store.record_workspace_read(
            session_id="reader", relative_path=path, identity=identity
        )
    return repository


def test_outside_scan_checks_only_changed_reads(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = service_factory(tmp_path)
    try:
        repository = _shared(tmp_path, repository_factory, service)
        scanner = service._external_scanner
        assert scanner.scan_session("reader") == 0
        stamped = _count(monkeypatch, external, "metadata")
        (repository / "src/005.py").write_text("value = 'outside'\n")
        assert scanner.scan_session("reader") == 1
        assert [args[1] for args in stamped] == ["src/005.py"]
        assert scanner.scan_session("reader") == 0
        assert [args[1] for args in stamped] == ["src/005.py"]
    finally:
        service.close()


def test_edits_while_the_daemon_is_down_are_found(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    first = service_factory(tmp_path)
    try:
        repository = _shared(tmp_path, repository_factory, first)
        assert first._external_scanner.scan_session("reader") == 0
    finally:
        first.close()
    (repository / "src/009.py").write_text("value = 'while stopped'\n")
    second = service_factory(tmp_path)
    try:
        second.initialize()
        # The new daemon's first scan is a full one. Taking over the checkout
        # can already run it, so count notices rather than this call's result.
        second._external_scanner.scan_session("reader")
        with second.store.connection() as connection:
            notices = connection.execute(
                "SELECT payload_json FROM checkout_events WHERE event_type=?",
                (external.EVENT_TYPE,),
            ).fetchall()
        assert [json.loads(row[0]) for row in notices] == [
            {"path": "src/009.py", "affected_session_id": "reader"}
        ]
    finally:
        second.close()


def test_watchers_off_keeps_the_periodic_full_scans(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = service_factory(tmp_path)
    base.close()
    service = DaemonService(
        base.paths,
        dataclasses.replace(base.settings, workspace_watchers="off"),
        asyncio.Event(),
        boot_id="watchers-off",
        authority_root=tmp_path / "physical-authority" / "v1",
    )
    try:
        assert not service.watchers.enabled
        assert service.shared_runner.watchers is None
        assert service._external_scanner.watchers is None
        repository, _private, synchronize = _isolated(
            tmp_path, repository_factory, service
        )
        listed = _count(monkeypatch, isolated, "_source_paths")
        (repository / "src/004.py").write_text("value = 'periodic'\n")
        assert synchronize().applied == ("src/004.py",)
        assert synchronize().applied == ()
        assert len(listed) == 2
        assert service.watchers.watching() == []
    finally:
        service.close()


def test_constant_file_churn_does_not_starve_reconciliation(
    tmp_path: Path,
    service_factory: Callable[[Path], DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = service_factory(tmp_path)
    base.close()
    service = DaemonService(
        base.paths,
        dataclasses.replace(base.settings, reconciliation_interval_ms=200),
        asyncio.Event(),
        boot_id="watch-churn",
        authority_root=tmp_path / "physical-authority" / "v1",
    )
    service.initialize()
    reconciled: list[bool] = []
    monkeypatch.setattr(
        service.coordinator, "reconcile_expired", lambda: reconciled.append(True)
    )

    async def scenario() -> None:
        loop = asyncio.create_task(service.reconcile_loop())
        give_up = time.monotonic() + 5
        while not reconciled and time.monotonic() < give_up:
            # A watcher reporting changes faster than scans are spaced.
            service._watch_changed(tmp_path)
            await asyncio.sleep(0.01)
        service.shutdown.set()
        await loop

    try:
        asyncio.run(scenario())
        assert reconciled
    finally:
        service.close()


def test_watchers_never_wait_for_the_publication_lock(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    service = service_factory(tmp_path)
    try:
        repository = _shared(tmp_path, repository_factory, service)
        first = service.watchers.changes(repository, None)
        assert first is not None
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with service.workspace_batches.lock:
                held.set()
                release.wait(30)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            assert held.wait(10)
            (repository / "src/003.py").write_text("value = 'during publication'\n")
            changes = service.watchers.changes(repository, first.cursor)
            assert changes is not None and changes.full is None
            assert changes.touches("src/003.py")
        finally:
            release.set()
            holder.join()
    finally:
        service.close()
