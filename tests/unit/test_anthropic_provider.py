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
    assert session.snapshot() == {"messages": [{"role": "user", "content": "hello"}]}


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
    assert first.usage == {"input_tokens": 11, "output_tokens": 3}
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
