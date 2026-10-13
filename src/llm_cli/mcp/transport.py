"""Bounded Streamable HTTP for explicitly configured MCP authority.

Requests connect directly to the configured endpoint, with no redirects,
ambient proxies, cookies, or retries of calls that might have side effects.
POST responses may be JSON or SSE; a separate listening GET is optional in
MCP and is not opened. Server requests are answered only for ping.
"""

from __future__ import annotations

import contextlib
import http.client
import itertools
import json
import socket
import ssl
import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

from llm_cli.mcp.client import _MAX_MESSAGE_BYTES, McpClient, McpError

# getaddrinfo has no cancellation API. At most four daemon workers may remain
# resolving after callers time out; subsequent calls wait only their deadline.
_DNS_SLOTS = threading.BoundedSemaphore(4)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise McpError("request timed out", kind="timeout")
    return remaining


def _resolve(host: str, port: int, deadline: float) -> list[tuple[int, int, int, Any]]:
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise McpError("resolution timed out", kind="timeout")
    done = threading.Event()
    addresses: list[tuple[int, int, int, Any]] = []
    failure: list[Exception] = []

    def resolve() -> None:
        try:
            addresses.extend(
                (family, kind, protocol, address)
                for family, kind, protocol, _, address in socket.getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
            )
        except Exception as exc:
            failure.append(exc)
        finally:
            _DNS_SLOTS.release()
            done.set()

    worker = threading.Thread(target=resolve, name="loupe-mcp-dns", daemon=True)
    try:
        worker.start()
    except RuntimeError as exc:
        _DNS_SLOTS.release()
        raise McpError("resolution unavailable", kind="start") from exc
    if not done.wait(_remaining(deadline)):
        raise McpError("resolution timed out", kind="timeout")
    _remaining(deadline)
    if failure or not addresses:
        raise McpError("resolution failed", kind="protocol")
    return addresses


def _connect(host: str, port: int, deadline: float) -> socket.socket:
    for family, kind, protocol, address in _resolve(host, port, deadline):
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(_remaining(deadline))
            connection.connect(address)
            connection.settimeout(_remaining(deadline))
            return connection
        except McpError:
            connection.close()
            raise
        except OSError:
            connection.close()
    _remaining(deadline)
    raise McpError("connection failed", kind="protocol")


