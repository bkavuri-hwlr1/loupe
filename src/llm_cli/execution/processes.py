"""Durable, session-owned groups with screened, bounded output.

The trusted launcher is the recorded group leader. User code starts only after
its identity has been committed, and never inherits the launcher's control pipe.
"""

from __future__ import annotations

import builtins
import hashlib
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm_cli.agent.source_policy import ensure_safe_content
from llm_cli.errors import LlmCoordError
from llm_cli.ids import new_id
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.authority import process_start_identity
from llm_cli.workspace.installation import private_directory

RING_BYTES = 64 * 1024
OUTPUT_BYTES = 8 * 1024
LINE_BYTES = 16 * 1024
_ACTIVE_STATES = frozenset({"starting", "running", "stopping", "attention_required"})
_MAX_PROCESSES = 32


class ProcessLifecycleError(ValueError):
    """A managed group cannot safely be started or retired."""


@dataclass(frozen=True, slots=True, repr=False)
class PreparedProcess:
    process_id: str
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    run_directory: Path
    public_command: str
    public_cwd: str
    sensitive_values: tuple[str, ...] = ()
    reaper: bool = False


@dataclass(slots=True)
class ScreenedRing:
    """Only complete screened lines reach the ring or durable storage."""

    sensitive_values: tuple[str, ...] = field(default=(), repr=False)
    limit: int = RING_BYTES
    data: bytes = b""
    partial: bytes = field(default=b"", repr=False)
    truncated: bool = False
    withheld: bool = False
    discarding_line: bool = False
    screening_tail: bytes = field(default=b"", repr=False)
    _private_pattern: re.Pattern[str] | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        values = sorted({v for v in self.sensitive_values if v}, key=len, reverse=True)
        self._private_pattern = (
            re.compile("|".join(map(re.escape, values))) if values else None
        )

    def _append(self, value: str) -> None:
        encoded = value.encode("utf-8")
        combined = self.data + encoded
        if len(combined) > self.limit:
            self.truncated = True
            combined = combined[-self.limit :].decode("utf-8", errors="ignore").encode()
        self.data = combined

    def _line(self, line: bytes) -> None:
        if self.withheld:
            return
        value = line.decode("utf-8", errors="replace")
        try:
            ensure_safe_content(value)
        except LlmCoordError:
            self.withheld = True
            self.truncated = True
            self._append("[Remaining output withheld: recognized secret material.]\n")
            return
        if self._private_pattern is not None:
            value = self._private_pattern.sub("[private value]", value)
        self._append(value)

    def feed(self, block: bytes) -> None:
        if self.withheld:
            return
        for index in range(0, len(block), 4096):
            for part in block[index : index + 4096].splitlines(keepends=True):
                complete = part.endswith((b"\n", b"\r"))
                # Signature spans are bounded to 255 credential characters;
                # retain overlap even while discarding an oversized line. The
                # sentinel prevents an artificial read boundary matching \b.
                scan = self.screening_tail + part
                try:
                    ensure_safe_content(scan + b"A" * 256)
                except LlmCoordError:
                    self.withheld = True
                    self.truncated = True
                    self.partial = b""
                    self.screening_tail = b""
                    self._append(
                        "[Remaining output withheld: recognized secret material.]\n"
                    )
                    return
                self.screening_tail = scan[-512:]
                if self.discarding_line:
                    if complete:
                        self.discarding_line = False
                        self.screening_tail = b""
                        self._append("[Oversized output line omitted.]\n")
                    continue
                if len(self.partial) + len(part) > LINE_BYTES:
                    self.partial = b""
                    self.truncated = True
                    self.discarding_line = not complete
                    if complete:
                        self.screening_tail = b""
                        self._append("[Oversized output line omitted.]\n")
                    continue
                self.partial += part
                if complete:
                    self._line(self.partial)
                    self.partial = b""
                    self.screening_tail = b""

    def finish(self) -> None:
        if self.partial and not self.discarding_line:
            self._line(self.partial)
        self.partial = b""

    def text(self, limit: int | None = None) -> str:
        data = self.data if limit is None else self.data[-limit:]
        return data.decode("utf-8", errors="ignore")


@dataclass(slots=True)
class _Live:
    record: dict[str, Any]
    started: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    stop_requested: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    process: subprocess.Popen[bytes] | None = None
    error: ProcessLifecycleError | None = None
    ring: ScreenedRing | None = None


