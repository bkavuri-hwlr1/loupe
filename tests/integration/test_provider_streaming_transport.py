"""Public output arrives through the real optional SDK's SSE parser."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from test_codex_provider import _install_transport, _store
from test_openai_sdk_transport import _client, _message, _response, httpx

from llm_cli.paths import AppPaths
from llm_cli.providers.codex_provider import CodexProvider
from llm_cli.providers.openai_provider import OpenAIProvider


def _live_events(*, subscription: bool) -> bytes:
    output = _message()
    output["content"][0]["text"] = "Hello world"
    started = copy.deepcopy(output)
    started.update(status="in_progress", content=[])
    completed = _response([output])
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": _response([], status="in_progress")},
        {"type": "response.output_item.added", "output_index": 0, "item": started},
        {
            "type": "response.content_part.added",
            "item_id": output["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        *[
            {
                "type": "response.output_text.delta",
                "item_id": output["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": text,
                "logprobs": [],
            }
            for text in ("Hello", " world")
        ],
        {"type": "response.output_item.done", "output_index": 0, "item": output},
    ]
    if subscription:
        completed["output"] = []
    events.append({"type": "response.completed", "response": completed})
    return "".join(
        f"event: {event['type']}\n"
        f"data: {json.dumps({**event, 'sequence_number': index})}\n\n"
        for index, event in enumerate(events)
    ).encode()


def test_real_openai_sdk_delivers_each_public_delta() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    with _client([_live_events(subscription=False)]) as (client, requests):
        session = OpenAIProvider(client=client).session(system="test", tools=[])
        session.set_event_callback(lambda kind, data: events.append((kind, data)))
        assert session.send_user("hello").text == "Hello world"
        assert requests[0]["reasoning"]["summary"] == "auto"
    assert [data["text"] for _, data in events] == ["Hello", " world"]


def test_real_codex_sdk_delivers_deltas_and_retains_empty_terminal_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths(
        "test",
        tmp_path / "config",
        tmp_path / "data",
        tmp_path / "state",
        tmp_path / "run",
    )
    _store(paths)

    def handle(request: Any) -> Any:
        assert json.loads(request.content)["reasoning"]["summary"] == "auto"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_live_events(subscription=True),
        )

    _install_transport(monkeypatch, handle)
    events: list[tuple[str, dict[str, object]]] = []
    session = CodexProvider(paths=paths).session(system="test", tools=[])
    session.set_event_callback(lambda kind, data: events.append((kind, data)))
    assert session.send_user("hello").text == "Hello world"
    assert [data["text"] for _, data in events] == ["Hello", " world"]
