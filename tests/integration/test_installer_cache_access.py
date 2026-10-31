"""Production command, process and configured-check launchers cannot read cache."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest

from llm_cli.execution.checks import CheckRunner, supervised_run
from llm_cli.execution.commands import CommandRunner
from llm_cli.execution.sandbox import available_sandbox
from llm_cli.workspace.installer_cache import DEFAULT_MAX_BYTES, InstallerCache

pytestmark = pytest.mark.skipif(
    available_sandbox() is None, reason="real command sandbox required"
)
_PYTHON = str(Path(sys.executable).resolve())
_PROBE = """from pathlib import Path
import sys
root=Path(sys.argv[1])
for operation in (lambda: (root/'artifact').read_text(),
                  lambda: (root/'artifact').write_text('must not overwrite')):
    try:
        operation()
    except OSError:
        pass
    else:
        raise AssertionError('persistent installer cache access allowed')
print('cache read and write denied')
"""


def _artifact(tmp_path: Path) -> tuple[Path, Path]:
    profile = tmp_path / "profile"
    store = InstallerCache(profile / "installer-cache", DEFAULT_MAX_BYTES)
    with store.activity(()) as selected:
        (selected / "artifact").write_text("not-a-secret-public-test-artifact")
    return profile, selected


def _checker(profile: Path) -> CheckRunner:
    # Exercise the production configured-check launch point directly; no fake
    # sandbox/provider and no signature/value-based output filtering can hide
    # an access that unexpectedly succeeds.
    checker = object.__new__(CheckRunner)
    checker.store = SimpleNamespace(path=profile / "control.sqlite3")
    checker.cancelled = lambda: False
    checker.sensitive_values = ()
    checker.suppress_output = False
    checker.emit = lambda *_: None
    return checker


def test_configured_check_cannot_read_or_write_existing_cache_but_can_write_elsewhere(
    tmp_path: Path,
) -> None:
    profile, selected = _artifact(tmp_path)
    outside = tmp_path / "ordinary-check-output"
    code = _PROBE + "Path(sys.argv[2]).write_text('ordinary check write allowed')\n"
    result = _checker(profile)._command(
        [_PYTHON, "-c", code, str(selected), str(outside)],
        tmp_path,
        dict(os.environ),
        time.monotonic() + 10,
        "cache-check",
        4096,
    )
    assert result[0] == 0, result
    assert "cache read and write denied" in result[1]
    assert outside.read_text() == "ordinary check write allowed"
    assert (selected / "artifact").read_text() == "not-a-secret-public-test-artifact"


@pytest.mark.parametrize("mode", ["run_command", "start_process"])
def test_model_launchers_cannot_read_or_write_cache(
    tmp_path: Path, repository_factory: Callable[..., Path], mode: str
) -> None:
    profile, selected = _artifact(tmp_path)
    root = repository_factory(tmp_path / "checkout", {"a.txt": "source\n"})
    kind = available_sandbox()
    assert kind is not None
    runner = CommandRunner(
        root=root,
        snapshot_root=tmp_path / "commands",
        candidates=lambda: (),
        cancelled=lambda: False,
        emit=lambda *_: None,
        lock=RLock(),
        sandbox=kind,
        protected=(profile,),
    )
    argv = [_PYTHON, "-c", _PROBE, str(selected)]
    if mode == "run_command":
        result = runner.run(argv, ".", 10)
        assert not result.is_error, result.content
        output = result.content
    else:
        prepared = runner.prepare_process(
            argv, ".", process_id="cache-process", run_root=tmp_path / "processes"
        )
        result = supervised_run(
            list(prepared.argv),
            cwd=prepared.cwd,
            env=prepared.env,
            deadline=time.monotonic() + 10,
            remaining=4096,
            cancelled=lambda: False,
            on_output=lambda _: None,
            reaper=prepared.reaper,
        )
        assert result[0] == 0, result
        output = result[1]
    assert "cache read and write denied" in output
    assert (selected / "artifact").read_text() == "not-a-secret-public-test-artifact"


def test_empty_cache_off_checks_retain_legacy_launch_when_no_sandbox_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.execution import sandbox

    profile = tmp_path / "profile"
    monkeypatch.setattr(sandbox, "available_sandbox", lambda: None)
    result = _checker(profile)._command(
        [_PYTHON, "-c", "print('legacy check')"],
        tmp_path,
        {},
        time.monotonic() + 10,
        "legacy",
        4096,
    )
    assert result[0] == 0 and result[1] == "legacy check\n"


def test_existing_cache_without_working_sandbox_refuses_with_exact_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.execution import sandbox

    profile, _ = _artifact(tmp_path)
    monkeypatch.setattr(sandbox, "available_sandbox", lambda: None)
    with pytest.raises(RuntimeError, match="loupe workspace cache prune --all"):
        _checker(profile)._command(
            [_PYTHON, "-c", "raise SystemExit(99)"],
            tmp_path,
            {},
            time.monotonic() + 10,
            "refused",
            4096,
        )


def test_check_started_before_first_install_cannot_read_later_cache_artifact(
    tmp_path: Path,
) -> None:
    from llm_cli.workspace.installer_cache import register_guard

    profile = tmp_path / "profile"
    root = profile / "installer-cache"
    release_guard = register_guard(root, enabled=True)
    started, release, selected_file = (
        tmp_path / name for name in ("started", "release", "selected")
    )
    script = """from pathlib import Path
import sys,time
Path(sys.argv[1]).touch()
while not Path(sys.argv[2]).exists(): time.sleep(.01)
root=Path(Path(sys.argv[3]).read_text())
try: (root/'artifact').read_text()
except OSError: print('later cache denied')
else: raise AssertionError('later cache was readable')
"""
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            check = pool.submit(
                _checker(profile)._command,
                [_PYTHON, "-c", script, str(started), str(release), str(selected_file)],
                tmp_path,
                dict(os.environ),
                time.monotonic() + 10,
                "before-install",
                4096,
            )
            try:
                limit = time.monotonic() + 5
                while not started.exists():
                    assert not check.done(), check.result()
                    assert time.monotonic() < limit
                    time.sleep(0.01)
                with InstallerCache(root, DEFAULT_MAX_BYTES).activity(()) as selected:
                    (selected / "artifact").write_text("created after check launch")
                selected_file.write_text(str(selected))
            finally:
                release.touch()
            result = check.result(timeout=10)
            assert result[0] == 0 and result[1] == "later cache denied\n", result
    finally:
        release_guard()


def test_slow_cache_prune_does_not_hold_daemon_dispatch_lock(
    tmp_path: Path,
    service_factory: Callable[..., object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from threading import Event

    from test_shared_agent_execution import _request

    from llm_cli.workspace import installer_cache

    entered, release = Event(), Event()

    def delayed(*_: object) -> dict[str, object]:
        entered.set()
        assert release.wait(10)
        return {"state": "pruned", "pruned": True}

    async def scenario() -> None:
        service = service_factory(tmp_path)
        service.initialize()
        pruning = None
        try:
            monkeypatch.setattr(installer_cache, "prune_profile_cache", delayed)
            pruning = asyncio.create_task(
                service.handle(_request("workspace.cache.prune", {"all": True}))
            )
            assert await asyncio.to_thread(entered.wait, 5)
            assert (
                await asyncio.wait_for(service.handle(_request("repo.list", {})), 1)
                == []
            )
            assert not pruning.done()
        finally:
            release.set()
            if pruning is not None:
                await pruning
            service.close()

    asyncio.run(scenario())
