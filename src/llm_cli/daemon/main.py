"""Singleton local daemon entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import signal
import time
from pathlib import Path
from types import FrameType
from typing import IO

from llm_cli import PROTOCOL_VERSION, __version__
from llm_cli.build import code_identity
from llm_cli.config.loader import load_settings
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import LlmCoordError
from llm_cli.ids import new_id
from llm_cli.observability.logging import JsonLogger
from llm_cli.paths import AppPaths, make_private_file
from llm_cli.protocol.server import RpcServer


class SingletonLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: IO[str] | None = None

    def acquire(self) -> None:
        self.handle = self.path.open("a+", encoding="utf-8")
        make_private_file(self.path)
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("another daemon owns this profile") from exc

    def close(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


async def run_daemon(paths: AppPaths) -> int:
    paths.ensure()
    logger = JsonLogger(paths.log_file)
    lock = SingletonLock(paths.lock_file)
    try:
        lock.acquire()
    except RuntimeError as exc:
        logger.write("daemon.singleton_rejected", reason=str(exc))
        return 73

    shutdown = asyncio.Event()
    boot_id = new_id("boot")
    # Snapshot the sources this daemon loaded; later edits must not change it.
    identity = code_identity()
    settings = load_settings(paths.config_file, profile_id=paths.profile_id)
    service = DaemonService(paths, settings, shutdown, boot_id=boot_id)
    server = RpcServer(paths.socket, paths.profile_id, service.handle)
    reconcile_task: asyncio.Task[None] | None = None

    def request_shutdown(
        _signal: int | None = None, _frame: FrameType | None = None
    ) -> None:
        shutdown.set()

    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, request_shutdown)

    _write_pid_file(paths.pid_file, boot_id)
    status = 0
    try:
        service.initialize()
        recovery = service.startup_recovery
        if recovery is not None and recovery.outcomes:
            logger.write(
                "daemon.recovered_executions",
                boot_id=boot_id,
                summary=recovery.summary,
                blocking_task_ids=sorted(
                    {outcome.task_id for outcome in recovery.blocking}
                ),
            )
        await server.start()
        reconcile_task = asyncio.create_task(
            service.reconcile_loop(), name="claim-reconciler"
        )
        logger.write(
            "daemon.ready",
            boot_id=boot_id,
            pid=os.getpid(),
            protocol_version=PROTOCOL_VERSION,
            version=__version__,
            code_path=identity["path"],
            code_fingerprint=identity["fingerprint"],
        )
        await server.serve(shutdown)
        return 0
    except LlmCoordError as exc:
        logger.write(
            "daemon.failed",
            exception_type=type(exc).__name__,
            code=exc.code.value,
            detail=exc.message,
        )
        status = 70
        return status
    except Exception as exc:
        logger.write(
            "daemon.failed",
            exception_type=type(exc).__name__,
            detail=str(exc)[:500],
        )
        status = 70
        return status
    finally:
        if reconcile_task is not None:
            reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reconcile_task
        await server.close()
        # Workers run on threads that outlive their awaiting task, so they are
        # waited for rather than cancelled. Anything still running when the
        # budget expires is resolved by the next boot's recovery pass.
        abandoned = await service.drain()
        if abandoned:
            logger.write(
                "daemon.abandoned_executions",
                boot_id=boot_id,
                executions=[
                    {"task_id": task_id, "attempt": attempt}
                    for task_id, attempt in abandoned
                ],
            )
        service.close()
        _safe_unlink(paths.pid_file)
        if abandoned:
            logger.write("daemon.stopped", boot_id=boot_id)
            # An abandoned worker keeps running on its thread: it would keep
            # writing task state after this daemon reported stopping, even
            # beside a successor. Boot recovery resumes it from durable records,
            # so end the process now. The kernel releases the singleton lock
            # only once none of those threads can run.
            os._exit(status)
        lock.close()
        logger.write("daemon.stopped", boot_id=boot_id)


def _write_pid_file(path: Path, boot_id: str) -> None:
    payload = {
        "pid": os.getpid(),
        "boot_id": boot_id,
        "process_start_ms": time.time_ns() // 1_000_000,
        "version": __version__,
        "protocol_version": PROTOCOL_VERSION,
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _safe_unlink(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-coordd")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> None:
    arguments = build_parser().parse_args(argv)
    paths = AppPaths.resolve(arguments.profile)
    raise SystemExit(asyncio.run(run_daemon(paths)))


if __name__ == "__main__":
    main()
