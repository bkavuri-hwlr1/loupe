"""Trusted group supervision units; real sandbox/daemon flows live in integration."""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from llm_cli.errors import LlmCoordError
from llm_cli.execution import processes
from llm_cli.execution.processes import (
    OUTPUT_BYTES,
    RING_BYTES,
    ManagedProcessManager,
    PreparedProcess,
    ProcessLifecycleError,
    ScreenedRing,
)
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.authority import process_start_identity


def wait_until(check: Callable[[], bool], seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    assert check()


@dataclass
class Environment:
    store: ControlStore
    root: Path
    manager: ManagedProcessManager
    workspace_id: str
    active: list[int]

    def prepare(
        self, script: str, sensitive: tuple[str, ...] = ()
    ) -> Callable[[str], PreparedProcess]:
        def build(process_id: str) -> PreparedProcess:
            directory = self.manager.run_root / process_id
            directory.mkdir(mode=0o700)
            return PreparedProcess(
                process_id=process_id,
                argv=(sys.executable, "-u", "-c", script),
                cwd=self.root,
                env={"PATH": os.environ.get("PATH", os.defpath)},
                run_directory=directory,
                public_command="scripted unit fixture",
                public_cwd=".",
                sensitive_values=sensitive,
            )

        return build

    def start(self, script: str, sensitive: tuple[str, ...] = ()) -> dict[str, Any]:
        return self.manager.start(
            "owner", self.workspace_id, self.prepare(script, sensitive)
        )


@pytest.fixture
def environment(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
) -> Iterator[Environment]:
    root = repository_factory(tmp_path, {"app.py": "VALUE = 1\n"})
    store = ControlStore(tmp_path / "control.sqlite3")
    store.initialize()
    repo = store.register_repository(
        repo_key="a" * 64,
        display_name="managed fixture",
        git_common_dir=str(root / ".git"),
        main_worktree_path=str(root),
        target_ref="refs/heads/main",
        path_case_insensitive=False,
    )
    checkout = store.ensure_checkout(
        repository=repo, canonical_path=str(root), git_common_dir=str(root / ".git")
    )
    workspace = store.ensure_shared_workspace(checkout)
    with store.connection() as con:
        con.execute(
            "UPDATE workspaces SET mode='isolated',kind='linked_worktree' "
            "WHERE workspace_id=?",
            (workspace.workspace_id,),
        )
    store.open_session(
        session_id="owner",
        checkout_id=checkout.checkout_id,
        resume_token_hash=hashlib.sha256(b"owner").hexdigest(),
        provider="fixture",
        model="fixture",
        workspace_id=workspace.workspace_id,
        workspace_mode="isolated",
    )
    with store.connection() as con:
        con.execute("UPDATE sessions SET state='active'")
    active: list[int] = []
    binding: contextvars.ContextVar[int] = contextvars.ContextVar(
        "managed-test-binding"
    )

    @contextmanager
    def activity(workspace_id: str) -> Iterator[None]:
        me = threading.get_ident()
        token = binding.set(me)
        active.append(me)
        try:
            yield
        finally:
            assert binding.get() == threading.get_ident() == me
            binding.reset(token)
            active.remove(me)

    manager = ManagedProcessManager(
        store,
        tmp_path / "managed",
        activity=activity,
        ipc_root=tmp_path / "ipc",
        grace_seconds=0.1,
        profile_id="fixture-profile",
        cleanup_guard=lambda workspace_id: nullcontext(not active),
    )
    env = Environment(store, root, manager, workspace.workspace_id, active)
    yield env
    manager.stop_all()


def test_constructor_does_not_query_unmigrated_database(tmp_path: Path) -> None:
    store = ControlStore(tmp_path / "uninitialized.sqlite3")
    manager = ManagedProcessManager(store, tmp_path / "managed")
    assert manager.blockers("uncreated") == []
    assert not store.path.exists()


def test_stop_all_never_opens_uninitialized_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ControlStore(tmp_path / "uninitialized.sqlite3")
    manager = ManagedProcessManager(store, tmp_path / "managed")

    def refuse_connection() -> None:
        pytest.fail("shutdown must not open an uninitialized process store")

    monkeypatch.setattr(store, "connection", refuse_connection)
    assert manager.stop_all() == []
    assert manager._shutdown
    assert not store.path.exists()


def test_group_identity_and_lifetime_activity(environment: Environment) -> None:
    env = environment
    row = env.start("import time; print('ready', flush=True); time.sleep(30)")
    process_id = row["process_id"]
    assert row["pid"] == row["pgid"] == os.getpgid(row["pid"])
    assert row["start_identity"] == process_start_identity(row["pid"])
    assert "--profile fixture-profile" in row["stop_command"]
    assert process_id in row["stop_command"]
    assert env.active
    assert env.manager.blockers(env.workspace_id)
    wait_until(lambda: "ready" in env.manager.output(process_id, "owner")["output"])
    stopped = env.manager.stop(process_id, "owner")
    assert stopped["state"] == "stopped"
    assert not env.active
    assert not env.manager.blockers(env.workspace_id)
    assert not (env.manager.run_root / process_id).exists()
    assert env.manager.output(process_id, "owner")["output"] == "ready\n"
    assert process_id not in env.manager._live


def test_whole_group_stops_term_ignoring_fork(environment: Environment) -> None:
    env = environment
    marker = env.root / "child-writes"
    script = f"""import os,signal,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child=os.fork()
if child==0:
    while True:
        with open({str(marker)!r},'ab') as f: f.write(b'x')
        time.sleep(.01)
print('ready',flush=True)
while True: time.sleep(1)
"""
    row = env.start(script)
    wait_until(lambda: marker.exists() and marker.stat().st_size > 2)
    before = time.monotonic()
    env.manager.stop(row["process_id"], "owner")
    assert time.monotonic() - before < 4
    size = marker.stat().st_size
    time.sleep(0.1)
    assert marker.stat().st_size == size
    assert not env.active


def test_natural_child_exit_retires_forked_writer_before_activity(
    environment: Environment,
) -> None:
    env = environment
    marker = env.root / "orphan-writes"
    script = f"""import os,time
child=os.fork()
if child:
    os._exit(0)
while True:
    with open({str(marker)!r},'ab') as f: f.write(b'x')
    time.sleep(.01)
"""
    row = env.start(script)
    wait_until(lambda: env.manager.output(row["process_id"])["state"] == "exited")
    assert not env.active
    size = marker.stat().st_size if marker.exists() else 0
    time.sleep(0.1)
    assert (marker.stat().st_size if marker.exists() else 0) == size
    assert not env.manager.blockers(env.workspace_id)


def test_secret_stdout_is_screened_before_durable_storage(
    environment: Environment,
) -> None:
    env = environment
    canary = "managed-output-value-with-no-provider-signature"
    row = env.start(
        f"import time; print({canary!r},flush=True); time.sleep(30)", (canary,)
    )
    process_id = row["process_id"]
    wait_until(lambda: "[private value]" in env.manager.output(process_id)["output"])
    assert canary not in json.dumps(env.manager.output(process_id))
    env.manager.stop(process_id)
    with env.store.connection() as con:
        assert canary not in "\n".join(con.iterdump())
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(env.store.path) + suffix)
        if path.exists():
            assert canary.encode() not in path.read_bytes()


