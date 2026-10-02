"""CLI-side daemon connection and bounded automatic startup."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import subprocess
import sys
import time
from collections.abc import Coroutine, Iterator, Mapping
from pathlib import Path
from typing import Any

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.ids import new_id
from llm_cli.paths import AppPaths, make_private_file
from llm_cli.protocol.envelopes import Request, Response
from llm_cli.protocol.framing import read_frame, write_frame


def _stream_step[T](
    loop: asyncio.AbstractEventLoop, operation: Coroutine[Any, Any, T]
) -> T:
    task = loop.create_task(operation)
    try:
        return loop.run_until_complete(task)
    except BaseException:
        # run_until_complete leaves its task alive when a signal interrupts
        # the loop. Settle it before closing the socket or losing the handle.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            loop.run_until_complete(task)
        raise


class DaemonClient:
    def __init__(self, paths: AppPaths, *, timeout_seconds: float = 5.0) -> None:
        self.paths = paths
        self.timeout_seconds = timeout_seconds

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        autostart: bool = True,
        idempotency_key: str | None = None,
    ) -> Any:
        request = Request.create(
            request_id=new_id("req"),
            method=method,
            params=params,
            profile_id=self.paths.profile_id,
            idempotency_key=idempotency_key,
        )
        try:
            response = asyncio.run(self._call_once(request))
        except (asyncio.IncompleteReadError, TimeoutError) as exc:
            # The daemon may have committed the mutation before its response
            # was lost. Report uncertainty without issuing the request again.
            raise LlmCoordError(
                ErrorCode.DAEMON_UNAVAILABLE,
                "the daemon response was interrupted or timed out; "
                "the request outcome may be unknown",
            ) from exc
        except (
            FileNotFoundError,
            ConnectionRefusedError,
            ConnectionResetError,
            OSError,
        ) as exc:
            if not autostart:
                raise LlmCoordError(
                    ErrorCode.DAEMON_UNAVAILABLE, "the local daemon is not running"
                ) from exc
            self._start_and_wait()
            try:
                response = asyncio.run(self._call_once(request))
            except (asyncio.IncompleteReadError, OSError) as retry_exc:
                raise LlmCoordError(
                    ErrorCode.DAEMON_UNAVAILABLE,
                    "the daemon request did not finish after startup; "
                    "its outcome may be unknown; "
                    f"see {self.paths.log_file}",
                ) from retry_exc
        if not response.ok:
            error = response.error or {}
            raise LlmCoordError(
                _error_code(error.get("code")),
                str(error.get("message", "daemon request failed")),
                error.get("details")
                if isinstance(error.get("details"), dict)
                else None,
            )
        return response.result

    def stream(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield durable events as the daemon writes them, then stop.

        The loop is driven one frame at a time rather than with ``asyncio.run``
        so this stays an ordinary generator: the caller renders each event as
        it arrives without the CLI having to become async throughout.
        """

        request = Request.create(
            request_id=new_id("req"),
            method=method,
            params=params,
            profile_id=self.paths.profile_id,
        )
        loop = asyncio.new_event_loop()
        try:
            reader, writer = _stream_step(loop, self._open_stream(request))
            try:
                while True:
                    frame = _stream_step(loop, read_frame(reader))
                    if frame.get("stream"):
                        event = frame.get("event")
                        if isinstance(event, dict):
                            yield event
                        continue
                    response = Response.from_dict(frame)
                    if not response.ok:
                        error = response.error or {}
                        raise LlmCoordError(
                            _error_code(error.get("code")),
                            str(error.get("message", "the event stream failed")),
                        )
                    return
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    _stream_step(loop, writer.wait_closed())
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise LlmCoordError(
                ErrorCode.DAEMON_UNAVAILABLE, "the local daemon is not running"
            ) from exc
        except (asyncio.IncompleteReadError, OSError) as exc:
            raise LlmCoordError(
                ErrorCode.DAEMON_UNAVAILABLE,
                "the connection to the local daemon was interrupted; "
                "attach again to resume the task's output",
            ) from exc
        finally:
            loop.close()

    async def _open_stream(
        self, request: Request
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self.paths.socket), self.timeout_seconds
        )
        await asyncio.wait_for(
            write_frame(writer, request.to_dict()), self.timeout_seconds
        )
        return reader, writer

    async def _call_once(self, request: Request) -> Response:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self.paths.socket), self.timeout_seconds
        )
        try:
            await asyncio.wait_for(
                write_frame(writer, request.to_dict()), self.timeout_seconds
            )
            raw = await asyncio.wait_for(read_frame(reader), self.timeout_seconds)
            response = Response.from_dict(raw)
            if response.request_id != request.request_id:
                raise LlmCoordError(
                    ErrorCode.PROTOCOL_MISMATCH,
                    "daemon response request ID does not match",
                )
            return response
        finally:
            writer.close()
            await writer.wait_closed()

    def wait_until_stopped(self, timeout_seconds: float) -> bool:
        """Wait until no daemon process owns this profile's singleton lock.

        A stopping daemon first drains running tasks. Starting a successor
        before it exits is refused by the lock, so restart must wait here.
        """

        deadline = time.monotonic() + timeout_seconds
        while _lock_held(self.paths.lock_file):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def _start_and_wait(self) -> None:
        self.paths.ensure()
        if not self.wait_until_stopped(self.timeout_seconds):
            raise LlmCoordError(
                ErrorCode.DAEMON_UNAVAILABLE,
                "the Loupe background service is still stopping; try again in "
                f"a moment. If this persists, see {self.paths.log_file}",
            )
        log = self.paths.log_file.open("ab", buffering=0)
        make_private_file(self.paths.log_file)
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    # Ignore the repository, PYTHONPATH, and user site packages
                    # when resolving the installed (including editable) daemon.
                    "-I",
                    "-u",
                    "-m",
                    "llm_cli.daemon.main",
                    "--profile",
                    self.paths.profile_id,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                close_fds=True,
                start_new_session=True,
                cwd=self.paths.runtime_dir,
                env=_daemon_environment(),
            )
        finally:
            log.close()
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if self.paths.socket.exists():
                try:
                    result = asyncio.run(
                        self._call_once(
                            Request.create(
                                request_id=new_id("ready"),
                                method="system.ping",
                                params={},
                                profile_id=self.paths.profile_id,
                            )
                        )
                    )
                    if result.ok:
                        return
                except (TimeoutError, OSError):
                    pass
            time.sleep(0.05)
        # Nothing was sent to a daemon, so the request outcome is not unknown.
        raise LlmCoordError(
            ErrorCode.DAEMON_UNAVAILABLE,
            f"the Loupe background service did not start; see {self.paths.log_file}",
        )


def _lock_held(path: Path) -> bool:
    """Report whether a live process holds the daemon singleton lock."""

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _daemon_environment() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    # The child runs outside the repository; retain the meaning of supported
    # relative profile paths before changing its working directory.
    for name in (
        "LLM_COORD_CONFIG_HOME",
        "LLM_COORD_DATA_HOME",
        "LLM_COORD_STATE_HOME",
        "LLM_COORD_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "GIT_CONFIG_GLOBAL",
    ):
        if env.get(name):
            env[name] = os.path.abspath(os.path.expanduser(env[name]))
    return env


__all__ = ["DaemonClient"]


def _error_code(raw: object) -> ErrorCode:
    if not isinstance(raw, str):
        return ErrorCode.INTERNAL_RECOVERABLE
    try:
        return ErrorCode(raw)
    except ValueError:
        return ErrorCode.INTERNAL_RECOVERABLE
