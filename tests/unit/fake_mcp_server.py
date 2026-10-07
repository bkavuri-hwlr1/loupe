"""A scripted MCP server for tests, speaking newline-delimited JSON-RPC.

FAKE_MCP_MODE changes its behaviour: "crash" exits at once, "silent" never
answers, and "old" offers an unsupported protocol version. FAKE_MCP_PID_FILE
names a file to record the server's process ID in.
"""

from __future__ import annotations

import json
import os
import sys
import time

MODE = os.environ.get("FAKE_MCP_MODE", "")
PAGES = [
    [
        {
            "name": "echo",
            "description": "Echo the arguments.",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        },
        {"name": "fail", "description": "Report a tool error."},
        {"name": "slow", "description": "Take five seconds."},
    ],
    [
        {"name": "secret", "description": "Return a credential."},
        {"name": "env", "description": "Describe the environment."},
        {"name": "roots", "description": "Ask the client for its roots."},
        {"name": "big", "description": "Return a lot of text."},
        {"name": "image", "description": "Return an image."},
        {"name": "bad name!", "description": "Not a valid tool name."},
        {
            "name": "huge",
            "inputSchema": {"type": "object", "description": "x" * 20_000},
        },
    ],
]


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def read() -> dict | None:
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def text(value: str, **extra: object) -> dict:
    return {"content": [{"type": "text", "text": value}], **extra}


def call(name: str, arguments: dict) -> dict:
    if name == "echo":
        return text(json.dumps(arguments, sort_keys=True))
    if name == "fail":
        return text("it broke", isError=True)
    if name == "slow":
        time.sleep(5)
        return text("finally")
    if name == "secret":
        return text("key: AKIA" + "ABCDEFGHIJKLMNOP")
    if name == "env":
        return text(json.dumps({"keys": sorted(os.environ), "cwd": os.getcwd()}))
    if name == "roots":
        # A request to the client, with a notification first; the client
        # declares no capabilities, so it should decline.
        send({"jsonrpc": "2.0", "method": "notifications/message", "params": {}})
        send({"jsonrpc": "2.0", "id": "roots-1", "method": "roots/list"})
        while True:
            reply = read()
            if reply is None or reply.get("id") == "roots-1":
                return text(json.dumps(reply))
    if name == "big":
        return text("x" * 100_000)
    if name == "image":
        return {"content": [{"type": "image", "data": "AAAA", "mimeType": "image/png"}]}
    raise KeyError(name)


def main() -> None:
    if MODE == "crash":
        sys.exit(3)
    if pid_file := os.environ.get("FAKE_MCP_PID_FILE"):
        with open(pid_file, "w") as handle:
            handle.write(str(os.getpid()))
    while (message := read()) is not None:
        method, request_id = message.get("method"), message.get("id")
        if request_id is None or MODE == "silent":
            continue
        if method == "initialize":
            version = "1999-01-01" if MODE == "old" else "2025-06-18"
            result: dict = {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
            }
        elif method == "tools/list":
            page = int(message.get("params", {}).get("cursor") or 0)
            result = {"tools": PAGES[page]}
            if page + 1 < len(PAGES):
                result["nextCursor"] = str(page + 1)
        elif method == "tools/call":
            params = message["params"]
            result = call(params["name"], params.get("arguments", {}))
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )
            continue
        send({"jsonrpc": "2.0", "id": request_id, "result": result})


if __name__ == "__main__":
    main()
