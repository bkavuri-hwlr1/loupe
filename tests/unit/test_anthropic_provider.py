"""Exercise provider failures without installing the optional SDK or using a key."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.providers import anthropic_provider
from llm_cli.providers.anthropic_provider import AnthropicProvider
from llm_cli.providers.base import ToolCallResult


class APIStatusError(Exception):
    def __init__(self, status_code: int, body: object = None) -> None:
        super().__init__("raw exception with request data: do not display")
        self.status_code = status_code
        self.body = body


class BadRequestError(APIStatusError):
    pass


class APIConnectionError(Exception):
    pass


class Block(SimpleNamespace):
    def model_dump(self, *, mode: str) -> dict[str, object]:
        return vars(self).copy()


def _message(text: str = "done") -> SimpleNamespace:
    return SimpleNamespace(
        content=[Block(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=11, output_tokens=3),
    )


class Client:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = iter(outcomes)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.api_key = "test-static-api-key"
        self.auth_token = "test-static-auth-token"
        self.messages = SimpleNamespace(stream=self._stable_stream)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._beta_stream))

    @contextmanager
    def _stream(self, endpoint: str, arguments: dict[str, Any]) -> Iterator[object]:
        self.calls.append((endpoint, copy.deepcopy(arguments)))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        yield SimpleNamespace(get_final_message=lambda: outcome)

    def _stable_stream(self, **arguments: Any) -> Any:
        return self._stream("stable", arguments)

    def _beta_stream(self, **arguments: Any) -> Any:
        return self._stream("beta", arguments)


@pytest.fixture(autouse=True)
def stub_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        anthropic_provider,
        "_load_sdk",
        lambda: SimpleNamespace(
            BadRequestError=BadRequestError,
            APIStatusError=APIStatusError,
            APIConnectionError=APIConnectionError,
        ),
    )


def test_explicit_model_bad_request_retains_provider_explanation() -> None:
    client = Client(
        BadRequestError(
            400, {"error": {"message": "The selected model is unavailable."}}
        )
    )
    session = AnthropicProvider(
        model="explicit-model", fallback_model=None, client=client
    ).session(system="test", tools=[])

    with pytest.raises(LlmCoordError) as failure:
        session.send_user("hello")

    assert failure.value.code is ErrorCode.PROVIDER_UNAVAILABLE
    assert failure.value.message == "the model provider returned HTTP 400"
    assert failure.value.details == {
        "status_code": 400,
        "provider_message": "The selected model is unavailable.",
    }
    assert len(client.calls) == 1
    assert client.calls[0][0] == "stable"
    assert client.calls[0][1]["model"] == "explicit-model"
    # A failed request leaves native history unchanged.
    assert session.snapshot() == {"messages": []}


@pytest.mark.parametrize(
    ("retry_error", "expected_message"),
    [
        (APIStatusError(401), "the model provider returned HTTP 401"),
        (BadRequestError(400), "the model provider returned HTTP 400"),
        (
            APIConnectionError("network detail"),
            "the model provider could not be reached",
        ),
        (
            TypeError("Could not resolve authentication method"),
            "no Anthropic credential is configured",
        ),
    ],
)
def test_fallback_retry_errors_are_translated(
    retry_error: Exception, expected_message: str
) -> None:
    client = Client(BadRequestError(400), retry_error)
    session = AnthropicProvider(client=client).session(system="test", tools=[])

    with pytest.raises(LlmCoordError) as failure:
        session.send_user("hello")

    assert failure.value.code is ErrorCode.PROVIDER_UNAVAILABLE
    assert expected_message in failure.value.message
    assert [endpoint for endpoint, _ in client.calls] == ["beta", "stable"]
    assert "fallbacks" in client.calls[0][1]
    assert "fallbacks" not in client.calls[1][1]
    assert "betas" not in client.calls[1][1]


def test_rejected_fallback_can_succeed_and_stays_disabled_for_later_turns() -> None:
    client = Client(BadRequestError(400), _message("first"), _message("second"))
    session = AnthropicProvider(client=client).session(system="test", tools=[])

    first = session.send_user("hello")
    second = session.send_user("again")

    assert first.text == "first"
    assert first.usage == {"input_tokens": 11, "output_tokens": 3, "prompt_tokens": 11}
    assert second.text == "second"
    assert [endpoint for endpoint, _ in client.calls] == ["beta", "stable", "stable"]
    assert client.calls[0][1]["messages"] == client.calls[1][1]["messages"]
    assert session.snapshot() == {
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "first"}]},
            {"role": "user", "content": "again"},
            {"role": "assistant", "content": [{"type": "text", "text": "second"}]},
        ]
    }


def test_status_explanation_redacts_credentials_and_strips_control_characters() -> None:
    error = BadRequestError(
        400,
        {
            "error": {
                "message": (
                    "Rejected test-static-api-key\n\x1b and test-static-auth-token\t."
                )
            },
            "request": "unrelated body field: do not display",
        },
    )
    session = AnthropicProvider(fallback_model=None, client=Client(error)).session(
        system="test", tools=[]
    )

    with pytest.raises(LlmCoordError) as failure:
        session.send_user("hello")

    assert failure.value.message == "the model provider returned HTTP 400"
    assert failure.value.details == {
        "status_code": 400,
        "provider_message": "Rejected [redacted] and [redacted] .",
    }


def test_status_explanation_is_bounded_after_redacting_credentials() -> None:
    session = AnthropicProvider(
        fallback_model=None,
        client=Client(
            BadRequestError(
                400,
                {"error": {"message": "x" * 490 + "test-static-api-key" + "x" * 1000}},
            )
        ),
    ).session(system="test", tools=[])

    with pytest.raises(LlmCoordError) as failure:
        session.send_user("hello")

    assert failure.value.message == "the model provider returned HTTP 400"
    assert failure.value.details is not None
    detail = failure.value.details["provider_message"]
    assert len(detail) == 500
    assert detail.endswith("...")
    assert "test-static" not in detail


@pytest.mark.parametrize(
    "body",
    [None, "raw server body", {"error": "unexpected"}, {"error": {"message": 42}}],
)
def test_malformed_provider_error_body_preserves_status_only(body: object) -> None:
    session = AnthropicProvider(
        fallback_model=None, client=Client(BadRequestError(400, body))
    ).session(system="test", tools=[])

    with pytest.raises(LlmCoordError) as failure:
        session.send_user("hello")

    assert failure.value.message == "the model provider returned HTTP 400"
    assert failure.value.details == {"status_code": 400}


def test_unrelated_type_error_remains_a_programming_failure() -> None:
    session = AnthropicProvider(
        fallback_model=None, client=Client(TypeError("unsupported keyword"))
    ).session(system="test", tools=[])

    with pytest.raises(TypeError, match="unsupported keyword"):
        session.send_user("hello")


def test_requests_enable_automatic_prompt_caching() -> None:
    client = Client(_message())
    session = AnthropicProvider(
        model="explicit-model", fallback_model=None, client=client
    ).session(system="test", tools=[])

    turn = session.send_user("hello")

    assert client.calls[0][1]["cache_control"] == {"type": "ephemeral"}
    assert "tool_choice" not in client.calls[0][1]
    assert turn.context_tokens == 14


def test_summarize_disables_tools_and_leaves_history_unchanged() -> None:
    client = Client(_message(), _message("the summary"))
    session = AnthropicProvider(
        model="explicit-model", fallback_model=None, client=client
    ).session(system="test", tools=[{"name": "read_file"}])
    events: list[str] = []
    session.set_event_callback(lambda kind, payload: events.append(kind))
    session.send_user("hello")
    before = session.snapshot()

    turn = session.summarize("summarize please")

    arguments = client.calls[1][1]
    assert turn.text == "the summary"
    assert arguments["tool_choice"] == {"type": "none"}
    assert arguments["tools"] == [{"name": "read_file"}]
    assert arguments["messages"][-1] == {
        "role": "user",
        "content": "summarize please",
    }
    assert session.snapshot() == before


def test_replace_history_keeps_only_the_summary() -> None:
    client = Client(_message(), _message())
    session = AnthropicProvider(
        model="explicit-model", fallback_model=None, client=client
    ).session(system="test", tools=[])
    session.send_user("hello")

    session.replace_history("summary text")
    session.send_user("next")

    assert client.calls[1][1]["messages"] == [
        {"role": "user", "content": "summary text"},
        {"role": "user", "content": "next"},
    ]


def test_cached_prompt_tokens_count_toward_context_size() -> None:
    message = _message()
    message.usage = SimpleNamespace(
        input_tokens=5,
        output_tokens=7,
        cache_read_input_tokens=1_000,
        cache_creation_input_tokens=20,
    )
    session = AnthropicProvider(
        model="explicit-model", fallback_model=None, client=Client(message)
    ).session(system="test", tools=[])

    turn = session.send_user("hello")

    assert turn.usage["prompt_tokens"] == 1_025
    assert turn.context_tokens == 1_032


def test_oversized_prompt_is_classified_without_dropping_fallback() -> None:
    client = Client(
        BadRequestError(
            400,
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": "prompt is too long: 215000 tokens > 200000 maximum",
                }
            },
        ),
        _message(),
    )
    session = AnthropicProvider(client=client).session(system="test", tools=[])

    with pytest.raises(LlmCoordError) as failure:
        session.send_user("hello")

    assert failure.value.details == {
        "provider_error": "context_overflow",
        "status_code": 400,
    }
    assert [endpoint for endpoint, _ in client.calls] == ["beta"]
    assert session.snapshot() == {"messages": []}
    session.send_user("smaller")
    # The refusal fallback stays enabled for later requests.
    assert client.calls[1][0] == "beta"


def test_failed_tool_result_request_leaves_history_unchanged() -> None:
    client = Client(APIStatusError(503))
    session = AnthropicProvider(
        model="explicit-model", fallback_model=None, client=client
    ).session(
        system="test",
        tools=[],
        state={
            "messages": [
                {"role": "user", "content": "go"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "read_file",
                            "input": {},
                        }
                    ],
                },
            ]
        },
    )

    with pytest.raises(LlmCoordError):
        session.send_tool_results([ToolCallResult("t1", "contents")])

    assert len(session.snapshot()["messages"]) == 2


def test_shortened_summary_request_trims_tool_payloads_only() -> None:
    client = Client(_message("the summary"))
    state = {
        "messages": [
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "write_file",
                        "input": {"path": "a.py", "content": "x" * 50},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "y" * 50}
                ],
            },
        ]
    }
    session = AnthropicProvider(
        model="explicit-model", fallback_model=None, client=client
    ).session(system="test", tools=[], state=state)

    session.summarize("summarize", max_tool_text=10)

    sent = client.calls[0][1]["messages"]
    assert sent[0] == {"role": "user", "content": "inspect"}
    tool_input = sent[1]["content"][0]["input"]
    assert tool_input["path"] == "a.py"
    assert tool_input["content"].startswith("x" * 10 + "\n[... 40 characters omitted")
    assert sent[2]["content"][0]["content"].startswith("y" * 10 + "\n[...")
    assert session.snapshot() == state


def test_input_and_max_tokens_rejection_is_an_overflow() -> None:
    message = (
        "input length and `max_tokens` exceed context limit: 190000 + 64000 > "
        "200000, decrease input length or `max_tokens` and try again"
    )
    client = Client(BadRequestError(400, {"error": {"message": message}}))
    session = AnthropicProvider(
        model="explicit-model", fallback_model=None, client=client
    ).session(system="test", tools=[])

    with pytest.raises(LlmCoordError) as failure:
        session.send_user("hello")

    assert failure.value.details == {
        "provider_error": "context_overflow",
        "status_code": 400,
    }


def test_unrelated_context_wording_is_not_an_overflow() -> None:
    message = "thinking.budget_tokens must be less than the context window"
    client = Client(BadRequestError(400, {"error": {"message": message}}))
    session = AnthropicProvider(
        model="explicit-model", fallback_model=None, client=client
    ).session(system="test", tools=[])

    with pytest.raises(LlmCoordError) as failure:
        session.send_user("hello")

    assert (failure.value.details or {}).get("provider_error") != "context_overflow"


def test_reply_cut_off_by_the_window_is_an_overflow() -> None:
    message = _message("partial")
    message.stop_reason = "model_context_window_exceeded"
    session = AnthropicProvider(
        model="explicit-model", fallback_model=None, client=Client(message)
    ).session(system="test", tools=[])

    with pytest.raises(LlmCoordError) as failure:
        session.send_user("hello")

    assert failure.value.details == {"provider_error": "context_overflow"}
    assert session.snapshot() == {"messages": []}


def test_summary_rejection_does_not_disable_the_fallback() -> None:
    client = Client(
        BadRequestError(400, {"error": {"message": "beta not enabled"}}),
        _message("the summary"),
        _message(),
    )
    session = AnthropicProvider(client=client).session(system="test", tools=[])

    assert session.summarize("summarize").text == "the summary"
    session.send_user("next")

    assert [endpoint for endpoint, _ in client.calls] == ["beta", "stable", "beta"]


def test_summary_can_cover_pending_tool_results() -> None:
    client = Client(_message("the summary"))
    state = {
        "messages": [
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}
                ],
            },
        ]
    }
    session = AnthropicProvider(
        model="explicit-model", fallback_model=None, client=client
    ).session(system="test", tools=[], state=state)

    session.summarize("summarize", pending_results=[ToolCallResult("t1", "contents")])

    last = client.calls[0][1]["messages"][-1]
    assert last["role"] == "user"
    assert last["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": "contents",
            "is_error": False,
        },
        {"type": "text", "text": "summarize"},
    ]
    assert session.snapshot() == state
