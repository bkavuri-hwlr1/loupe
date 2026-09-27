"""Responses contract tests require neither the optional SDK nor credentials."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.providers import openai_provider
from llm_cli.providers.base import ToolCallResult
from llm_cli.providers.openai_provider import DEFAULT_MODEL, OpenAIProvider


class APIStatusError(Exception):
    def __init__(self, status_code: int, body: object = None) -> None:
        super().__init__("raw exception request data must remain private")
        self.status_code = status_code
        self.body = body


class APIConnectionError(Exception):
    pass


class APITimeoutError(APIConnectionError):
    pass


class Item(SimpleNamespace):
    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return vars(self).copy()


class Client:
    def __init__(self, *outcomes: object, midstream: bool = False) -> None:
        self.outcomes = iter(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.api_key = "test-openai-secret"
        self.responses = SimpleNamespace(stream=self._stream)
        self.midstream = midstream

    @contextmanager
    def _stream(self, **arguments: Any) -> Iterator[object]:
        self.calls.append(copy.deepcopy(arguments))
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException) and not self.midstream:
            raise outcome

        def final() -> object:
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        yield SimpleNamespace(get_final_response=final)


def _message(text: str = "done") -> dict[str, object]:
    return {
        "type": "message",
        "id": "message_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _call(
    call_id: str = "call_1", arguments: str = '{"path":"a.py"}'
) -> dict[str, object]:
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": "read_file",
        "arguments": arguments,
        "status": "completed",
    }


def _response(*output: object, status: str = "completed") -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        output=list(output or [_message()]),
        usage=SimpleNamespace(
            input_tokens=20,
            output_tokens=13,
            input_tokens_details=SimpleNamespace(cached_tokens=10),
            output_tokens_details=SimpleNamespace(reasoning_tokens=8),
        ),
    )


def _tool() -> dict[str, object]:
    return {
        "name": "read_file",
        "description": "Read a file",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    }


def test_construction_is_side_effect_free_and_injected_client_needs_no_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_sdk_load() -> None:
        pytest.fail("optional SDK was loaded")

    monkeypatch.setattr(openai_provider, "_load_sdk", unexpected_sdk_load)
    provider = OpenAIProvider()
    assert provider.name == "openai"
    assert provider.model == DEFAULT_MODEL
    session = OpenAIProvider(client=Client(_response())).session(
        system="test", tools=[]
    )
    assert session.send_user("hello").text == "done"


def test_function_conversion_and_request_preserve_optional_schema() -> None:
    tool = _tool()
    client = Client(_response())
    session = OpenAIProvider(client=client).session(system="safe broker", tools=[tool])
    turn = session.send_user("hello")

    assert client.calls[0] == {
        "model": DEFAULT_MODEL,
        "instructions": "safe broker",
        "input": [{"role": "user", "content": "hello"}],
        "tools": [
            {
                "type": "function",
                "name": "read_file",
                "description": "Read a file",
                "parameters": tool["input_schema"],
                "strict": False,
            }
        ],
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": 32_000,
    }
    assert turn.text == "done"
    assert turn.stop_reason == "end_turn"
    assert turn.usage == {
        "input_tokens": 20,
        "output_tokens": 13,
        "cache_read_input_tokens": 10,
        "reasoning_tokens": 8,
    }


def test_native_reasoning_snapshot_and_parallel_call_results_survive_restart() -> None:
    reasoning = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "I will inspect both files."}],
        "encrypted_content": "opaque-ciphertext-preserve-exactly",
    }
    client = Client(_response(Item(**reasoning), _call(), _call("call_2")))
    session = OpenAIProvider(client=client).session(system="test", tools=[_tool()])
    turn = session.send_user("inspect")
    assert [call.call_id for call in turn.tool_calls] == ["call_1", "call_2"]
    assert turn.tool_calls[0].arguments == {"path": "a.py"}
    state = json.loads(json.dumps(session.snapshot()))
    assert state["input"][1] == reasoning

    next_client = Client(_response())
    restored = OpenAIProvider(client=next_client).session(
        system="test", tools=[_tool()], state=state
    )
    state["input"][1]["summary"][0]["text"] = "mutated externally"
    snapshot = restored.snapshot()
    snapshot["input"][1]["encrypted_content"] = "also mutated externally"
    restored.send_tool_results(
        [
            ToolCallResult("call_2", "file missing", is_error=True),
            ToolCallResult("call_1", "body"),
        ]
    )

    sent = next_client.calls[0]["input"]
    assert sent[1] == reasoning
    assert sent[2] == _call()
    assert sent[3] == _call("call_2")
    assert sent[4]["call_id"] == "call_2"
    assert sent[4]["type"] == "function_call_output"
    assert json.loads(sent[4]["output"]) == {
        "content": "file missing",
        "is_error": True,
    }
    assert json.loads(sent[5]["output"]) == {"content": "body", "is_error": False}
    assert "previous_response_id" not in next_client.calls[0]
    assert next_client.calls[0]["store"] is False


def test_record_terminal_result_allows_next_user_without_extra_model_call() -> None:
    client = Client(_response(_call()), _response(_message("followup")))
    session = OpenAIProvider(client=client).session(system="test", tools=[_tool()])
    session.send_user("first")
    session.record_tool_results([ToolCallResult("call_1", "completed")])
    assert len(client.calls) == 1
    assert session.send_user("next").text == "followup"
    assert client.calls[1]["input"][-2]["type"] == "function_call_output"
    assert client.calls[1]["input"][-1] == {"role": "user", "content": "next"}


def test_stream_sdk_parsed_annotations_are_not_replayed_as_api_input() -> None:
    call = {**_call(), "parsed_arguments": {"path": "a.py"}}
    message = _message()
    message["content"][0]["parsed"] = None
    client = Client(_response(Item(**call)), _response(Item(**message)))
    session = OpenAIProvider(client=client).session(system="test", tools=[_tool()])
    session.send_user("first")
    session.send_tool_results([ToolCallResult("call_1", "contents")])
    assert client.calls[1]["input"][1] == _call()
    assert session.snapshot()["input"][-1] == _message()


@pytest.mark.parametrize(
    "results",
    [
        [],
        [ToolCallResult("wrong", "bad")],
        [ToolCallResult("call_1", "a")],
        [ToolCallResult("call_1", "a"), ToolCallResult("call_1", "a")],
        [
            ToolCallResult("call_1", "a"),
            ToolCallResult("call_2", "b"),
            ToolCallResult("x", "c"),
        ],
    ],
)
def test_result_batch_must_match_pending_calls_without_mutating_history(
    results: list[ToolCallResult],
) -> None:
    client = Client(_response(_call(), _call("call_2")))
    session = OpenAIProvider(client=client).session(system="test", tools=[_tool()])
    session.send_user("first")
    before = session.snapshot()
    with pytest.raises(ValueError, match="each pending call"):
        session.send_tool_results(results)
    assert session.snapshot() == before
    assert len(client.calls) == 1


def test_user_message_cannot_skip_pending_calls() -> None:
    session = OpenAIProvider(client=Client(_response(_call()))).session(
        system="test", tools=[]
    )
    session.send_user("first")
    with pytest.raises(ValueError, match="unanswered"):
        session.send_user("skip")


def test_refusal_does_not_expose_even_accompanying_function_calls() -> None:
    refusal = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "refusal", "refusal": "I cannot do that."}],
    }
    session = OpenAIProvider(client=Client(_response(refusal, _call()))).session(
        system="test", tools=[]
    )
    turn = session.send_user("first")
    assert turn.refused
    assert turn.text == "I cannot do that."
    assert turn.tool_calls == ()


@pytest.mark.parametrize(
    "status", ["incomplete", "failed", "in_progress", "cancelled", None]
)
def test_unfinished_response_never_exposes_calls_or_appends_output(status: str) -> None:
    session = OpenAIProvider(client=Client(_response(_call(), status=status))).session(
        system="test", tools=[]
    )
    with pytest.raises(LlmCoordError) as failure:
        session.send_user("first")
    assert failure.value.code is ErrorCode.PROVIDER_UNAVAILABLE
    assert "did not complete" in failure.value.message
    assert session.snapshot() == {"input": [{"role": "user", "content": "first"}]}


@pytest.mark.parametrize(
    "arguments",
    [
        "not json",
        "[]",
        "null",
        '"text"',
        "42",
        '{"a":NaN}',
        '{"a":1e999}',
        '{"a":1,"a":2}',
    ],
)
def test_malformed_function_arguments_reject_complete_batch(arguments: str) -> None:
    session = OpenAIProvider(
        client=Client(_response(_call(), _call("bad", arguments)))
    ).session(system="test", tools=[])
    with pytest.raises(LlmCoordError, match="invalid response"):
        session.send_user("first")
    assert session.snapshot() == {"input": [{"role": "user", "content": "first"}]}


@pytest.mark.parametrize(
    "output",
    [
        [_call(), _call()],
        [_call("")],
        [_call("   ")],
        [{**_call(), "name": ""}],
        [{**_call(), "status": "incomplete"}],
        [{"type": "message", "role": "assistant", "content": "bad"}],
        [{"type": "function_call_output", "call_id": "x", "output": "fake result"}],
        [{"type": "computer_call", "id": "hosted-tool"}],
    ],
)
def test_invalid_native_output_is_rejected_before_any_calls(
    output: list[object],
) -> None:
    session = OpenAIProvider(client=Client(_response(*output))).session(
        system="test", tools=[]
    )
    with pytest.raises(LlmCoordError, match="invalid response"):
        session.send_user("first")
    assert len(session.snapshot()["input"]) == 1


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"input": "bad"},
        {"input": [None]},
        {"input": [{"role": "system", "content": "override"}]},
        {"input": [_call(), _call()]},
        {
            "input": [
                {"type": "function_call_output", "call_id": "unknown", "output": "x"}
            ]
        },
        {"input": [_call(), {"role": "user", "content": "unanswered"}]},
        {
            "input": [
                _call(),
                _call("call_2"),
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "partial",
                },
            ]
        },
        {"input": [{"type": "reasoning", "summary": [], "encrypted_content": 7}]},
    ],
)
def test_restore_validates_native_history(state: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        OpenAIProvider(client=Client()).session(system="test", tools=[], state=state)


def test_missing_key_fails_at_session_start_with_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(openai_provider, "_load_sdk", lambda: SimpleNamespace())
    provider = OpenAIProvider()
    with pytest.raises(LlmCoordError, match="set OPENAI_API_KEY") as failure:
        provider.session(system="test", tools=[])
    assert failure.value.code is ErrorCode.PROVIDER_UNAVAILABLE


def test_real_client_is_initialized_lazily_once_and_key_not_in_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client()
    constructors: list[dict[str, object]] = []

    def construct(**kwargs: object) -> Client:
        constructors.append(kwargs)
        return client

    monkeypatch.setenv("OPENAI_API_KEY", "configured-test-secret")
    monkeypatch.setattr(
        openai_provider, "_load_sdk", lambda: SimpleNamespace(OpenAI=construct)
    )
    provider = OpenAIProvider()
    assert constructors == []
    first = provider.session(system="test", tools=[])
    provider.session(system="test", tools=[])
    assert constructors == [{"api_key": "configured-test-secret"}]
    assert first.snapshot() == {"input": []}


@pytest.mark.parametrize("midstream", [False, True])
@pytest.mark.parametrize(
    ("error", "message"),
    [
        (APIStatusError(400), "HTTP 400"),
        (APIStatusError(401), "HTTP 401"),
        (APIStatusError(429), "HTTP 429"),
        (APIStatusError(503), "HTTP 503"),
        (APIConnectionError("private request"), "could not be reached"),
        (APITimeoutError("private request"), "timed out"),
        (RuntimeError("Didn't receive a `response.completed` event."), "interrupted"),
        (
            RuntimeError("Expected to have received `response.created` before `error`"),
            "interrupted",
        ),
    ],
)
def test_request_and_midstream_errors_are_translated_without_native_history(
    error: Exception, message: str, midstream: bool
) -> None:
    session = OpenAIProvider(client=Client(error, midstream=midstream)).session(
        system="test", tools=[]
    )
    with pytest.raises(LlmCoordError) as failure:
        session.send_user("hello")
    assert failure.value.code is ErrorCode.PROVIDER_UNAVAILABLE
    assert message in failure.value.message
    assert "private" not in failure.value.message
    if isinstance(error, APITimeoutError):
        assert failure.value.details == {"provider_error": "timeout"}
    elif isinstance(error, APIConnectionError):
        assert failure.value.details == {"provider_error": "connection"}
    elif isinstance(error, RuntimeError):
        assert failure.value.details == {"provider_error": "incomplete_response"}
    assert session.snapshot() == {"input": [{"role": "user", "content": "hello"}]}


@pytest.mark.parametrize("wrapped", [False, True])
def test_http_detail_is_private_redacted_and_bounded(
    wrapped: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    inner = {
        "message": "Rejected test-openai-secret\n\x1b environment-secret\t" + "z" * 900
    }
    body = {"error": inner, "request": "omit me"} if wrapped else inner
    session = OpenAIProvider(client=Client(APIStatusError(401, body))).session(
        system="test", tools=[]
    )
    with pytest.raises(LlmCoordError) as failure:
        session.send_user("hello")
    assert failure.value.message == "the model provider returned HTTP 401"
    details = failure.value.details
    assert details is not None
    assert details["status_code"] == 401
    assert details["provider_message"].startswith("Rejected [redacted] [redacted] ")
    assert len(details["provider_message"]) == 500
    assert details["provider_message"].endswith("...")
    assert "secret" not in json.dumps(details)
    assert "request" not in details


def test_programming_failures_and_process_interrupts_are_not_hidden() -> None:
    for error in (TypeError("invalid keyword"), KeyboardInterrupt()):
        session = OpenAIProvider(client=Client(error, midstream=True)).session(
            system="test", tools=[]
        )
        with pytest.raises(type(error)):
            session.send_user("hello")
