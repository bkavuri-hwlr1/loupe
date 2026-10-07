"""A minimal Model Context Protocol client over stdio.

Loupe uses only MCP tools: the client performs the initialize handshake, lists
tools, and calls them. It declares no client capabilities, so requests a server
sends to the client (sampling, roots, elicitation) are declined; notifications
are ignored. Messages are newline-delimited JSON-RPC 2.0, as the stdio
transport specifies.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import signal
import subprocess
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, Any

from llm_cli import __version__

PROTOCOL_VERSION = "2025-06-18"
# Versions whose tool messages this client understands.
_SUPPORTED_VERSIONS = frozenset(
    {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
)
_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
_MAX_TOOL_PAGES = 20
_EXIT_GRACE_SECONDS = 2.0


class McpError(Exception):
    """A server could not start, timed out, exited, or broke the protocol.

    ``kind`` is one of "start", "timeout", "exited", "protocol", or "error"
    (the server answered with a JSON-RPC error). Only ``kind`` is safe to
    record; the message may quote the server.
    """

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class _Pending:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.message: dict[str, Any] | None = None
        self.failure: McpError | None = None


class StdioClient:
    """One running MCP server, driven over its standard input and output."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
    ) -> None:
        self._command = tuple(command)
        self._env = dict(env)
        self._cwd = cwd
        self._process: subprocess.Popen[bytes] | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        self._threads: list[threading.Thread] = []
        self.server_info: dict[str, Any] = {}

    def start(self, timeout: float) -> None:
        """Launch the server and complete the initialize handshake."""

        try:
            self._process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
                cwd=self._cwd,
                # Its own process group, so closing it also ends its children.
                start_new_session=True,
            )
        except OSError as exc:
            raise McpError(
                f"could not start: {exc.strerror or exc}", kind="start"
            ) from exc
        assert self._process.stdout is not None and self._process.stderr is not None
        for target, stream in (
            (self._read_messages, self._process.stdout),
            (self._drain_stderr, self._process.stderr),
        ):
            thread = threading.Thread(target=target, args=(stream,), daemon=True)
            thread.start()
            self._threads.append(thread)
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "loupe", "version": __version__},
            },
            timeout,
        )
        version = result.get("protocolVersion")
        if version not in _SUPPORTED_VERSIONS:
            raise McpError(f"unsupported protocol version {version!r}", kind="protocol")
        info = result.get("serverInfo")
        self.server_info = info if isinstance(info, dict) else {}
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self, timeout: float) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_TOOL_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = self.request("tools/list", params, timeout)
            page = result.get("tools")
            if not isinstance(page, list):
                raise McpError("tools/list returned no tool list", kind="protocol")
            tools.extend(tool for tool in page if isinstance(tool, dict))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return tools
            cursor = next_cursor
        return tools

    def call_tool(
        self, name: str, arguments: Mapping[str, object], timeout: float
    ) -> dict[str, Any]:
        return self.request(
            "tools/call", {"name": name, "arguments": dict(arguments)}, timeout
        )

    def request(
        self, method: str, params: Mapping[str, object], timeout: float
    ) -> dict[str, Any]:
        pending = _Pending()
        with self._lock:
            if self._closed:
                raise McpError("the server has exited", kind="exited")
            request_id = next(self._ids)
            self._pending[request_id] = pending
        try:
            self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            if not pending.done.wait(timeout):
                with contextlib.suppress(McpError):
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/cancelled",
                            "params": {"requestId": request_id, "reason": "timeout"},
                        }
                    )
                raise McpError(f"{method} timed out after {timeout:g}s", kind="timeout")
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        if pending.failure is not None:
            raise pending.failure
        message = pending.message or {}
        error = message.get("error")
        if error is not None:
            text = error.get("message") if isinstance(error, dict) else None
            raise McpError(str(text or "the server returned an error"), kind="error")
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpError(f"{method} returned no result", kind="protocol")
        return result

    def close(self) -> None:
        with self._lock:
            self._closed = True
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()
        try:
            process.wait(_EXIT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, sig)
                try:
                    process.wait(_EXIT_GRACE_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    continue
        for thread in self._threads:
            thread.join(_EXIT_GRACE_SECONDS)
        self._fail_pending(McpError("the server has exited", kind="exited"))

    def _send(self, message: Mapping[str, object]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpError("the server is not running", kind="exited")
        data = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        with self._write_lock:
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                raise McpError("the server has exited", kind="exited") from exc

    def _read_messages(self, stream: IO[bytes]) -> None:
        try:
            while True:
                line = stream.readline(_MAX_MESSAGE_BYTES + 1)
                if not line:
                    break
                if len(line) > _MAX_MESSAGE_BYTES:
                    self._fail_pending(
                        McpError(
                            "the server sent an oversized message", kind="protocol"
                        )
                    )
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # Stray output is not a message.
                if isinstance(message, dict):
                    self._dispatch(message)
        except (OSError, ValueError):
            pass
        with self._lock:
            self._closed = True
        self._fail_pending(McpError("the server has exited", kind="exited"))

    def _dispatch(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if isinstance(method, str):
            if request_id is None:
                return  # A notification: logging, progress, list changes.
            # A request from the server. Loupe answers pings and declines the
            # rest, since it declared no client capabilities.
            response: dict[str, object] = {"jsonrpc": "2.0", "id": request_id}
            if method == "ping":
                response["result"] = {}
            else:
                response["error"] = {"code": -32601, "message": "Method not found"}
            with contextlib.suppress(McpError):
                self._send(response)
            return
        if type(request_id) is not int:
            return
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is not None:
            pending.message = message
            pending.done.set()

    @staticmethod
    def _drain_stderr(stream: IO[bytes]) -> None:
        # Read so the server never blocks on a full pipe. Its logs may quote
        # anything, so they are discarded rather than recorded.
        with contextlib.suppress(OSError, ValueError):
            for _ in iter(lambda: stream.read(4096), b""):
                pass

    def _fail_pending(self, failure: McpError) -> None:
        with self._lock:
            waiting = list(self._pending.values())
        for pending in waiting:
            if not pending.done.is_set():
                pending.failure = failure
                pending.done.set()


__all__ = ["PROTOCOL_VERSION", "McpError", "StdioClient"]
