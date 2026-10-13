"""Disposable local Streamable HTTP server for unit and daemon tests."""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class HttpServer:
    url: str = ""
    mode: str = "json"
    messages: list[dict[str, Any]] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)
    sessions: int = 0
    deleted: bool = False


@contextlib.contextmanager
def http_mcp_server(mode: str = "json") -> Iterator[HttpServer]:
    state = HttpServer(mode=mode)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def send(self, payload: bytes, content_type: str = "application/json") -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if self.initializing:
                self.send_header("Mcp-Session-Id", f"session-{state.sessions}")
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(payload)

        def do_DELETE(self) -> None:
            state.headers.append(dict(self.headers))
            state.deleted = True
            self.send_response(204)
            self.end_headers()

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            message = json.loads(body)
            state.messages.append(message)
            state.headers.append(dict(self.headers))
            method = message.get("method")
            self.initializing = method == "initialize"
            if method == "initialize":
                state.sessions += 1
            if "id" not in message or "method" not in message:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if state.mode in {"redirect", "http-error"}:
                self.send_response(302 if state.mode == "redirect" else 403)
                self.send_header("Location", state.url + "/untrusted")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            params = message.get("params", {})
            if (
                state.mode == "expired"
                and method == "tools/call"
                and state.sessions == 1
            ):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "initialize":
                result: dict[str, Any] = {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "http-fake", "version": "1"},
                    "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                }
            elif method == "tools/list":
                result = {
                    "tools": [{"name": "echo", "inputSchema": {"type": "object"}}]
                }
                if state.mode == "schema-secret":
                    result["tools"].append(
                        {
                            "name": "leak",
                            "description": self.headers.get("Authorization", ""),
                        }
                    )
            elif method == "tools/call":
                text = json.dumps(params.get("arguments", {}), sort_keys=True)
                if params["name"] == "header":
                    text = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if params["name"] == "slow":
                    time.sleep(0.3)
                if params["name"] == "drip":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for _ in range(30):
                        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                        time.sleep(0.01)
                    self.close_connection = True
                    return
                result = {"content": [{"type": "text", "text": text}]}
            elif method == "resources/list":
                result = {
                    "resources": [
                        {
                            "uri": "test://second"
                            if params.get("cursor")
                            else "test://guide",
                            "name": "guide",
                            "description": "Reference material",
                        }
                    ]
                }
                if not params.get("cursor"):
                    result["nextCursor"] = "page-2"
            elif method == "resources/read":
                uri = params["uri"]
                result = {
                    "contents": [
                        {"uri": uri, "text": "Guide text"},
                        {
                            "uri": "test://image",
                            "blob": "AAAA",
                            "mimeType": "image/png",
                        },
                    ]
                }
                if uri == "test://secret":
                    result["contents"][0]["text"] = "AKIA" + "ABCDEFGHIJKLMNOP"
                if uri == "test://header":
                    result["contents"][0]["text"] = self.headers.get(
                        "Authorization", ""
                    )
                if uri == "test://big":
                    result["contents"][0]["text"] = "x" * 100_000
                if uri == "test://binary-secret":
                    result["contents"][1]["blob"] = self.headers.get(
                        "Authorization", ""
                    )
            elif method == "prompts/list":
                result = {
                    "prompts": [
                        {
                            "name": "review",
                            "arguments": [{"name": "subject", "required": True}],
                        }
                    ]
                }
                if not params.get("cursor"):
                    result["nextCursor"] = "page-2"
            elif method == "prompts/get":
                text = (
                    f"Review {params.get('arguments', {}).get('subject', 'the code')}"
                )
                if params["name"] == "secret":
                    text = "AKIA" + "ABCDEFGHIJKLMNOP"
                if params["name"] == "header":
                    text = self.headers.get("Authorization", "")
                result = {
                    "messages": [
                        {"role": "user", "content": {"type": "text", "text": text}},
                        {
                            "role": "assistant",
                            "content": {"type": "image", "data": "AAAA"},
                        },
                    ]
                }
            else:
                result = {}
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": message["id"], "result": result}
            ).encode()
            if state.mode == "invalid-json":
                payload = b"not JSON"
            elif state.mode == "wrong-id":
                payload = b'{"jsonrpc":"2.0","id":false,"result":{}}'
            elif state.mode == "oversized":
                payload = b" " * (8 * 1024 * 1024 + 1)
            elif state.mode == "oversized-sse":
                self.send(b":" + b"x" * (8 * 1024 * 1024), "text/event-stream")
                return
            elif state.mode == "sse":
                payload = b": keepalive\r\n\r\n" + b"".join(
                    b"event: message\r\ndata: "
                    + json.dumps(item).encode()
                    + b"\r\n\r\n"
                    for item in (
                        {"jsonrpc": "2.0", "method": "notifications/message"},
                        {"jsonrpc": "2.0", "method": "ping", "id": "ping-1"},
                        {"jsonrpc": "2.0", "method": "roots/list", "id": "roots-1"},
                        json.loads(payload),
                    )
                )
                self.send(payload, "text/event-stream")
                return
            self.send(payload)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    state.url = f"http://127.0.0.1:{httpd.server_port}/mcp"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)
