"""A local, explicitly simulated tour of the terminal interface."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, TextIO

from llm_cli.cli.render import EventRenderer
from llm_cli.cli.terminal import TerminalUI


def demo_events() -> list[dict[str, Any]]:
    def event(kind: str, **payload: Any) -> dict[str, Any]:
        return {"event_type": kind, "payload": {"turn_id": "demo", **payload}}

    reasoning = "I'll read the greeting, update it, then check the result."
    response = "The greeting lives in src/hello.py. I'll update the return value."
    events = [
        event("model.started", model="demo"),
        event("model.turn.started"),
        event("model.reasoning.delta", block_id="reasoning", text=reasoning),
        event("model.reasoning", text=reasoning),
    ]
    events.extend(
        event("model.text.delta", block_id="answer", text=word + " ")
        for word in response.split()
    )
    events.extend(
        [
            event("model.said", text=response + " "),
            event(
                "model.tool_call",
                call_id="read",
                tool="read_file",
                arguments={"path": "src/hello.py"},
            ),
            event(
                "model.tool_result",
                call_id="read",
                tool="read_file",
                content='def greet():\n    return "Hello, world!"\n',
            ),
            event(
                "model.tool_call",
                call_id="edit",
                tool="apply_patch",
                arguments={
                    "patch": "--- a/src/hello.py\n+++ b/src/hello.py\n"
                    "@@ -1,2 +1,2 @@\n def greet():\n"
                    '-    return "Hello, world!"\n'
                    '+    return "Hello from Loupe!"'
                },
            ),
            event(
                "model.tool_result",
                call_id="edit",
                tool="apply_patch",
                content="Updated src/hello.py",
            ),
            event(
                "model.finished",
                tool_calls=2,
                summary="Updated **src/hello.py** with the new greeting:\n\n"
                '```python\ndef greet():\n    return "Hello from Loupe!"\n```\n\n'
                "This is a simulated tour. Your files have not changed.",
                usage={"input_tokens": 1240, "output_tokens": 183},
            ),
            event("execution.published", outcome="demo"),
        ]
    )
    return events


def run_demo(*, stream: TextIO | None = None, plain: bool = False) -> None:
    output = stream if stream is not None else sys.stdout
    ui = TerminalUI(output, plain=plain)
    ui.banner(
        repository=Path.cwd(),
        scopes=["src/"],
        provider="demo",
        model="simulated",
        session_id="preview",
    )
    ui.notice("Interface tour · simulated output · no model connection or file changes")
    ui.user("Update the greeting to say Hello from Loupe!")
    renderer = EventRenderer(output, plain=plain)
    for event in demo_events():
        renderer.render(event)
        if output.isatty():
            time.sleep(0.035)
    renderer.finish()
    ui.notice("Start a conversation: loupe chat --provider codex")


__all__ = ["demo_events", "run_demo"]