def stop_command(process_id: str, session_id: str, profile_id: str = "default") -> str:
    return shlex.join(
        [
            "loupe",
            *(["--profile", profile_id] if profile_id != "default" else []),
            "workspace",
            "process",
            "stop",
            process_id,
            "--session",
            session_id,
        ]
    )


class ManagedProcessManager:
    """Own groups independently of model turns; no dispatch-path queries."""

    def __init__(
        self,
        store: ControlStore,
        run_root: Path,
        *,
        activity: Callable[[str], AbstractContextManager[object]] | None = None,
        pin: Callable[[str], AbstractContextManager[object]] | None = None,
        admit: Callable[[str, str], None] | None = None,
        ipc_root: Path | None = None,
        grace_seconds: float = 1.0,
        profile_id: str = "default",
        cleanup_allowed: Callable[[str], bool] | None = None,
        cleanup_guard: Callable[[str], AbstractContextManager[bool]] | None = None,
    ) -> None:
        self.store = store
        self.profile_id = profile_id
        self.cleanup_allowed = cleanup_allowed
        self.cleanup_guard = cleanup_guard
        self.run_root = Path(os.path.abspath(run_root))
        private_directory(self.run_root)
        self.activity, self.pin, self.admit = activity, pin, admit
        self.grace_seconds = max(0.05, min(grace_seconds, 2.0))
        base = Path("/private/tmp") if sys.platform == "darwin" else Path("/tmp")
        self.ipc_root = ipc_root or base / f"loupe-ipc-{os.geteuid()}"
        self._database_key = hashlib.sha256(
            os.fsencode(store.path.resolve())
        ).hexdigest()[:20]
        self._lock = threading.RLock()
        self._persist_lock = threading.Lock()
        self._closing: set[str] = set()
        self._shutdown = False
        self._live: dict[str, _Live] = {}
        self._workspaces: dict[str, set[str]] = {}
        self._sessions: dict[str, set[str]] = {}
        self._load_lock = threading.Lock()
        self._loaded = False

    def _load_active(self) -> None:
        # Called only at startup recovery or an explicit start/stop, never on
        # ordinary dispatch paths. The service constructs us before migration.
        with self._load_lock:
            if self._loaded:
                return
            with self.store.connection() as con:
                rows = con.execute(
                    "SELECT * FROM managed_processes WHERE state IN "
                    "('starting','running','stopping','attention_required')"
                ).fetchall()
            with self._lock:
                for row in rows:
                    if row["process_id"] not in self._live:
                        self._register(_Live(dict(row)))
                self._loaded = True

    def _register(self, live: _Live) -> None:
        key = str(live.record["process_id"])
        self._live[key] = live
        self._workspaces.setdefault(str(live.record["workspace_id"]), set()).add(key)
        self._sessions.setdefault(str(live.record["session_id"]), set()).add(key)

    def _retire(self, live: _Live) -> None:
        with self._lock:
            if live.record["state"] in _ACTIVE_STATES:
                return
            key = live.record["process_id"]
            if self._live.get(key) is live:
                self._live.pop(key)
                for index, owner in (
                    (self._workspaces, str(live.record["workspace_id"])),
                    (self._sessions, str(live.record["session_id"])),
                ):
                    keys = index.get(owner)
                    if keys is not None:
                        keys.discard(key)
                        if not keys:
                            index.pop(owner)
            live.ring = None
            live.process = None
        workspace_id = str(live.record["workspace_id"])
        try:
            self.cleanup_workspace(workspace_id)
        except (OSError, LlmCoordError, ProcessLifecycleError):
            self._change(live, error_code="ipc_cleanup_needed")
        live.finished.set()

    def ipc_directory(self, workspace_id: str) -> Path:
        """Expose only private IPC scratch, never the live source directory."""
        key = hashlib.sha256(workspace_id.encode()).hexdigest()[:20]
        directory = self.ipc_root / self._database_key / key
        with self._lock:
            for candidate in (self.ipc_root, directory.parent, directory):
                private_directory(candidate)
        return directory

    def _public(
        self, record: dict[str, Any], *, output: bool = False
    ) -> dict[str, Any]:
        result = {
            key: record.get(key)
            for key in (
                "process_id",
                "session_id",
                "workspace_id",
                "binding_generation",
                "pid",
                "start_identity",
                "pgid",
                "state",
                "command",
                "cwd",
                "exit_code",
                "created_at",
                "updated_at",
                "error_code",
            )
        }
        result["stop_command"] = stop_command(
            record["process_id"], record["session_id"], self.profile_id
        )
        if output:
            body = str(record.get("screened_output", "")).encode()
            result["output"] = body[-OUTPUT_BYTES:].decode("utf-8", errors="ignore")
            result["truncated"] = (
                bool(record.get("truncated")) or len(body) > OUTPUT_BYTES
            )
            result["ring_bytes"] = len(body)
            result["ring_limit_bytes"] = RING_BYTES
        return result

    def blockers(self, workspace_id: str) -> builtins.list[dict[str, Any]]:
        with self._lock:
            return [
                self._public(live.record)
                for key in self._workspaces.get(workspace_id, ())
                if (live := self._live[key]).record["state"] in _ACTIVE_STATES
            ]

    def list(
        self,
        session_id: str | None = None,
        workspace_id: str | None = None,
        *,
        include_terminal: bool = False,
    ) -> builtins.list[dict[str, Any]]:
        clauses, values = [], []
        if session_id is not None:
            clauses.append("session_id=?")
            values.append(session_id)
        if workspace_id is not None:
            clauses.append("workspace_id=?")
            values.append(workspace_id)
        if not include_terminal:
            clauses.append(
                "state IN ('starting','running','stopping','attention_required')"
            )
        query = "SELECT * FROM managed_processes"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at,process_id LIMIT 256"
        with self.store.connection() as con:
            return [self._public(dict(row)) for row in con.execute(query, values)]

    def _find(self, process_id: str, session_id: str | None) -> _Live:
        with self._lock:
            live = self._live.get(process_id)
            if live is None:
                with self.store.connection() as con:
                    row = con.execute(
                        "SELECT * FROM managed_processes WHERE process_id=?",
                        (process_id,),
                    ).fetchone()
                if row is None:
                    raise ProcessLifecycleError("managed process was not found")
                live = _Live(dict(row))
                if live.record["state"] not in _ACTIVE_STATES:
                    live.finished.set()
                if live.record["state"] in _ACTIVE_STATES:
                    self._register(live)
            if session_id is not None and live.record["session_id"] != session_id:
                raise ProcessLifecycleError(
                    "managed process belongs to another session"
                )
            return live

    def _persist(self, live: _Live) -> None:
        with self._persist_lock:
            with self._lock:
                record = dict(live.record)
            with self.store.connection() as con:
                con.execute(
                    "UPDATE managed_processes SET pid=?,start_identity=?,pgid=?,"
                    "state=?,"
                    "command=?,cwd=?,exit_code=?,screened_output=?,truncated=?,"
                    "error_code=?,updated_at=? WHERE process_id=?",
                    (
                        *(
                            record.get(key)
                            for key in (
                                "pid",
                                "start_identity",
                                "pgid",
                                "state",
                                "command",
                                "cwd",
                                "exit_code",
                                "screened_output",
                                "truncated",
                                "error_code",
                                "updated_at",
                            )
                        ),
                        record["process_id"],
                    ),
                )

    def _change(self, live: _Live, **values: Any) -> None:
        with self._lock:
            live.record.update(values)
            live.record["updated_at"] = self.store._now(None)
        self._persist(live)

    def _owner(self, session_id: str, workspace_id: str) -> int:
        session = self.store.get_session(session_id)
        workspace = self.store.get_workspace(workspace_id)
        if (
            session is None
            or session.state != "active"
            or session.workspace_mode != "isolated"
            or session.workspace_id != workspace_id
            or workspace is None
            or workspace.mode != "isolated"
            or workspace.state != "active"
        ):
            raise ProcessLifecycleError(
                "managed processes require an active isolated session"
            )
        if self.admit is not None:
            self.admit(session_id, workspace_id)
        return session.workspace_binding_generation

    def start(
        self,
        session_id: str,
        workspace_id: str,
        prepare: Callable[[str], PreparedProcess],
    ) -> dict[str, Any]:
        self._load_active()
        process_id = new_id("process")
        generation = self._owner(session_id, workspace_id)
        with self._lock:
            if self._shutdown or session_id in self._closing:
                raise ProcessLifecycleError(
                    "the session is closing; no managed process may start"
                )
            active = [
                v for v in self._live.values() if v.record["state"] in _ACTIVE_STATES
            ]
            if len(active) >= _MAX_PROCESSES:
                names = "; ".join(
                    stop_command(
                        v.record["process_id"], v.record["session_id"], self.profile_id
                    )
                    for v in active
                )
                raise ProcessLifecycleError(
                    "managed process limit reached; stop one: " + names
                )
            now = self.store._now(None)
            record = dict(
                process_id=process_id,
                session_id=session_id,
                workspace_id=workspace_id,
                binding_generation=generation,
                pid=None,
                start_identity=None,
                pgid=None,
                state="starting",
                command="",
                cwd=".",
                exit_code=None,
                screened_output="",
                truncated=0,
                error_code=None,
                created_at=now,
                updated_at=now,
            )
            live = _Live(record)
            self._register(live)
        try:
            with self.store.connection() as con:
                con.execute(
                    "INSERT INTO managed_processes(process_id,session_id,workspace_id,"
                    "binding_generation,state,command,cwd,screened_output,"
                    "truncated,created_at,updated_at) "
                    "VALUES (?,?,?,?,'starting','','.','',0,?,?)",
                    (process_id, session_id, workspace_id, generation, now, now),
                )
        except Exception:
            with self._lock:
                self._live.pop(process_id, None)
                self._workspaces.get(workspace_id, set()).discard(process_id)
                self._sessions.get(session_id, set()).discard(process_id)
            raise
        with self._lock:
            if (
                live.stop_requested.is_set()
                or session_id in self._closing
                or self._shutdown
            ):
                live.record["state"] = "stopped"
                cancelled = True
            else:
                cancelled = False
                live.thread = threading.Thread(
                    target=self._run,
                    args=(live, prepare),
                    name="loupe-managed-" + process_id,
                    daemon=True,
                )
                live.thread.start()
        if cancelled:
            self._persist(live)
            live.started.set()
            live.finished.set()
        if not live.started.wait(10):
            live.stop_requested.set()
            raise ProcessLifecycleError(
                self._blocked(live, "process startup is still settling")
            )
        if live.error is not None:
            raise live.error
        with self._lock:
            return self._public(live.record)

    def _blocked(self, live: _Live, reason: str) -> str:
        command = stop_command(
            live.record["process_id"], live.record["session_id"], self.profile_id
        )
        return (
            f"Managed process {live.record['process_id']}: {reason}. "
            f"Retry `{command}`. If identity remains unverifiable, inspect and "
            "end the original process manually; Loupe will not signal an "
            "unrelated or reused PID."
        )

    def _group_presence(self, pgid: int) -> str:
        """Bounded non-signalling group probe, never used by hot dispatch."""
        executable = shutil.which("ps", path=os.defpath)
        if executable is None or pgid <= 1:
            return "unknown"
        process: subprocess.Popen[bytes] | None = None
        selector = selectors.DefaultSelector()
        data = bytearray()
        try:
            process = subprocess.Popen(
                [executable, "-axo", "pgid=,stat="],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"PATH": os.defpath, "LC_ALL": "C", "LANG": "C"},
            )
            assert process.stdout is not None
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 1.0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "unknown"
                for key, _ in selector.select(min(remaining, 0.1)):
                    block = os.read(key.fd, 4096)
                    if not block:
                        selector.unregister(key.fileobj)
                    else:
                        data.extend(block)
                        if len(data) > 1024 * 1024:
                            return "unknown"
            if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                return "unknown"
            for line in bytes(data).splitlines():
                if not line.strip():
                    continue
                match = re.fullmatch(rb"\s*(\d+)\s+([A-Za-z+<>?=-]+)\s*", line)
                if match is None:
                    return "unknown"
                if int(match[1]) == pgid and not match[2].startswith(b"Z"):
                    return "present"
            return "absent"
        except (OSError, subprocess.SubprocessError, ValueError):
            return "unknown"
        finally:
            selector.close()
            if process is not None:
                if process.poll() is None:
                    with suppress(OSError):
                        process.kill()
                with suppress(subprocess.SubprocessError):
                    process.wait(timeout=1)
                if process.stdout is not None:
                    process.stdout.close()

    def _identity(self, live: _Live) -> str:
        record = live.record
        pid, pgid = record.get("pid"), record.get("pgid")
        if pid is None:
            # The durable GO handshake forbids user code before running state.
            return "absent" if record["state"] == "starting" else "unverifiable"
        if not isinstance(pid, int) or pid <= 1 or pgid != pid:
            return "unverifiable"
        try:
            current_group = os.getpgid(pid)
        except ProcessLookupError:
            return (
                "absent" if self._group_presence(pgid) == "absent" else "unverifiable"
            )
        except OSError:
            return "unverifiable"
        try:
            identity = process_start_identity(pid)
        except (OSError, ValueError, LlmCoordError):
            try:
                os.getpgid(pid)
            except ProcessLookupError:
                return (
                    "absent"
                    if self._group_presence(pgid) == "absent"
                    else "unverifiable"
                )
            except OSError:
                pass
            return "unverifiable"
        if identity != record.get("start_identity") or current_group != pgid:
            return "unverifiable"
        return "matching"

    def _signal(self, live: _Live, signum: int) -> bool:
        if self._identity(live) != "matching":
            return False
        try:
            os.killpg(int(live.record["pgid"]), signum)
        except OSError:
            return False
        return True

    def _terminate(self, live: _Live) -> bool:
        status = self._identity(live)
        if status == "absent":
            return True
        if status != "matching":
            return False
        self._signal(live, signal.SIGTERM)
        deadline = time.monotonic() + self.grace_seconds + 2.5
        while time.monotonic() < deadline:
            if self._identity(live) == "absent":
                return True
            if live.process is not None and live.process.poll() is not None:
                return self._identity(live) == "absent"
            time.sleep(0.025)
        status = self._identity(live)
        if status == "absent":
            return True
        if status != "matching":
            return False
        self._signal(live, signal.SIGKILL)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if live.process is not None and live.process.poll() is not None:
                return self._identity(live) == "absent"
            if self._identity(live) == "absent":
                return True
            time.sleep(0.025)
        return False

    def _run(self, live: _Live, prepare: Callable[[str], PreparedProcess]) -> None:
        prepared: PreparedProcess | None = None
        writer_fd: int | None = None
        reader_fd: int | None = None
        stack = ExitStack()
        terminal_state = "failed"
        safe = False
        try:
            workspace_id = str(live.record["workspace_id"])
            if self.activity is not None:
                stack.enter_context(self.activity(workspace_id))
            if self.pin is not None:
                stack.enter_context(self.pin(workspace_id))
            generation = self._owner(str(live.record["session_id"]), workspace_id)
            if (
                generation != live.record["binding_generation"]
                or live.stop_requested.is_set()
            ):
                terminal_state, safe = "stopped", True
                return
            prepared = prepare(str(live.record["process_id"]))
            if (
                prepared.process_id != live.record["process_id"]
                or prepared.run_directory != self.run_root / prepared.process_id
            ):
                raise ProcessLifecycleError(
                    "managed process preparation has an invalid owned directory"
                )
            if live.stop_requested.is_set():
                terminal_state, safe = "stopped", True
                self._cleanup(prepared.process_id)
                return
            live.ring = ScreenedRing(prepared.sensitive_values)
            reader_fd, writer_fd = os.pipe()
            # Keep status clear of descriptors 3/4 reserved by Seatbelt.
            import fcntl

            moved = fcntl.fcntl(writer_fd, fcntl.F_DUPFD_CLOEXEC, 10)
            os.close(writer_fd)
            writer_fd = moved
            argv = [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("check_process.py")),
                "--managed",
                str(writer_fd),
                str(self.grace_seconds),
                *(["--sandbox-reaper"] if prepared.reaper else []),
                *prepared.argv,
            ]
            process = subprocess.Popen(
                argv,
                cwd=prepared.cwd,
                env=prepared.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(writer_fd,),
            )
            live.process = process
            os.close(writer_fd)
            writer_fd = None
            identity = process_start_identity(process.pid)
            pgid = os.getpgid(process.pid)
            if pgid != process.pid:
                raise ProcessLifecycleError(
                    "managed process group identity cannot be established"
                )
            self._change(
                live,
                pid=process.pid,
                start_identity=identity,
                pgid=pgid,
                state="running",
                command=prepared.public_command[:512],
                cwd=prepared.public_cwd[:512],
            )
            assert process.stdin is not None
            if not live.stop_requested.is_set():
                process.stdin.write(b"S")
                process.stdin.flush()
            live.started.set()
            completed = self._collect(live, reader_fd)
            reader_fd = None
            if completed is not None:
                terminal_state, safe = completed, True
                self._cleanup(prepared.process_id)
        except Exception:
            live.error = ProcessLifecycleError(
                self._blocked(live, "startup or supervision could not complete safely")
            )
            if live.process is not None:
                if live.process.stdin is not None:
                    with suppress(OSError):
                        live.process.stdin.close()
                safe = self._terminate(live)
                if safe:
                    try:
                        live.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        # This is our unreaped, GO-gated trusted child, not a
                        # recovered PID: user code could not have started.
                        live.process.kill()
                        live.process.wait(timeout=3)
            else:
                safe = True
            if safe:
                terminal_state = "failed"
                with self._lock:
                    live.record["error_code"] = "start_failed"
                if prepared is not None:
                    with suppress(OSError, LlmCoordError, ProcessLifecycleError):
                        self._cleanup(prepared.process_id)
            else:
                self._change(
                    live, state="attention_required", error_code="identity_unverifiable"
                )
        finally:
            if writer_fd is not None:
                with suppress(OSError):
                    os.close(writer_fd)
            if reader_fd is not None:
                with suppress(OSError):
                    os.close(reader_fd)
            if not safe:
                live.started.set()
            # Never release activity/pin for a group whose end is unproved.
            while not safe:
                live.stop_requested.wait(0.2)
                if live.stop_requested.is_set():
                    live.stop_requested.clear()
                    if self._terminate(live):
                        safe = True
                        terminal_state = "stopped"
                        self._cleanup(str(live.record["process_id"]))
            stack.close()
            self._change(live, state=terminal_state)
            self._retire(live)
            live.started.set()

    def _collect(self, live: _Live, status_fd: int) -> str | None:
        process, ring = live.process, live.ring
        assert process is not None and process.stdout is not None and ring is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "output")
        selector.register(status_fd, selectors.EVENT_READ, "status")
        status = b""
        last_flush = time.monotonic()
        stopped = False
        drain_until: float | None = None
        try:
            while selector.get_map() or process.poll() is None:
                if live.stop_requested.is_set() and not stopped:
                    self._change(live, state="stopping")
                    if not self._terminate(live):
                        self._change(
                            live,
                            state="attention_required",
                            error_code="identity_unverifiable",
                        )
                        return None
                    stopped = True
                if process.poll() is not None:
                    drain_until = drain_until or time.monotonic() + 0.2
                if drain_until is not None and time.monotonic() >= drain_until:
                    break
                for key, _ in selector.select(0.05):
                    block = os.read(key.fd, 4096)
                    if not block:
                        selector.unregister(key.fileobj)
                    elif key.data == "output":
                        ring.feed(block)
                    else:
                        status += block[: max(0, 32 - len(status))]
                if time.monotonic() - last_flush >= 0.25:
                    text, truncated = ring.text(), int(ring.truncated)
                    if text != live.record.get(
                        "screened_output", ""
                    ) or truncated != live.record.get("truncated", 0):
                        self._change(live, screened_output=text, truncated=truncated)
                    last_flush = time.monotonic()
            ring.finish()
            code = process.wait(timeout=4)
            if status:
                with suppress(ValueError):
                    code = int(status.decode("ascii").strip())
            if self._group_presence(int(live.record["pgid"])) != "absent":
                self._change(
                    live, state="attention_required", error_code="group_unverifiable"
                )
                return None
            self._change(
                live,
                state="stopping",
                exit_code=code,
                screened_output=ring.text(),
                truncated=int(ring.truncated),
                error_code=None,
            )
            return "stopped" if stopped or live.stop_requested.is_set() else "exited"
        finally:
            selector.close()
            os.close(status_fd)
            if process.stdin is not None:
                with suppress(OSError):
                    process.stdin.close()
            process.stdout.close()

    def output(self, process_id: str, session_id: str | None = None) -> dict[str, Any]:
        live = self._find(process_id, session_id)
        with self._lock:
            record = dict(live.record)
            if live.ring is not None:
                record["screened_output"] = live.ring.text()
                record["truncated"] = int(live.ring.truncated)
            return self._public(record, output=True)

    def stop(self, process_id: str, session_id: str | None = None) -> dict[str, Any]:
        live = self._find(process_id, session_id)
        with self._lock:
            if live.record["state"] not in _ACTIVE_STATES:
                return self._public(live.record)
            live.stop_requested.set()
        if live.thread is None:
            self._recover_one(live)
            if live.record["state"] == "attention_required":
                raise ProcessLifecycleError(
                    self._blocked(live, "identity cannot be verified")
                )
        if not live.finished.wait(4):
            raise ProcessLifecycleError(
                self._blocked(live, "the writer has not been safely stopped")
            )
        with self._lock:
            return self._public(live.record)

    def stop_session(self, session_id: str) -> builtins.list[str]:
        self._load_active()
        with self._lock:
            self._closing.add(session_id)
            ids = [
                key
                for key in self._sessions.get(session_id, ())
                if self._live[key].record["state"] in _ACTIVE_STATES
            ]
        notices = []
        for key in ids:
            try:
                self.stop(key, session_id)
            except ProcessLifecycleError as exc:
                notices.append(str(exc))
        return notices

    def stop_all(self) -> builtins.list[str]:
        # Construction may be used for an uninitialized service's code probe.
        # Synchronize with an in-flight load, without opening the database only
        # to close a manager that has never recovered or admitted a process.
        with self._load_lock, self._lock:
            self._shutdown = True
            if not self._loaded:
                return []
            ids = [
                key
                for key, live in self._live.items()
                if live.record["state"] in _ACTIVE_STATES
            ]
        notices = []
        for key in ids:
            try:
                self.stop(key)
            except ProcessLifecycleError as exc:
                notices.append(str(exc))
        return notices

    def _recover_one(self, live: _Live) -> None:
        if self._identity(live) == "absent":
            self._change(live, state="stopped", error_code=None)
            self._cleanup(str(live.record["process_id"]))
            self._retire(live)
            return
        if self._terminate(live):
            self._change(live, state="stopped", error_code=None)
            self._cleanup(str(live.record["process_id"]))
            self._retire(live)
            return
        self._change(
            live, state="attention_required", error_code="identity_unverifiable"
        )
        # Root restores the cached writer fence before any workspace recovery.
        # Do not enter normal runtime.activity here: it replays runtime journals.

    def recover(self) -> builtins.list[str]:
        self._load_active()
        notices = []
        for live in tuple(self._live.values()):
            if live.record["state"] not in _ACTIVE_STATES:
                continue
            self._recover_one(live)
            if live.record["state"] == "attention_required":
                notices.append(
                    self._blocked(live, "identity cannot be verified after restart")
                )
        return notices

    def _cleanup(self, process_id: str) -> None:
        if not re.fullmatch(r"process_[a-f0-9]+", process_id):
            raise ProcessLifecycleError("managed process directory identity is invalid")
        private_directory(self.run_root)
        directory = self.run_root / process_id
        if directory.is_symlink():
            raise ProcessLifecycleError(
                "managed process directory needs manual inspection"
            )
        if directory.exists():
            private_directory(directory)
            shutil.rmtree(directory)

    def cleanup_workspace(self, workspace_id: str) -> None:
        tombstone: Path | None = None
        with self._lock:
            blockers = self.blockers(workspace_id)
            if blockers:
                raise ProcessLifecycleError(
                    "; ".join(
                        self._blocked(
                            self._live[p["process_id"]], "workspace cleanup is waiting"
                        )
                        for p in blockers
                    )
                )
            if self.cleanup_allowed is not None and not self.cleanup_allowed(
                workspace_id
            ):
                return
            if self.cleanup_guard is None and self.activity is not None:
                # Without an activity gate, a client may still be writing IPC.
                return
            from contextlib import nullcontext

            guard = (
                self.cleanup_guard(workspace_id)
                if self.cleanup_guard
                else nullcontext(True)
            )
            with guard as quiet:
                if not quiet:
                    return
                key = hashlib.sha256(workspace_id.encode()).hexdigest()[:20]
                directory = self.ipc_root / self._database_key / key
                if not directory.exists() and not directory.is_symlink():
                    return
                for parent in (self.ipc_root, directory.parent, directory):
                    private_directory(parent)
                tombstone = Path(
                    tempfile.mkdtemp(prefix=".retired-", dir=directory.parent)
                )
                try:
                    os.replace(directory, tombstone)
                except BaseException:
                    tombstone.rmdir()
                    raise
        # No manager, publication, or activity-counter lock during removal.
        if tombstone is not None:
            shutil.rmtree(tombstone)
