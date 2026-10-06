"""Exercise the optional real SDK's SSE parser without network or credentials."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from llm_cli.agent.tools import tool_schemas
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.providers.base import ToolCallResult
from llm_cli.providers.openai_provider import DEFAULT_MODEL, OpenAIProvider

openai = pytest.importorskip("openai")
_http_module = (
    "httpx2"
    if any(
        cls.__module__.partition(".")[0] == "httpx2"
        for cls in openai.DefaultHttpxClient.__mro__
    )
    else "httpx"
)
httpx = pytest.importorskip(_http_module)


def _response(
    output: list[dict[str, Any]], *, status: str = "completed"
) -> dict[str, Any]:
    return {
        "id": "resp_transport",
        "object": "response",
        "created_at": 1_700_000_000,
        "status": status,
        "error": None,
        "incomplete_details": (
            {"reason": "max_output_tokens"} if status == "incomplete" else None
        ),
        "instructions": None,
        "model": DEFAULT_MODEL,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 18,
            "input_tokens_details": {"cached_tokens": 5},
            "output_tokens": 11,
            "output_tokens_details": {"reasoning_tokens": 7},
            "total_tokens": 29,
        },
    }


def _call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
        "status": "completed",
    }


def _message() -> dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_transport",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "done", "annotations": []}],
    }


def _events(response: dict[str, Any]) -> bytes:
    initial = copy.deepcopy(response)
    initial.update(status="in_progress", output=[])
    events: list[dict[str, Any]] = [
        {"type": "response.created", "sequence_number": 0, "response": initial}
    ]
    events.extend(
        {
            "type": "response.output_item.added",
            "sequence_number": index + 1,
            "output_index": index,
            "item": item,
        }
        for index, item in enumerate(response["output"])
    )
    events.append(
        {
            "type": f"response.{response['status']}",
            "sequence_number": len(events),
            "response": response,
        }
    )
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


@contextmanager
def _client(outcomes: list[object]) -> Iterator[tuple[Any, list[dict[str, Any]]]]:
    responses = iter(outcomes)
    requests: list[dict[str, Any]] = []

    def handle(request: Any) -> Any:
        assert request.method == "POST"
        assert request.url.path == "/v1/responses"
        requests.append(json.loads(request.content))
        outcome = next(responses)
        if isinstance(outcome, tuple):
            code, body = outcome
            return httpx.Response(code, json=body)
        body = outcome if isinstance(outcome, bytes) else _events(outcome)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    # An in-memory transport prevents any HTTP request from leaving the process.
    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http_client,
        openai.OpenAI(
            api_key="sk-local-transport-placeholder",
            base_url="https://api.openai.test/v1",
            http_client=http_client,
            max_retries=0,
        ) as client,
    ):
        yield client, requests


def test_real_sdk_stream_and_restart_replay_only_api_native_fields() -> None:
    reasoning = {
        "type": "reasoning",
        "id": "rs_transport",
        "summary": [{"type": "summary_text", "text": "Inspect the file."}],
        "encrypted_content": "opaque-encrypted-reasoning",
    }
    calls = [
        _call("read_1", "read_file", {"path": "docs/a.md"}),
        _call(
            "finish_1", "finish_task", {"answer": "Inspected.", "summary": "Inspected."}
        ),
    ]
    outcomes = [
        _response([reasoning, _message(), *calls]),
        _response([_call("read_2", "read_file", {"path": "docs/b.md"})]),
        _response([_message()]),
    ]
    with _client(outcomes) as (client, requests):
        provider = OpenAIProvider(client=client)
        tools = tool_schemas(["read_file", "finish_task", "list_files"])
        session = provider.session(system="Use the scoped broker.", tools=tools)
        turn = session.send_user("Inspect a.md")
        assert [call.call_id for call in turn.tool_calls] == ["read_1", "finish_1"]
        assert turn.usage == {
            "input_tokens": 18,
            "prompt_tokens": 18,
            "cache_read_input_tokens": 5,
            "output_tokens": 11,
            "reasoning_tokens": 7,
        }
        session.record_tool_results(
            [
                ToolCallResult("read_1", "actual file content"),
                ToolCallResult("finish_1", "task marked complete"),
            ]
        )
        assert len(requests) == 1

        state = json.loads(json.dumps(session.snapshot()))
        restored = provider.session(
            system="Use the scoped broker.", tools=tools, state=state
        )
        restored.send_user("Inspect b.md")
        final = restored.send_tool_results(
            [ToolCallResult("read_2", "File does not exist", is_error=True)]
        )
        assert final.text == "done"

        assert len(requests) == 3
        for request in requests:
            assert request["store"] is False
            assert request["stream"] is True
            assert request["include"] == ["reasoning.encrypted_content"]
            assert "previous_response_id" not in request
            assert request["instructions"] == "Use the scoped broker."
            assert all(tool["strict"] is False for tool in request["tools"])
            for item in request["input"]:
                assert "parsed_arguments" not in item
                if item.get("type") == "message":
                    assert all("parsed" not in part for part in item["content"])
        replayed = requests[1]["input"]
        assert replayed[1]["encrypted_content"] == reasoning["encrypted_content"]
        assert replayed[1]["summary"] == reasoning["summary"]
        assert replayed[3]["id"] == "fc_read_1"
        assert replayed[3]["call_id"] == "read_1"
        assert json.loads(replayed[-3]["output"]) == {
            "content": "actual file content",
            "is_error": False,
        }
        assert replayed[-1] == {"role": "user", "content": "Inspect b.md"}
        assert json.loads(requests[2]["input"][-1]["output"]) == {
            "content": "File does not exist",
            "is_error": True,
        }


def test_real_sdk_http_error_explanation_is_redacted() -> None:
    body = {
        "error": {
            "message": "Rejected sk-local-transport-placeholder credential.",
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        }
    }
    with _client([(400, body)]) as (client, requests):
        session = OpenAIProvider(client=client).session(system="test", tools=[])
        with pytest.raises(LlmCoordError) as failure:
            session.send_user("hello")
        assert len(requests) == 1
        assert failure.value.code is ErrorCode.PROVIDER_UNAVAILABLE
        assert failure.value.details == {
            "status_code": 400,
            "provider_message": "Rejected [redacted] credential.",
        }


@pytest.mark.parametrize("status", ["incomplete", "failed"])
def test_real_sdk_noncompleted_terminal_event_cannot_expose_tool_calls(
    status: str,
) -> None:
    response = _response(
        [_call("write_1", "write_file", {"path": "a.md", "content": "unsafe"})],
        status=status,
    )
    with _client([response]) as (client, _requests):
        session = OpenAIProvider(client=client).session(system="test", tools=[])
        with pytest.raises(LlmCoordError) as failure:
            session.send_user("hello")
        assert failure.value.code is ErrorCode.PROVIDER_UNAVAILABLE
        assert session.snapshot() == {"input": []}


def test_real_sdk_stream_error_is_translated_without_raw_diagnostics() -> None:
    event = {
        "type": "error",
        "code": "server_error",
        "message": "private request details",
        "param": None,
        "sequence_number": 0,
    }
    stream = f"event: error\ndata: {json.dumps(event)}\n\n".encode()
    with _client([stream]) as (client, _requests):
        session = OpenAIProvider(client=client).session(system="test", tools=[])
        with pytest.raises(LlmCoordError) as failure:
            session.send_user("hello")
        assert failure.value.code is ErrorCode.PROVIDER_UNAVAILABLE
        assert "private request details" not in str(failure.value)
