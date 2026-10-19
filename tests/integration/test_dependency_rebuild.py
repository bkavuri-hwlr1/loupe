"""Opt-in private dependency rebuilds, real sandbox execution and durable replay."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from test_isolated_creation_safety import checkout, manager
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.agent.source_policy import contains_secret_material, excluded_paths
from llm_cli.config.models import RebuildConfig
from llm_cli.execution.sandbox import available_sandbox
from llm_cli.providers.base import ToolCallRequest
from llm_cli.workspace.hydration import HydrationBudgets, plan_hydration
from llm_cli.workspace.isolated import copy_runtime_files
from llm_cli.workspace.rebuild import INSTALLER_HOME, JOURNAL, inspect_tree

pytestmark = pytest.mark.skipif(
    available_sandbox() is None, reason="real command sandbox required"
)

PYTHON = str(Path(sys.executable).resolve())

INSTALL = """from pathlib import Path
import sys, venv
venv.EnvBuilder(with_pip=False, symlinks=False).create('.venv')
site = Path('.venv/lib/python%d.%d/site-packages' % sys.version_info[:2])
site.mkdir(parents=True, exist_ok=True)
(site / 'rebuilt_dependency.py').write_text('answer = 42\\n')
"""


def prepare(env: BatchEnvironment, tmp_path: Path, script: str = INSTALL) -> Any:
    (env.repository / ".gitignore").write_text(".venv/\nnode_modules/\n.env\n")
    (env.repository / "install.py").write_text(script)
    isolate = manager(env, tmp_path)
    isolate.rebuild = (RebuildConfig(".venv", (PYTHON, "install.py")),)
    return isolate


@pytest.mark.parametrize("shape", ["missing", "escaping", "oversized"])
def test_rebuild_avoids_primary_copy_and_dependencies_import_in_private_check_snapshot(
    batch_env: BatchEnvironment, tmp_path: Path, shape: str
) -> None:
    env = batch_env
    isolate = prepare(env, tmp_path)
    original = env.repository / ".venv"
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "untouched").write_text("primary external bytes")
    if shape == "escaping":
        original.mkdir()
        (original / "external-store").symlink_to(outside, target_is_directory=True)
    elif shape == "oversized":
        original.mkdir()
        with (original / "too-large").open("wb") as stream:
            stream.truncate(5 * 1024**3 + 1)
    plan = plan_hydration(env.repository, rebuild=isolate.rebuild)
    entry = next(entry for entry in plan.entries if entry.path == ".venv")
    assert plan.complete and entry.action == "rebuild"
    assert not entry.descendants_inventoried and entry.apparent_bytes == 0
    workspace, notices = isolate.open(checkout(env), "rebuild-" + shape)
    assert notices == ("Dependency rebuilt privately: .venv",), notices
    private = Path(workspace.canonical_path)
    assert not (private / ".venv").is_symlink()
    assert (outside / "untouched").read_text() == "primary external bytes"
    assert not original.is_symlink()
    if shape == "escaping":
        assert (original / "external-store").is_symlink()
    if shape == "oversized":
        assert (original / "too-large").stat().st_size == 5 * 1024**3 + 1
    state = isolate.validate(workspace.workspace_id)
    assert ".venv" in state.runtime_files
    assert excluded_paths(private, [".venv/lib/anything"]) == {".venv/lib/anything"}
    check = tmp_path / "check"
    check.mkdir()
    copy_runtime_files(private, check, (".venv",))
    result = subprocess.run(
        [
            str(check / ".venv/bin/python"),
            "-c",
            "import rebuilt_dependency; assert rebuilt_dependency.answer == 42; "
            "print('dependency check passed')",
        ],
        cwd=check,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "dependency check passed\n"
    # C8 refresh must neither copy oversized primary bytes nor replace the
    # rebuilt generation with an external environment.
    assert isolate.runtime.refresh(workspace.workspace_id) == ()
    assert (private / ".venv/bin/python").is_file()


def test_without_setting_no_installer_is_run(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    isolate = prepare(
        env, tmp_path, "from pathlib import Path; Path('.venv/ran').write_text('bad')"
    )
    isolate.rebuild = ()
    workspace, _ = isolate.open(checkout(env), "no-installer-permission")
    assert not (Path(workspace.canonical_path) / ".venv").exists()
    assert not any(
        entry.action == "rebuild" for entry in plan_hydration(env.repository).entries
    )


def test_failed_rebuild_opens_without_partial_tree_and_screens_private_value(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    canary = "private-rebuild-canary-12345"
    assert not contains_secret_material(canary)
    isolate = prepare(
        env,
        tmp_path,
        """from pathlib import Path