def test_flood_keeps_screened_ring_and_tool_envelope_bounded(
    environment: Environment,
) -> None:
    env = environment
    row = env.start(
        "import sys,time; sys.stdout.write(('\\x01'*500+'\\n')*3000); "
        "sys.stdout.flush(); time.sleep(30)"
    )
    process_id = row["process_id"]
    wait_until(lambda: env.manager.output(process_id)["truncated"])
    value = env.manager.output(process_id)
    assert value["ring_bytes"] <= RING_BYTES
    assert len(value["output"].encode()) <= OUTPUT_BYTES
    assert len(json.dumps(value).encode()) < 64 * 1024
    env.manager.stop(process_id)
    with env.store.connection() as con:
        length = con.execute(
            "SELECT length(CAST(screened_output AS BLOB)) FROM managed_processes "
            "WHERE process_id=?",
            (process_id,),
        ).fetchone()[0]
    assert length <= RING_BYTES


@pytest.mark.parametrize("split", [1, 5, 19])
def test_ring_screens_private_values_split_across_reads(split: int) -> None:
    canary = "opaque-long-private-canary"
    ring = ScreenedRing((canary,))
    encoded = ("before " + canary + " after\n").encode()
    for index in range(0, len(encoded), split):
        ring.feed(encoded[index : index + split])
    assert canary not in ring.text()
    assert "[private value]" in ring.text()