class HttpClient(McpClient):
    """One Streamable HTTP session; header values stay in process memory."""

    def __init__(self, url: str, *, headers: Mapping[str, str]) -> None:
        super().__init__()
        self._url = urlsplit(url)
        self._headers = dict(headers)
        self._session_id: str | None = None
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._closed = False
        self._timeout = 30.0

    def start(self, timeout: float) -> None:
        with self._lock:
            self._timeout = timeout
            self._initialize(timeout)

    def _notify(self, method: str) -> None:
        self._exchange({"jsonrpc": "2.0", "method": method}, timeout=self._timeout)

    def request(
        self, method: str, params: Mapping[str, object], timeout: float
    ) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise McpError("the session has closed", kind="exited")
            message = self._exchange(
                {
                    "jsonrpc": "2.0",
                    "id": next(self._ids),
                    "method": method,
                    "params": params,
                },
                timeout=timeout,
            )
            if message is None:
                raise McpError("request returned no response", kind="protocol")
            if message.get("error") is not None:
                # Never quote remote errors; they may echo credentials/headers.
                raise McpError("the server returned an error", kind="error")
            result = message.get("result")
            if not isinstance(result, dict):
                raise McpError("request returned no result", kind="protocol")
            return result

    def close(self) -> None:
        with self._lock:
            if not self._closed and self._session_id is not None:
                with contextlib.suppress(McpError):
                    self._exchange(None, timeout=2.0, verb="DELETE")
            self._closed = True
            self._session_id = None
            self._headers.clear()

    def _exchange(
        self,
        message: Mapping[str, object] | None,
        *,
        timeout: float,
        verb: str = "POST",
    ) -> dict[str, Any] | None:
        if timeout <= 0:
            raise McpError("request timed out", kind="timeout")
        deadline = time.monotonic() + timeout
        body = json.dumps(message, separators=(",", ":")).encode() if message else None
        if body is not None and len(body) > _MAX_MESSAGE_BYTES:
            raise McpError("request is oversized", kind="protocol")
        host = self._url.hostname or ""
        port = self._url.port or (443 if self._url.scheme == "https" else 80)
        connection = http.client.HTTPConnection(
            host, port, timeout=_remaining(deadline)
        )
        headers = {
            **self._headers,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if message is None or message.get("method") != "initialize":
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        path = self._url.path or "/"
        if self._url.query:
            path += "?" + self._url.query
        expired = threading.Event()
        active_socket: socket.socket | None = None

        def expire() -> None:
            expired.set()
            # close() alone does not end a socket retained by HTTPResponse.
            if active_socket is not None:
                with contextlib.suppress(OSError):
                    active_socket.shutdown(socket.SHUT_RDWR)
            connection.close()

        timer = threading.Timer(_remaining(deadline), expire)
        timer.daemon = True
        timer.start()
        try:
            context = (
                ssl.create_default_context() if self._url.scheme == "https" else None
            )
            active_socket = _connect(host, port, deadline)
            connection.sock = active_socket
            if context is not None:
                active_socket.settimeout(_remaining(deadline))
                active_socket = context.wrap_socket(active_socket, server_hostname=host)
                connection.sock = active_socket
            # DNS/connect/TLS may have completed at the deadline. Never start
            # sending a call once it has expired, including a late DNS result.
            active_socket.settimeout(_remaining(deadline))
            if expired.is_set():
                raise McpError("request timed out", kind="timeout")
            connection.request(verb, path, body=body, headers=headers)
            response = connection.getresponse()
            if verb == "DELETE":
                return None
            if response.status == 404 and self._session_id is not None:
                # Establish a new session but do not replay the original call:
                # it may already have caused a side effect before expiration.
                self._session_id = None
                self.start(max(0.001, deadline - time.monotonic()))
                raise McpError(
                    "the session expired; call was not replayed", kind="error"
                )
            if not 200 <= response.status < 300:
                raise McpError("HTTP request failed", kind="error")
            request_id = (
                message.get("id")
                if message is not None and "method" in message
                else None
            )
            if request_id is None:
                if response.status != 202:
                    raise McpError("notification was not accepted", kind="protocol")
                return None
            if message is not None and message.get("method") == "initialize":
                session_id = response.getheader("Mcp-Session-Id")
                if session_id is not None:
                    if (
                        not session_id
                        or len(session_id) > 4_096
                        or not all(0x21 <= ord(char) <= 0x7E for char in session_id)
                    ):
                        raise McpError("invalid session identifier", kind="protocol")
                    self._session_id = session_id
            content_type = response.getheader("Content-Type", "").split(";", 1)[0]
            if content_type == "application/json":
                payload = b"".join(self._chunks(response, active_socket, deadline))
                return self._response(json.loads(payload), request_id)
            if content_type == "text/event-stream":
                return self._sse(response, active_socket, deadline, request_id)
            raise McpError("unexpected HTTP content type", kind="protocol")
        except McpError as exc:
            if expired.is_set():
                raise McpError("request timed out", kind="timeout") from exc
            raise
        except (
            TimeoutError,
            OSError,
            http.client.HTTPException,
            ValueError,
            RecursionError,
        ) as exc:
            kind = (
                "timeout"
                if expired.is_set() or isinstance(exc, TimeoutError)
                else "protocol"
            )
            raise McpError("HTTP transport failed", kind=kind) from exc
        finally:
            timer.cancel()
            connection.close()

    @staticmethod
    def _chunks(
        response: http.client.HTTPResponse,
        active_socket: socket.socket | None,
        deadline: float,
    ) -> Iterator[bytes]:
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError("request timed out", kind="timeout")
            if active_socket is not None:
                active_socket.settimeout(remaining)
            chunk = response.read1(8_192)
            if not chunk:
                return
            total += len(chunk)
            if total > _MAX_MESSAGE_BYTES:
                raise McpError("response is oversized", kind="protocol")
            yield chunk

    def _sse(
        self,
        response: http.client.HTTPResponse,
        active_socket: socket.socket | None,
        deadline: float,
        request_id: object,
    ) -> dict[str, Any]:
        buffer = b""
        data: list[bytes] = []
        for chunk in self._chunks(response, active_socket, deadline):
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if line.startswith(b"data:"):
                    data.append(line[5:].lstrip(b" "))
                elif not line and data:
                    message = json.loads(b"\n".join(data))
                    data.clear()
                    if not isinstance(message, dict):
                        raise McpError("invalid SSE message", kind="protocol")
                    if isinstance(message.get("method"), str):
                        if "id" in message:
                            reply: dict[str, object] = {
                                "jsonrpc": "2.0",
                                "id": message["id"],
                            }
                            if message["method"] == "ping":
                                reply["result"] = {}
                            else:
                                reply["error"] = {
                                    "code": -32601,
                                    "message": "Method not found",
                                }
                            self._exchange(reply, timeout=deadline - time.monotonic())
                        continue
                    if message.get("id") == request_id:
                        return self._response(message, request_id)
        raise McpError("SSE stream ended without a response", kind="protocol")

    @staticmethod
    def _response(message: object, request_id: object) -> dict[str, Any]:
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or type(message.get("id")) is not type(request_id)
            or message.get("id") != request_id
        ):
            raise McpError("invalid JSON-RPC response", kind="protocol")
        return message


__all__ = ["HttpClient"]