Path('.venv/partial').write_text('incomplete')
print('useful installer diagnostic')
print(Path('.env').read_text())
raise SystemExit(7)
""",
    )
    (env.repository / ".env").write_text("MARKER=" + canary)
    workspace, notices = isolate.open(
        checkout(env), "failed-rebuild", approved_secrets=(".env",)
    )
    private = Path(workspace.canonical_path)
    assert not (private / ".venv").exists()
    assert ".venv" not in isolate.validate(workspace.workspace_id).runtime_files
    assert any("Dependency rebuild failed" in note for note in notices)
    assert "useful installer diagnostic" in "\n".join(notices)
    assert "[private value]" in "\n".join(notices)
    assert canary not in repr(notices)
    with env.store.connection() as con:
        database = "\n".join(con.iterdump())
    assert canary not in database
    assert canary not in (Path(workspace.git_dir) / JOURNAL).read_text()
    assert all(
        canary.encode() not in path.read_bytes()
        for path in env.publisher.candidate_dir.iterdir()
        if path.is_file()
    )


@pytest.mark.parametrize(
    "stage",
    [
        "rebuild_prepared",
        "rebuild_removed:.venv",
        "rebuild_running:.venv",
        "rebuild_installed:.venv",
        "rebuild_recorded:.venv",
        "rebuilt",
        "hydrated",
    ],
)
def test_interrupted_rebuild_retries_durably_without_current_configuration(
    batch_env: BatchEnvironment, tmp_path: Path, stage: str
) -> None:
    env = batch_env
    isolate = prepare(env, tmp_path)

    class Crash(BaseException):
        pass

    def interrupt(current: str) -> None:
        if current == stage:
            raise Crash()

    isolate.fault = interrupt
    with pytest.raises(Crash):
        isolate.open(checkout(env), "crash-rebuild")
    with env.store.connection() as con:
        workspace_id = con.execute(
            "SELECT workspace_id FROM isolated_workspaces"
        ).fetchone()[0]
    workspace = env.store.get_workspace(workspace_id)
    assert workspace is not None
    private = Path(workspace.canonical_path)
    if stage == "rebuild_running:.venv":
        (private / ".venv/partial-from-crash").write_text("discard me")
    # Reconfiguration after the crash does not grant a different command.
    restarted = manager(env, tmp_path)
    restarted.recover()
    state = restarted.validate(workspace_id)
    assert state.stage == "ready" and ".venv" in state.runtime_files
    assert not (private / ".venv/partial-from-crash").exists()
    assert (private / ".venv/bin/python").exists()


def test_rebuild_preserves_frozen_secret_consent_across_refresh_and_restart(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    isolate = prepare(env, tmp_path)
    (env.repository / ".env").write_text("MARKER=opening-private-consent")
    workspace, _ = isolate.open(
        checkout(env), "consent-rebuild", approved_secrets=(".env",)
    )
    (env.repository / ".env").write_text("MARKER=next-private-consent")
    restarted = manager(env, tmp_path)
    restarted.recover()
    notes = restarted.runtime.refresh(workspace.workspace_id)
    assert "Runtime refreshed: .env" in notes
    assert (
        Path(workspace.canonical_path) / ".env"
    ).read_text() == "MARKER=next-private-consent"
    with env.store.connection() as con:
        approved = json.loads(
            con.execute(
                "SELECT approved_json FROM isolated_runtime_state WHERE workspace_id=?",
                (workspace.workspace_id,),
            ).fetchone()[0]
        )
    assert approved == [".env"]


def test_many_entry_rebuild_uses_compact_receipt_and_restarts(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    isolate = prepare(
        env,
        tmp_path,
        """from pathlib import Path
for number in range(1500):
    Path('.venv/dependency-%04d.py' % number).write_text('answer = 42\\n')