def test_ring_withholds_provider_signature_and_following_private_key_body() -> None:
    secret = "sk-" + "1a" * 20
    ring = ScreenedRing()
    ring.feed(b"useful first line\n")
    for block in (
        secret[:8].encode(),
        secret[8:].encode() + b"\n",
        b"following secret body\n",
    ):
        ring.feed(block)
    ring.finish()
    assert secret not in ring.text()
    assert "following secret body" not in ring.text()
    assert "useful first line" in ring.text()
    assert "withheld" in ring.text()


def test_ring_drops_oversized_partial_line_without_leaking_secret_prefix() -> None:
    ring = ScreenedRing()
    ring.feed(b"sk-" + b"a" * (2 * processes.LINE_BYTES))
    assert ring.partial == b""
    ring.feed(b"\nnext safe line\n")
    ring.finish()
    assert "next safe line" in ring.text()
    assert "sk-" not in ring.text()
    assert "omitted" in ring.text()
    assert ring.truncated


def test_close_reserves_starting_process_and_prevents_late_launch(
    environment: Environment,
) -> None:
    env = environment
    entered, release = threading.Event(), threading.Event()
    marker = env.root / "must-not-start"
    result: list[dict[str, Any]] = []
    base = env.prepare(f"open({str(marker)!r},'w').write('bad')")

    def prepare(process_id: str) -> PreparedProcess:
        entered.set()
        assert release.wait(5)
        return base(process_id)

    starter = threading.Thread(
        target=lambda: result.append(
            env.manager.start("owner", env.workspace_id, prepare)
        )
    )
    starter.start()
    assert entered.wait(5)
    closing: list[list[str]] = []
    closer = threading.Thread(
        target=lambda: closing.append(env.manager.stop_session("owner"))
    )
    closer.start()
    time.sleep(0.05)
    release.set()
    starter.join(5)
    closer.join(5)
    assert not starter.is_alive() and not closer.is_alive()
    assert closing == [[]]
    assert not marker.exists()
    assert not env.active
    with pytest.raises(ProcessLifecycleError, match="closing"):
        env.start("print('never')")


def test_owner_and_shared_mode_are_refused(environment: Environment) -> None:
    env = environment
    with env.store.connection() as con:
        con.execute("UPDATE sessions SET workspace_mode='shared'")
    with pytest.raises(ProcessLifecycleError, match="isolated"):
        env.start("print('never')")
    assert not env.manager.list()


def test_ipc_storage_is_private_and_rejects_symlink_parent(
    environment: Environment,
) -> None:
    env = environment
    directory = env.manager.ipc_directory(env.workspace_id)
    production = ManagedProcessManager(env.store, env.manager.run_root)
    short = production.ipc_directory(env.workspace_id)
    assert len(os.fsencode(short / "server.sock")) < 100
    production.cleanup_workspace(env.workspace_id)
    assert directory.stat().st_mode & 0o077 == 0
    env.manager.cleanup_workspace(env.workspace_id)
    assert not directory.exists()
    target = env.root / "foreign-ipc"
    target.mkdir()
    hostile = ManagedProcessManager(
        env.store, env.manager.run_root, ipc_root=env.root / "hostile-ipc"
    )
    hostile.ipc_root.symlink_to(target, target_is_directory=True)
    with pytest.raises(LlmCoordError):
        hostile.ipc_directory(env.workspace_id)


