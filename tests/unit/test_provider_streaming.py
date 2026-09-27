"""Provider streaming preserves useful output without exposing provisional prose."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from llm_cli.agent.driver import RunRequest
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.tools import ToolBroker, ToolOutcome
from llm_cli.errors import LlmCoordError
from llm_cli.providers import anthropic_provider
from llm_cli.providers.anthropic_provider import AnthropicProvider
from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult
from llm_cli.providers.codex_provider import _SubscriptionStream
from llm_cli.providers.openai_provider import OpenAIProvider


class Stream:
    def __init__(
        self, events: list[object], response: object, observe: Callable[[], None]
    ) -> None:
        self.events = events
        self.response = response
        self.observe = observe

    def __iter__(self) -> Iterator[object]:
        for event in self.events:
            yield event
            self.observe()

    def get_final_response(self) -> object:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    get_final_message = get_final_response


class Client:
    def __init__(self, stream: object) -> None:
        self.response_stream = stream
        self.responses = self
        self.messages = self
        self.arguments: dict[str, Any] = {}

    @contextmanager
    def stream(self, **arguments: Any) -> Iterator[object]:
        self.arguments = arguments
        yield self.response_stream


def _message(text: str) -> dict[str, object]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def test_openai_emits_text_summary_and_arguments_before_final_response() -> None:
    seen: list[tuple[str, dict[str, object]]] = []
    response = SimpleNamespace(
        status="completed",
        output=[
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Inspect first."}],
                "encrypted_content": "must-stay-private",
            },
            _message("Hello"),
            {
                "type": "function_call",
                "call_id": "read",
                "name": "read_file",
                "arguments": '{"path":"a.py"}',
            },
        ],
    )
    events = [
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs",
            "summary_index": 0,
            "delta": "Inspect first.",
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg",
            "content_index": 0,
            "delta": "Hello",
        },
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc",
                "call_id": "read",
                "name": "read_file",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc",
            "delta": '{"path":"a.py"}',
        },
        {"type": "response.reasoning_text.delta", "delta": "must-stay-private"},
    ]
    counts: list[int] = []
    stream = Stream(events, response, lambda: counts.append(len(seen)))
    client = Client(stream)
    session = OpenAIProvider(client=client).session(system="test", tools=[])
    session.set_event_callback(lambda kind, data: seen.append((kind, data)))
    turn = session.send_user("inspect")
    assert counts == [1, 2, 2, 3, 3]
    assert [kind for kind, _ in seen] == [
        "model.reasoning.delta",
        "model.text.delta",
        "model.tool.delta",
    ]
    assert seen[-1][1]["call_id"] == "read"
    assert turn.reasoning == "Inspect first."
    assert turn.tool_calls[0].arguments == {"path": "a.py"}
    assert client.arguments["reasoning"]["summary"] == "auto"
    assert "must-stay-private" not in json.dumps(seen)
    assert "must-stay-private" in json.dumps(session.snapshot())


def test_interrupted_openai_stream_keeps_visible_deltas_without_executable_calls() -> (
    None
):
    seen: list[tuple[str, dict[str, object]]] = []
    stream = Stream(
        [{"type": "response.output_text.delta", "delta": "Partial answer"}],
        RuntimeError("Didn't receive a `response.completed` event."),
        lambda: None,
    )
    session = OpenAIProvider(client=Client(stream)).session(system="test", tools=[])
    session.set_event_callback(lambda kind, data: seen.append((kind, data)))
    with pytest.raises(LlmCoordError, match="interrupted"):
        session.send_user("inspect")
    assert seen[0][1]["text"] == "Partial answer"
    assert session.snapshot() == {"input": [{"role": "user", "content": "inspect"}]}


def test_anthropic_stream_exposes_display_thinking_but_never_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anthropic_provider, "_load_sdk", lambda: SimpleNamespace())
    seen: list[tuple[str, dict[str, object]]] = []
    counts: list[int] = []
    events = [
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Inspect."},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "private-signature"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "Hello"},
        },
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "tool_use", "id": "read", "name": "read_file"},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"path":"a.py"}'},
        },
    ]
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="Inspect."),
            SimpleNamespace(type="text", text="Hello"),
        ],
        stop_reason="end_turn",
    )
    client = Client(Stream(events, response, lambda: counts.append(len(seen))))
    session = AnthropicProvider(client=client, fallback_model=None).session(
        system="test", tools=[]
    )
    session.set_event_callback(lambda kind, data: seen.append((kind, data)))
    assert session.send_user("inspect").reasoning == "Inspect."
    assert counts == [1, 1, 2, 2, 3]
    assert seen[-1][1]["call_id"] == "read"
    assert "private-signature" not in json.dumps(seen)


class WireEvent(dict[str, object]):
    def model_dump(self, **kwargs: object) -> dict[str, object]:
        return dict(self)


def test_codex_preserves_live_deltas_and_assembles_empty_terminal_output() -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    def events() -> Iterator[WireEvent]:
        yield WireEvent(type="response.output_text.delta", delta="Hello")
        assert seen[0][1]["text"] == "Hello"
        yield WireEvent(
            type="response.output_item.done", output_index=0, item=_message("Hello")
        )
        yield WireEvent(
            type="response.completed", response={"status": "completed", "output": []}
        )

    client = Client(_SubscriptionStream(events()))
    session = OpenAIProvider(client=client).session(system="test", tools=[])
    session.set_event_callback(lambda kind, data: seen.append((kind, data)))
    assert session.send_user("hello").text == "Hello"
    assert len(seen) == 1


class Script:
    name = "script"
    model = "script"

    def __init__(self, turns: Sequence[ModelTurn]) -> None:
        self.turns = iter(turns)
        self.recorded_results: list[tuple[ToolCallResult, ...]] = []

    def session(self, **kwargs: object) -> Script:
        return self

    def snapshot(self) -> dict[str, object]:
        return {}

    def send_user(self, text: str) -> ModelTurn:
        return next(self.turns)

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        return next(self.turns)

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        self.recorded_results.append(tuple(results))


def test_sync_harness_records_complete_responses_tool_data_and_summary(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("original file\n")
    events: list[tuple[str, dict[str, object]]] = []
    provider = Script(
        [
            ModelTurn(
                "I'll inspect the file.",
                (ToolCallRequest("r", "read_file", {"path": "a.py"}),),
                reasoning="Inspect before editing.",
            ),
            ModelTurn(
                "Finished checking.",
                (
                    ToolCallRequest(
                        "f",
                        "finish_task",
                        {
                            "answer": "The file contains one line.",
                            "summary": "Checked the file.",
                        },
                    ),
                ),
            ),
        ]
    )
    broker = ToolBroker(
        tmp_path, ("a.py",), on_event=lambda kind, data: events.append((kind, data))
    )
    request = RunRequest("task", 1, "inspect", ("a.py",), tmp_path, "base")
    result = CodingAgentHarness(provider).run(request, broker)
    assert result.summary == "Checked the file."
    assert result.answer == "The file contains one line."
    assert not any(kind in {"model.said", "model.text.delta"} for kind, _ in events)
    assert (
        next(data for kind, data in events if kind == "model.reasoning")["text"]
        == "Inspect before editing."
    )
    assert next(data for kind, data in events if kind == "model.tool_call")[
        "arguments"
    ] == {"path": "a.py"}
    assert (
        next(data for kind, data in events if kind == "model.tool_result")["content"]
        == "original file\n"
    )
    assert events[-1][1]["answer"] == "The file contains one line."


def test_large_tool_arguments_and_text_are_lossless_bounded_events(
    tmp_path: Path,
) -> None:
    # JSON escapes each character to six bytes with ensure_ascii=True.
    answer = "\u0001" * 50_000
    events: list[tuple[str, dict[str, object]]] = []
    provider = Script(
        [
            ModelTurn(
                "Provisional tool-turn prose.",
                (
                    ToolCallRequest(
                        "f", "finish_task", {"answer": answer, "summary": "done"}
                    ),
                ),
            )
        ]
    )
    broker = ToolBroker(
        tmp_path, ("a.py",), on_event=lambda kind, data: events.append((kind, data))
    )
    request = RunRequest("task", 1, "inspect", ("a.py",), tmp_path, "base")
    CodingAgentHarness(provider).run(request, broker)
    assert max(len(json.dumps(data)) for _, data in events) < 32_000
    text = "".join(
        data["answer"] for kind, data in events if kind == "model.finished"
    )
    arguments = "".join(
        data["arguments_text"] for kind, data in events if kind == "model.tool_call"
    )
    assert text == answer
    assert json.loads(arguments) == {"answer": answer, "summary": "done"}


def test_live_harness_coalesces_tiny_deltas_and_flushes_tail_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.agent import harness

    monkeypatch.setattr(harness.time, "monotonic", lambda: 100.0)
    events: list[tuple[str, dict[str, object]]] = []

    class LiveScript(Script):
        def set_event_callback(self, callback: Any) -> None:
            self.callback = callback

        def send_user(self, text: str) -> ModelTurn:
            self.callback("model.text.delta", {"text": "A", "block_id": "1"})
            assert not any(kind == "model.text.delta" for kind, _ in events)
            for _ in range(200):
                self.callback("model.text.delta", {"text": "b", "block_id": "1"})
            raise RuntimeError("stream failed")

    broker = ToolBroker(
        tmp_path, ("a.py",), on_event=lambda kind, data: events.append((kind, data))
    )
    request = RunRequest("task", 1, "inspect", ("a.py",), tmp_path, "base")
    with pytest.raises(RuntimeError, match="stream failed"):
        CodingAgentHarness(LiveScript([])).run(request, broker)
    partial = [data for kind, data in events if kind == "model.partial"]
    assert "".join(str(data["text"]) for data in partial) == "A" + "b" * 200
    assert len({data["turn_id"] for data in partial}) == 1


def test_streaming_tool_turn_prose_never_becomes_public_output(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("source\n")
    events: list[tuple[str, dict[str, object]]] = []

    class LiveScript(Script):
        def set_event_callback(self, callback: Any) -> None:
            self.callback = callback

        def send_user(self, text: str) -> ModelTurn:
            self.callback(
                "model.text.delta",
                {"text": "I'll read every file now.", "block_id": "text"},
            )
            self.callback(
                "model.tool.delta",
                {
                    "tool": "read_file",
                    "call_id": "read",
                    "arguments_delta": '{"path":"a.py"}',
                },
            )
            return ModelTurn(
                text="I'll read every file now.",
                tool_calls=(
                    ToolCallRequest("read", "read_file", {"path": "a.py"}),
                ),
                stop_reason="tool_use",
            )

        def send_tool_results(
            self, results: Sequence[ToolCallResult]
        ) -> ModelTurn:
            self.callback(
                "model.text.delta",
                {"text": "The repository contains source.", "block_id": "answer"},
            )
            return ModelTurn(text="The repository contains source.")

    broker = ToolBroker(
        tmp_path, ("a.py",), on_event=lambda kind, data: events.append((kind, data))
    )
    result = CodingAgentHarness(LiveScript([])).run(
        RunRequest("task", 1, "summarize", ("a.py",), tmp_path, "base"), broker
    )
    assert result.answer == "The repository contains source."
    public_text = "\n".join(
        str(data.get("text", data.get("answer", "")))
        for kind, data in events
        if kind in {"model.text.delta", "model.said", "model.finished"}
    )
    assert "I'll read every file" not in public_text
    assert public_text.count("The repository contains source.") == 1


def test_completion_gate_rejection_never_prints_the_rejected_draft(
    tmp_path: Path,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    gate_calls = 0

    def gate() -> ToolOutcome | None:
        nonlocal gate_calls
        gate_calls += 1
        return ToolOutcome("Required checks failed.", True) if gate_calls == 1 else None

    broker = ToolBroker(
        tmp_path,
        ("a.py",),
        finish_gate=gate,
        on_event=lambda kind, data: events.append((kind, data)),
    )
    result = CodingAgentHarness(
        Script([ModelTurn("Rejected draft."), ModelTurn("Accepted answer.")])
    ).run(
        RunRequest("task", 1, "answer", ("a.py",), tmp_path, "base"), broker
    )
    assert result.answer == "Accepted answer."
    visible = "\n".join(
        str(data.get("text", data.get("answer", "")))
        for kind, data in events
        if kind in {"model.text.delta", "model.said", "model.finished"}
    )
    assert "Rejected draft" not in visible
    assert visible.count("Accepted answer.") == 1