""",
    )
    workspace, notices = isolate.open(checkout(env), "many-dependencies")
    assert notices == ("Dependency rebuilt privately: .venv",), notices
    journal = Path(workspace.git_dir) / JOURNAL
    assert journal.stat().st_size < 4096
    started = time.monotonic()
    receipt = inspect_tree(Path(workspace.canonical_path), ".venv", validate=True)
    elapsed = time.monotonic() - started
    assert receipt["entries"] == 1501
    assert elapsed < 5
    manager(env, tmp_path).recover()
    assert isolate.validate(workspace.workspace_id).stage == "ready"
    assert isolate.runtime.refresh(workspace.workspace_id) == ()


def test_rebuild_plan_does_not_charge_primary_descendants(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    isolate = prepare(env, tmp_path)
    root = env.repository / ".venv"
    root.mkdir()
    for number in range(40):
        (root / str(number)).write_text("not inspected")
    plan = plan_hydration(
        env.repository,
        rebuild=isolate.rebuild,
        budgets=HydrationBudgets(max_entries=12),
    )
    assert plan.complete
    assert (
        next(entry for entry in plan.entries if entry.path == ".venv").action
        == "rebuild"
    )


@pytest.mark.parametrize(
    "stage", ["rebuild_running:.venv", "rebuild_installed:.venv", "hydrated"]
)
def test_rebuild_recovers_real_physical_authority_dirty_marker(
    batch_env: BatchEnvironment, tmp_path: Path, stage: str
) -> None:
    from llm_cli.workspace.authority import PhysicalAuthority

    env = batch_env
    isolate = prepare(env, tmp_path)
    authority_root = tmp_path / "authority" / "v1"
    authority = PhysicalAuthority(
        env.store,
        "rebuild-owner",
        "before-crash",
        tmp_path / "daemon.sock",
        root=authority_root,
    )
    env.store.authority = authority

    class Crash(BaseException):
        pass

    def interrupt(current: str) -> None:
        if current == stage:
            raise Crash()

    isolate.fault = interrupt
    with pytest.raises(Crash):
        isolate.open(checkout(env), "authority-crash-rebuild")
    with env.store.connection() as con:
        assert (
            con.execute(
                "SELECT state FROM authority_journals WHERE kind='isolated_create'"
            ).fetchone()[0]
            == "applying"
        )
        workspace_id = con.execute(
            "SELECT workspace_id FROM isolated_workspaces"
        ).fetchone()[0]
    authority.close()
    successor = PhysicalAuthority(
        env.store,
        "rebuild-owner",
        "after-crash",
        tmp_path / "daemon.sock",
        root=authority_root,
    )
    env.store.authority = successor
    try:
        restarted = manager(env, tmp_path)
        restarted.recover()
        assert restarted.validate(workspace_id).stage == "ready"
        with successor.pin(env.repository):
            successor.validate_current()
        with env.store.connection() as con:
            assert (
                con.execute(
                    "SELECT state FROM authority_journals WHERE kind='isolated_create'"
                ).fetchone()[0]
                == "confirmed"
            )
    finally:
        successor.close()
        env.store.authority = None


def test_primary_edit_during_rebuild_enters_through_normal_source_sync(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    isolate = prepare(env, tmp_path)

    def edit_primary(stage: str) -> None:
        if stage == "rebuild_running:.venv":
            (env.repository / "a.txt").write_text("primary edit during installer\n")

    isolate.fault = edit_primary
    workspace, notices = isolate.open(checkout(env), "editing-during-rebuild")
    assert notices == ("Dependency rebuilt privately: .venv",), notices
    private = Path(workspace.canonical_path)
    assert (private / "a.txt").read_text() == "old a\n"
    assert (env.repository / "a.txt").read_text() == "primary edit during installer\n"
    assert (private / ".venv/bin/python").exists()
    from llm_cli.workspace.isolated import capture_source
    from llm_cli.workspace.isolated_sync import IsolatedSynchronizer

    state = isolate.validate(workspace.workspace_id)
    sync = IsolatedSynchronizer(
        env.store,
        env.publisher,
        lambda root, protected: capture_source(
            root, env.publisher.candidate_dir, protected_paths=tuple(protected)
        ),
        isolate.validate,
    )
    sync.register(
        workspace.workspace_id,
        state.source_manifest,
        state.source_sequence,
        state.runtime_paths,
    )
    sync.synchronize(workspace.workspace_id)
    assert (private / "a.txt").read_text() == "primary edit during installer\n"


@pytest.mark.usefixtures("isolated_daemon_autostart")
def test_real_daemon_open_waits_for_active_rebuild_policy_with_short_client_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository_factory: Any
) -> None:
    import tempfile

    from llm_cli.cli.session import _runtime_open_client, open_or_resume_session
    from llm_cli.paths import AppPaths
    from llm_cli.protocol.client import DaemonClient

    root = repository_factory(
        tmp_path,
        {
            ".gitignore": ".venv/\n",
            "install.py": "import time\nfrom pathlib import Path\ntime.sleep(.3)\n"
            "Path('.venv/ready').write_text('ready')\n",
        },
    )
    with tempfile.TemporaryDirectory(prefix="lc-rebuild-", dir="/tmp") as runtime:
        for name, value in {
            "LLM_COORD_CONFIG_HOME": tmp_path / "config",
            "LLM_COORD_DATA_HOME": tmp_path / "data",
            "LLM_COORD_STATE_HOME": tmp_path / "state",
            "LLM_COORD_RUNTIME_DIR": Path(runtime).resolve(),
        }.items():
            monkeypatch.setenv(name, str(value))
        paths = AppPaths.resolve("rebuild-test")
        paths.config_file.parent.mkdir(parents=True)
        paths.config_file.write_text(
            '[isolated.rebuild]\n".venv" = ' + json.dumps([PYTHON, "install.py"]) + "\n"
        )
        setup = DaemonClient(paths, timeout_seconds=30)
        try:
            setup.call("repo.add", {"path": str(root)})
            short = DaemonClient(paths, timeout_seconds=0.1)
            assert short.timeout_seconds == 0.1
            # Saved config is now pending restart. Active policy still owns the
            # long opening budget, even though disk no longer requests a build.
            paths.config_file.write_text("")
            assert _runtime_open_client(short, "isolated").timeout_seconds >= 180
            started = time.monotonic()
            credentials = open_or_resume_session(
                short,
                repository=root,
                provider="anthropic",
                model=None,
                resume_session_id=None,
                workspace_mode="isolated",
            )
            assert time.monotonic() - started > 0.3
            assert credentials.workspace_mode == "isolated"
            assert credentials.workspace_notices == (
                "Dependency rebuilt privately: .venv",
            )
            assert short.timeout_seconds == 0.1
        finally:
            setup.call("system.shutdown", autostart=False)
            assert setup.wait_until_stopped(15)


def test_task_checks_rebuilt_dependency_and_apply_keeps_frozen_generation(
    tmp_path: Path, repository_factory: Any, service_factory: Any
) -> None:
    import asyncio
    import hashlib

    from test_shared_agent_execution import (
        ScriptedProvider,
        _call,
        _finish,
        _register,
        _request,
        _start,
        _tools,
    )

    async def scenario() -> None:
        root = repository_factory(
            tmp_path,
            {"a.py": "value = 1\n", ".gitignore": ".venv/\n", "install.py": INSTALL},
        )
        service = service_factory(tmp_path)
        service.isolated_workspaces.rebuild = (
            RebuildConfig(".venv", (PYTHON, "install.py")),
        )
        provider = ScriptedProvider(
            "rebuild-required",
            [
                _tools(
                    _call("read_file", path="a.py"),
                    _call("write_file", path="a.py", content="value = 2\n"),
                ),
                _tools(
                    ToolCallRequest(
                        "dependency-check", "run_check", {"name": "dependency"}
                    )
                ),
                _finish(),
            ],
        )
        _register(service, provider)
        service.initialize()
        credentials = {
            "session_id": "rebuild-required",
            "resume_secret": "rebuild-session-secret",
        }
        try:
            await service.handle(_request("repo.add", {"path": str(root)}))
            await service.handle(
                _request(
                    "checks.configure",
                    {
                        "path": str(root),
                        "config": {
                            "checks": {
                                "dependency": {
                                    "argv": [
                                        ".venv/bin/python",
                                        "-c",
                                        "import rebuilt_dependency; "
                                        "assert rebuilt_dependency.answer == 42; "
                                        "print('rebuilt dependency check passed')",
                                    ],
                                    "required": True,
                                }
                            }
                        },
                    },
                )
            )
            opened = await service.handle(
                _request(
                    "session.open",
                    {
                        "path": str(root),
                        "session_id": credentials["session_id"],
                        "resume_token_hash": hashlib.sha256(
                            credentials["resume_secret"].encode()
                        ).hexdigest(),
                        "provider": "scripted",
                        "model": provider.model,
                        "workspace": "isolated",
                        "mode": "normal",
                    },
                )
            )
            await service.handle(
                _request(
                    "session.ack",
                    {**credentials, "sequence": opened["bootstrap_sequence"]},
                )
            )
            task = await _start(service, root, credentials, "rebuild-task", ("a.py",))
            await asyncio.wait_for(task, 30)
            workflow = service.workflow.get("rebuild-task")
            assert workflow is not None and workflow["status"] == "awaiting_review"
            workspace = service.store.get_workspace(opened["session"]["workspace_id"])
            assert workspace is not None
            private = Path(workspace.canonical_path)
            live = next(
                (private / ".venv/lib").glob(
                    "python*/site-packages/rebuilt_dependency.py"
                )
            )
            live.write_text("answer = 99\n")
            # /apply checks use the immutable generation captured by the task,
            # despite later private dependency changes and a primary new tree.
            (root / ".venv").mkdir()
            (root / ".venv/primary-only").write_text("unrelated primary install")
            # Current shared source invalidates the original check evidence.
            # Restart also proves retained rebuilt inputs need no active setting.
            (root / "later_source.py").write_text("unrelated = 1\n")
            service.close()
            service = service_factory(tmp_path)
            service.initialize()
            applied = await service.handle(
                _request("task.apply", {"task_id": "rebuild-task", "path": str(root)})
            )
            assert applied["state"] == "published", applied
            assert (root / "a.py").read_text() == "value = 2\n"
            assert live.read_text() == "answer = 99\n"
            with service.store.connection() as con:
                runs = con.execute(
                    "SELECT state,output FROM check_runs WHERE task_id='rebuild-task'"
                ).fetchall()
            assert len(runs) >= 2 and all(run["state"] == "passed" for run in runs)
            assert all(
                "rebuilt dependency check passed" in run["output"] for run in runs
            )
        finally:
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("edit_source", [False, True])
def test_hard_crash_discards_private_installer_home_before_replay(
    batch_env: BatchEnvironment, tmp_path: Path, edit_source: bool
) -> None:
    env = batch_env
    isolate = prepare(env, tmp_path)
    canary = "orphaned-installer-private-canary"
    assert not contains_secret_material(canary)
    child = os.fork()
    if child == 0:

        def die_with_private_home(stage: str) -> None:
            if stage == "rebuild_running:.venv":
                with env.store.connection() as con:
                    row = con.execute(
                        "SELECT git_dir FROM workspaces WHERE mode='isolated'"
                    ).fetchone()
                (Path(row[0]) / INSTALLER_HOME / ".env").write_text("MARKER=" + canary)
                os._exit(17)

        isolate.fault = die_with_private_home
        isolate.open(checkout(env), "hard-crash-rebuild")
        os._exit(99)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 17
    with env.store.connection() as con:
        row = con.execute(
            "SELECT workspace_id,git_dir FROM workspaces WHERE mode='isolated'"
        ).fetchone()
    workspace_id, git_dir = row
    assert (Path(git_dir) / INSTALLER_HOME / ".env").read_text() == "MARKER=" + canary
    workspace = env.store.get_workspace(workspace_id)
    assert workspace is not None
    private = Path(workspace.canonical_path)
    if edit_source:
        (private / "install.py").write_text("print('preserve late source edit')\n")
    restarted = manager(env, tmp_path)
    restarted.recover()
    assert not (Path(git_dir) / INSTALLER_HOME).exists()
    if edit_source:
        assert restarted.get(workspace_id).stage == "attention"
        assert (
            private / "install.py"
        ).read_text() == "print('preserve late source edit')\n"
        assert not (private / ".venv").exists()
    else:
        restarted.validate(workspace_id)
    assert restarted._clean_incomplete(workspace_id, closed=True)
    assert not Path(git_dir).exists()
    with env.store.connection() as con:
        assert canary not in "\n".join(con.iterdump())
    assert all(
        canary.encode() not in path.read_bytes()
        for path in env.publisher.candidate_dir.iterdir()
        if path.is_file()
    )


def test_changed_ignore_rule_never_imports_primary_over_rebuilt_root(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    isolate = prepare(env, tmp_path)
    (env.repository / ".env").write_text("MARKER=opening-ignore-consent")
    workspace, _ = isolate.open(
        checkout(env), "ignore-changed-rebuild", approved_secrets=(".env",)
    )
    # The runtime exclusion is frozen even if primary stops ignoring this root.
    (env.repository / ".gitignore").write_text(".env\n")
    (env.repository / ".venv").mkdir()
    (env.repository / ".venv/primary").write_text("must never be imported")
    (env.repository / ".env").write_text("MARKER=refreshed-ignore-consent")
    assert "Runtime refreshed: .env" in isolate.runtime.refresh(workspace.workspace_id)
    private = Path(workspace.canonical_path)
    assert (private / ".venv/bin/python").exists()
    assert not (private / ".venv/primary").exists()


def test_recovery_preserves_late_source_edit_without_running_changed_installer(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    isolate = prepare(env, tmp_path)

    class Crash(BaseException):
        pass

    def interrupt(stage: str) -> None:
        if stage == "rebuild_prepared":
            raise Crash()

    isolate.fault = interrupt
    with pytest.raises(Crash):
        isolate.open(checkout(env), "late-source-edit-rebuild")
    with env.store.connection() as con:
        row = con.execute(
            "SELECT workspace_id,canonical_path FROM workspaces WHERE mode='isolated'"
        ).fetchone()
    workspace_id, location = row
    private = Path(location)
    edited = (
        "from pathlib import Path; Path('.venv/should-not-run').write_text('bad')\n"
    )
    (private / "install.py").write_text(edited)
    restarted = manager(env, tmp_path)
    restarted.recover()
    assert restarted.get(workspace_id).stage == "attention"
    assert (private / "install.py").read_text() == edited
    assert not (private / ".venv/should-not-run").exists()


@pytest.mark.parametrize("edit_private", [False, True])
def test_runtime_edits_during_rebuild_use_opening_receipts(
    batch_env: BatchEnvironment, tmp_path: Path, edit_private: bool
) -> None:
    env = batch_env
    isolate = prepare(env, tmp_path)
    (env.repository / ".env").write_text("MARKER=before-installer-runtime")

    def edit_runtime(stage: str) -> None:
        if stage != "rebuild_running:.venv":
            return
        (env.repository / ".env").write_text("MARKER=after-installer-runtime")
        if edit_private:
            with env.store.connection() as con:
                row = con.execute(
                    "SELECT canonical_path FROM workspaces WHERE mode='isolated'"
                ).fetchone()
            (Path(row[0]) / ".env").write_text("MARKER=private-installer-runtime")

    isolate.fault = edit_runtime
    workspace, _ = isolate.open(
        checkout(env), "runtime-edit-install", approved_secrets=(".env",)
    )
    notes = isolate.runtime.refresh(workspace.workspace_id)
    private = Path(workspace.canonical_path)
    if edit_private:
        assert "Runtime conflict; preserved private copy: .env" in notes
        assert (private / ".env").read_text() == "MARKER=private-installer-runtime"
    else:
        assert "Runtime refreshed: .env" in notes
        assert (private / ".env").read_text() == "MARKER=after-installer-runtime"


def test_failure_notice_rescreens_private_values_changed_while_installer_runs(
    batch_env: BatchEnvironment, tmp_path: Path
) -> None:
    env = batch_env
    isolate = prepare(
        env,
        tmp_path,
        "from pathlib import Path; print(Path('.env').read_text()); "
        "raise SystemExit(3)",
    )
    (env.repository / ".env").write_text("MARKER=before-installer-redaction")
    canary = "changed-private-installer-canary"
    assert not contains_secret_material(canary)

    def change_private(stage: str) -> None:
        if stage == "rebuild_running:.venv":
            with env.store.connection() as con:
                row = con.execute(
                    "SELECT canonical_path FROM workspaces WHERE mode='isolated'"
                ).fetchone()
            (Path(row[0]) / ".env").write_text("MARKER=" + canary)

    isolate.fault = change_private
    _, notices = isolate.open(
        checkout(env), "changing-redaction-rebuild", approved_secrets=(".env",)
    )
    assert "[private value]" in "\n".join(notices)
    assert canary not in repr(notices)
    with env.store.connection() as con:
        assert canary not in "\n".join(con.iterdump())