@pytest.mark.parametrize(
    "position", [processes.LINE_BYTES - 7, processes.LINE_BYTES + 30]
)
def test_private_key_header_in_oversized_line_withholds_following_body(
    position: int,
) -> None:
    ring = ScreenedRing()
    payload = (
        b"x" * position + b"-----BEGIN PRIVATE KEY-----\nprivate-body-after-header\n"
    )
    for index in range(0, len(payload), 13):
        ring.feed(payload[index : index + 13])
    ring.finish()
    assert "private-body-after-header" not in ring.text()
    assert "BEGIN PRIVATE KEY" not in ring.text()
    assert ring.withheld
    assert len(ring.data) <= RING_BYTES
    assert len(ring.partial) <= processes.LINE_BYTES


def test_stop_retires_ipc_only_after_runtime_activity_is_quiet(
    environment: Environment,
) -> None:
    env = environment
    directory = env.manager.ipc_directory(env.workspace_id)
    with env.manager.activity(env.workspace_id):
        env.manager.cleanup_workspace(env.workspace_id)
        assert directory.exists()
    row = env.start("import time; time.sleep(30)")
    env.manager.stop(row["process_id"], "owner")
    assert not directory.exists()
    assert not env.manager._workspaces
    assert not env.manager._sessions


def test_ipc_tombstone_removal_does_not_hold_locks_or_remove_new_client_directory(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment
    directory = env.manager.ipc_directory(env.workspace_id)
    original = processes.shutil.rmtree
    entered = threading.Event()

    def remove(path: Path) -> None:
        if Path(path).name.startswith(".retired-"):

            def client() -> None:
                with env.manager.activity(env.workspace_id):
                    new = env.manager.ipc_directory(env.workspace_id)
                    (new / "new-client").write_text("keep")
                entered.set()

            thread = threading.Thread(target=client)
            thread.start()
            assert entered.wait(2), (
                "cleanup held manager or activity lock while deleting"
            )
            thread.join(2)
        original(path)

    monkeypatch.setattr(processes.shutil, "rmtree", remove)
    env.manager.cleanup_workspace(env.workspace_id)
    assert (directory / "new-client").read_text() == "keep"


def _survivor(
    env: Environment,
) -> tuple[subprocess.Popen[bytes], str, threading.Thread]:
    import fcntl

    process_id = processes.new_id("process")
    directory = env.manager.run_root / process_id
    directory.mkdir(mode=0o700)
    ready = directory / "ready"
    original = os.open(os.devnull, os.O_WRONLY)
    status = fcntl.fcntl(original, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(original)
    script = f"import time;open({str(ready)!r},'w').write('ready');time.sleep(30)"
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(Path(processes.__file__).with_name("check_process.py")),
            "--managed",
            str(status),
            "0.1",
            sys.executable,
            "-u",
            "-c",
            script,
        ],
        cwd=env.root,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        pass_fds=(status,),
    )
    os.close(status)
    identity = process_start_identity(process.pid)
    with env.store.connection() as con:
        con.execute(
            "INSERT INTO managed_processes(process_id,session_id,workspace_id,"
            "binding_generation,pid,start_identity,pgid,state,command,cwd,"
            "created_at,updated_at) "
            "VALUES (?,'owner',?,1,?,?,?,'running','survivor','.',1,1)",
            (process_id, env.workspace_id, process.pid, identity, process.pid),
        )
    assert process.stdin is not None
    process.stdin.write(b"S")
    process.stdin.flush()
    wait_until(ready.exists)
    reaper = threading.Thread(target=process.wait, daemon=True)
    reaper.start()
    return process, process_id, reaper


def test_restart_stops_matching_surviving_group(environment: Environment) -> None:
    env = environment
    process, process_id, reaper = _survivor(env)
    recovered = ManagedProcessManager(
        env.store,
        env.manager.run_root,
        ipc_root=env.manager.ipc_root,
        grace_seconds=0.1,
    )
    try:
        assert recovered.recover() == []
        reaper.join(3)
        assert not reaper.is_alive()
        assert process.poll() is not None
        assert recovered.output(process_id)["state"] == "stopped"
        assert not recovered.blockers(env.workspace_id)
        assert not (env.manager.run_root / process_id).exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)
        if process.stdin is not None:
            process.stdin.close()


@pytest.mark.parametrize("change", ["reused", "unverifiable", "other_group"])
def test_restart_never_signals_unverifiable_or_reused_identity(
    environment: Environment, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    env = environment
    process, process_id, reaper = _survivor(env)
    real_identity, real_group = processes.process_start_identity, os.getpgid
    kill = os.killpg
    recovered = ManagedProcessManager(
        env.store,
        env.manager.run_root,
        ipc_root=env.manager.ipc_root,
        grace_seconds=0.1,
        profile_id="fixture-profile",
    )
    calls: list[tuple[int, int]] = []

    def never_signal(pid: int, signum: int) -> None:
        calls.append((pid, signum))
        raise AssertionError("an unverifiable process group was signalled")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(processes.os, "killpg", never_signal)
            if change == "other_group":
                patch.setattr(processes.os, "getpgid", lambda pid: real_group(pid) + 1)
            else:

                def identity(pid: int) -> str:
                    if change == "unverifiable":
                        raise OSError("identity unavailable in fixture")
                    return real_identity(pid) + ":different-process-birth"

                patch.setattr(processes, "process_start_identity", identity)
            notices = recovered.recover()
            assert len(notices) == 1 and process_id in notices[0]
            assert "--profile fixture-profile" in notices[0]
            assert recovered.output(process_id)["state"] == "attention_required"
            assert recovered.blockers(env.workspace_id)
            with pytest.raises(ProcessLifecycleError, match=process_id):
                recovered.stop(process_id, "owner")
            assert calls == []
            assert (env.manager.run_root / process_id).exists()
        # The independently verified fixture-owned survivor is now manually
        # stopped, like an operator resolving the report outside Loupe.
        kill(process.pid, signal.SIGKILL)
        reaper.join(3)
        assert recovered.stop(process_id)["state"] == "stopped"
        assert not recovered.blockers(env.workspace_id)
    finally:
        if process.poll() is None:
            kill(process.pid, signal.SIGKILL)
        process.wait(timeout=3)
        if process.stdin is not None:
            process.stdin.close()


def test_identity_persistence_failure_never_starts_user_executable(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment
    marker = env.root / "uncommitted-writer"
    original = env.manager._persist
    injected = False

    def fail_running(live: Any) -> None:
        nonlocal injected
        if live.record["state"] == "running" and not injected:
            injected = True
            raise sqlite3.OperationalError("injected identity commit failure")
        original(live)

    monkeypatch.setattr(env.manager, "_persist", fail_running)
    with pytest.raises(ProcessLifecycleError):
        env.start(f"open({str(marker)!r},'w').write('must not start')")
    assert injected
    assert not marker.exists()
    assert not env.active
    assert not env.manager.blockers(env.workspace_id)
    assert not list(env.manager.run_root.iterdir())


def test_missing_leader_with_live_child_keeps_fence_and_never_signals(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment
    marker = env.root / "orphan-still-writing"
    script = f"""import os,signal,sys,time
child=os.fork()
if child:
    print(child,flush=True)
    sys.stdin.buffer.read(1)
    os._exit(0)
signal.signal(signal.SIGTERM,signal.SIG_IGN)
while True:
    with open({str(marker)!r},'ab') as f:f.write(b'x')
    time.sleep(.01)
"""
    leader = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        cwd=env.root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert leader.stdout is not None and leader.stdin is not None
    child = int(leader.stdout.readline())
    child_birth = process_start_identity(child)
    birth = process_start_identity(leader.pid)
    process_id = processes.new_id("process")
    with env.store.connection() as con:
        con.execute(
            "INSERT INTO managed_processes(process_id,session_id,workspace_id,"
            "binding_generation,pid,start_identity,pgid,state,command,cwd,"
            "created_at,updated_at) "
            "VALUES (?,'owner',?,1,?,?,?,'running','orphan fixture','.',1,1)",
            (process_id, env.workspace_id, leader.pid, birth, leader.pid),
        )
    wait_until(lambda: marker.exists() and marker.stat().st_size > 2)
    leader.stdin.write(b"X")
    leader.stdin.flush()
    leader.wait(timeout=3)
    recovered = ManagedProcessManager(
        env.store,
        env.manager.run_root,
        ipc_root=env.manager.ipc_root,
        profile_id="fixture-profile",
        grace_seconds=0.1,
    )
    actual_kill = os.killpg
    calls: list[tuple[int, int]] = []

    def forbidden(pgid: int, signum: int) -> None:
        calls.append((pgid, signum))
        raise AssertionError("missing leader cannot authorize a group signal")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(processes.os, "killpg", forbidden)
            notices = recovered.recover()
            assert len(notices) == 1 and process_id in notices[0]
            assert recovered.output(process_id)["state"] == "attention_required"
            assert recovered.blockers(env.workspace_id)
            with pytest.raises(ProcessLifecycleError, match=process_id):
                recovered.stop(process_id)
            size = marker.stat().st_size
            wait_until(lambda: marker.stat().st_size > size)
            assert calls == []
        # Only the independently identified fixture child authorizes this
        # operator-side cleanup. The manager itself never signals the group.
        assert process_start_identity(child) == child_birth
        assert os.getpgid(child) == leader.pid
        actual_kill(leader.pid, signal.SIGKILL)
        assert recovered.stop(process_id)["state"] == "stopped"
        assert not recovered.blockers(env.workspace_id)
    finally:
        with suppress(OSError, LlmCoordError):
            if process_start_identity(child) == child_birth:
                actual_kill(leader.pid, signal.SIGKILL)
        leader.stdin.close()
        leader.stdout.close()


@pytest.mark.parametrize("case", ["nonzero", "malformed", "overflow", "timeout"])
def test_group_probe_uncertainty_is_bounded_and_never_proves_empty(
    environment: Environment, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    env = environment
    source = {
        "nonzero": "raise SystemExit(1)",
        "malformed": "print('unparseable process metadata')",
        "overflow": "import sys;sys.stdout.write('1 S\\n'*300000);sys.stdout.flush()",
        "timeout": "import time;time.sleep(5)",
    }[case]
    probe = env.root / "probe"
    probe.write_text(f"#!{sys.executable}\n{source}\n")
    probe.chmod(0o700)
    monkeypatch.setattr(processes.shutil, "which", lambda *args, **kw: str(probe))
    started = time.monotonic()
    assert env.manager._group_presence(99999999) == "unknown"
    assert time.monotonic() - started < 4


def test_idle_process_and_output_reads_do_not_rewrite_control_database(
    environment: Environment,
) -> None:
    env = environment
    row = env.start("import time;time.sleep(30)")
    connection = env.store.connect()
    try:
        before = connection.execute("PRAGMA data_version").fetchone()[0]
        for _ in range(4):
            assert env.manager.output(row["process_id"])["state"] == "running"
            time.sleep(0.2)
        after = connection.execute("PRAGMA data_version").fetchone()[0]
        assert after == before
    finally:
        connection.close()
        env.manager.stop(row["process_id"])
