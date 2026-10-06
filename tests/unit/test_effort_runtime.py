"""Effort is an exact request setting, never a display-only preference."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_anthropic_provider import (
    APIConnectionError,
    APIStatusError,
    BadRequestError,
    _message,
)
from test_anthropic_provider import (
    Client as AnthropicClient,
)
from test_openai_provider import Client as ResponsesClient
from test_openai_provider import _response

from llm_cli.cli import session
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient
from llm_cli.providers import anthropic_provider, openai_provider
from llm_cli.providers.anthropic_provider import AnthropicProvider
from llm_cli.providers.base import capped_effort
from llm_cli.providers.codex_provider import CodexProvider
from llm_cli.providers.openai_provider import OpenAIProvider
from llm_cli.providers.registry import ProviderRegistry


@pytest.mark.parametrize("effort", [None, "low", "medium", "high", "xhigh"])
def test_openai_request_uses_exact_effort_and_preserves_summary(effort: str | None):
    client = ResponsesClient(_response())
    chat = OpenAIProvider(client=client, effort=effort).session(system="test", tools=[])
    chat.set_event_callback(lambda *args: None)
    chat.send_user("hello")
    assert client.calls[0]["reasoning"] == {
        "summary": "auto",
        **({"effort": effort} if effort is not None else {}),
    }
    assert client.calls[0]["include"] == ["reasoning.encrypted_content"]


@pytest.mark.parametrize("model", ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "unknown"])
def test_nonreasoning_openai_models_omit_unsupported_parameters(model: str):
    client = ResponsesClient(_response())
    chat = OpenAIProvider(client=client, model=model).session(system="test", tools=[])
    chat.set_event_callback(lambda *args: None)
    chat.send_user("hello")
    assert "reasoning" not in client.calls[0]
    assert "include" not in client.calls[0]
    with pytest.raises(ValueError, match="does not support effort"):
        OpenAIProvider(model=model, effort="high")


def test_explicit_none_remains_distinct_from_provider_default():
    client = ResponsesClient(_response())
    chat = OpenAIProvider(model="gpt-5.6-sol", effort="none", client=client).session(
        system="test", tools=[]
    )
    chat.send_user("hello")
    assert client.calls[0]["reasoning"] == {"effort": "none"}


def test_openai_respects_model_output_limit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        openai_provider,
        "model_option",
        lambda *a, **kw: SimpleNamespace(
            efforts=(),
            default_effort=None,
            max_output_tokens=16384,
            context_window=None,
        ),
    )
    client = ResponsesClient(_response())
    chat = OpenAIProvider(model="gpt-4o", client=client).session(
        system="test", tools=[]
    )
    chat.send_user("hello")
    assert client.calls[0]["max_output_tokens"] == 16384


@pytest.mark.parametrize(
    ("current", "ceiling", "supported", "expected"),
    [
        ("xhigh", "low", ("low", "medium", "high", "xhigh", "max"), "low"),
        # A cap never raises effort, and an equal level changes nothing.
        ("low", "medium", ("low", "medium", "high"), None),
        ("low", "low", ("low", "medium", "high"), None),
        # Unknown defaults take the cap.
        (None, "low", ("low", "medium", "high"), "low"),
        # Without a level at or below the cap, the lowest supported one is used.
        ("high", "minimal", ("low", "medium", "high"), "low"),
        ("high", "medium", ("low", "high"), "low"),
        ("high", "low", (), None),
    ],
)
def test_capped_effort_only_ever_lowers_to_a_supported_level(
    current: str | None, ceiling: str, supported: tuple[str, ...], expected: str | None
):
    assert capped_effort(current, ceiling, supported) == expected


def test_sessions_can_think_less_than_their_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = ResponsesClient(_response(), _response())
    paths = AppPaths("test", *(tmp_path / key for key in ("c", "d", "s", "r")))
    codex = CodexProvider(
        paths=paths, model="gpt-6-astra", effort="xhigh", client=client
    )
    assert codex.capped_effort("low") == "low"
    codex.session(system="test", tools=[], effort="low").send_user("hello")
    codex.session(system="test", tools=[]).send_user("hello")
    assert [call["reasoning"] for call in client.calls] == [
        {"effort": "low"},
        {"effort": "xhigh"},
    ]
    with pytest.raises(ValueError, match="does not support effort"):
        codex.session(system="test", tools=[], effort="ultra")

    # gpt-5.4 defaults to no reasoning, so a low cap would think more.
    assert OpenAIProvider(model="gpt-5.4", client=object()).capped_effort("low") is None

    monkeypatch.setattr(
        anthropic_provider,
        "_load_sdk",
        lambda: SimpleNamespace(
            BadRequestError=BadRequestError,
            APIStatusError=APIStatusError,
            APIConnectionError=APIConnectionError,
        ),
    )
    messages = AnthropicClient(_message())
    claude = AnthropicProvider(
        model="claude-opus-5", effort="max", fallback_model=None, client=messages
    )
    assert claude.capped_effort("low") == "low"
    claude.session(system="test", tools=[], effort="low").send_user("hello")
    assert messages.calls[0][1]["output_config"] == {"effort": "low"}
    haiku = AnthropicProvider(model="claude-haiku-4-5", client=object())
    assert haiku.capped_effort("low") is None


def test_codex_subscription_passes_effort_without_output_token_limit(tmp_path: Path):
    client = ResponsesClient(_response())
    paths = AppPaths("test", *(tmp_path / key for key in ("c", "d", "s", "r")))
    chat = CodexProvider(paths=paths, effort="max", client=client).session(
        system="test", tools=[]
    )
    chat.send_user("hello")
    assert client.calls[0]["reasoning"] == {"effort": "max"}
    assert "max_output_tokens" not in client.calls[0]


@pytest.mark.parametrize(
    ("model", "effort", "adaptive"),
    [
        ("claude-opus-5", "max", True),
        ("claude-opus-5", None, True),
        ("claude-sonnet-4-6", "medium", True),
        ("claude-opus-4-5", "low", False),
        ("claude-haiku-4-5", None, False),
        ("unknown", None, False),
    ],
)
def test_anthropic_only_sends_supported_effort_and_thinking(
    model: str, effort: str | None, adaptive: bool, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        anthropic_provider,
        "_load_sdk",
        lambda: SimpleNamespace(
            BadRequestError=BadRequestError,
            APIStatusError=APIStatusError,
            APIConnectionError=APIConnectionError,
        ),
    )
    client = AnthropicClient(_message())
    chat = AnthropicProvider(
        model=model, effort=effort, fallback_model=None, client=client
    ).session(system="test", tools=[])
    chat.send_user("hello")
    arguments = client.calls[0][1]
    assert arguments.get("thinking") == ({"type": "adaptive"} if adaptive else None)
    assert arguments.get("output_config") == (
        {"effort": effort} if effort is not None else None
    )


@pytest.mark.parametrize(
    ("model", "effort"),
    [
        ("claude-sonnet-4-6", "xhigh"),
        ("claude-opus-4-5", "max"),
        ("claude-haiku-4-5", "high"),
        ("claude-opus-5", "ultra"),
    ],
)
def test_unsupported_anthropic_effort_is_rejected_before_a_request(model, effort):
    with pytest.raises(ValueError, match="does not support effort"):
        AnthropicProvider(model=model, effort=effort)


def test_selected_anthropic_model_never_inherits_opus_fallback():
    chat = AnthropicProvider(model="claude-haiku-4-5", client=object()).session(
        system="test", tools=[]
    )
    arguments = chat._request_arguments(with_fallback=True)
    assert arguments["model"] == "claude-haiku-4-5"
    assert "fallbacks" not in arguments
    assert "betas" not in arguments
    explicit_off = AnthropicProvider(fallback_model=None, client=object()).session(
        system="test", tools=[]
    )
    assert "fallbacks" not in explicit_off._request_arguments(with_fallback=True)


@pytest.mark.parametrize("maximum", [4096, 8192, 32000, 200000])
def test_anthropic_caps_output_at_account_model_limit(
    maximum: int, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        anthropic_provider,
        "model_option",
        lambda *a, **kw: SimpleNamespace(
            efforts=(),
            default_effort=None,
            adaptive_thinking=False,
            max_output_tokens=maximum,
            context_window=None,
        ),
    )
    chat = AnthropicProvider(model="account-model", client=object()).session(
        system="test", tools=[]
    )
    assert chat._request_arguments(with_fallback=False)["max_tokens"] == min(
        64000, maximum
    )


def test_anthropic_unknown_legacy_model_uses_conservative_output_limit():
    chat = AnthropicProvider(model="claude-unknown-legacy", client=object()).session(
        system="test", tools=[]
    )
    arguments = chat._request_arguments(with_fallback=True)
    assert arguments["max_tokens"] == 4096
    assert "thinking" not in arguments
    assert "fallbacks" not in arguments


def test_registry_preserves_extensions_and_never_retries_an_executed_factory():
    registry = ProviderRegistry()
    calls = []
    registry.register(
        "example", lambda model: SimpleNamespace(name="example", model=model)
    )
    assert registry.create("example", "custom").model == "custom"
    with pytest.raises(ValueError, match="does not support selecting effort"):
        registry.create("example", "custom", effort="high")

    def factory(model, *, effort=None):
        calls.append((model, effort))
        raise TypeError("factory failed internally")

    registry.replace("example", factory)
    with pytest.raises(TypeError, match="factory failed internally"):
        registry.create("example", "custom", effort="high")
    assert calls == [("custom", "high")]


class SessionClient:
    def __init__(self, paths: AppPaths, *, old_daemon: bool = False):
        self.paths = paths
        self.old_daemon = old_daemon
        self.opened: dict[str, Any] = {}

    def call(self, method, params=None, **kwargs):
        params = params or {}
        if method == "session.open":
            self.opened = dict(params)
            return {
                "session": {
                    "provider": "openai",
                    "model": "gpt-5.3-codex",
                    "agent_mode": params.get("mode", "auto"),
                    **({} if self.old_daemon else {"effort": params.get("effort")}),
                },
                "bootstrap_sequence": 1,
            }
        if method == "session.resume":
            return {
                "session": {
                    "provider": "openai",
                    "model": "gpt-5.3-codex",
                    "effort": self.opened.get("effort"),
                    "agent_mode": self.opened.get("mode", "auto"),
                },
                "cursor": {"transport_received_sequence": 1},
            }
        if method == "session.events":
            return []
        if method == "session.ack":
            return {"cursor": {"transport_received_sequence": 1}}
        raise AssertionError(method)


def test_cli_resumes_and_refreshes_the_sessions_own_effort(tmp_path: Path):
    paths = AppPaths("test", *(tmp_path / key for key in ("c", "d", "s", "r")))
    client = cast(DaemonClient, SessionClient(paths))
    credentials = session.open_or_resume_session(
        client,
        repository=tmp_path,
        provider="openai",
        model=None,
        effort="low",
        resume_session_id=None,
    )
    assert credentials.effort == "low"
    resumed = session.open_or_resume_session(
        client,
        repository=tmp_path,
        provider="openai",
        model=None,
        effort="high",
        resume_session_id=credentials.session_id,
    )
    assert resumed.effort == "low"
    assert session._refresh_changes(client, resumed, io.StringIO()).effort == "low"


def test_old_daemon_cannot_silently_discard_effort_or_lose_resume_secret(
    tmp_path: Path,
):
    paths = AppPaths("test", *(tmp_path / key for key in ("c", "d", "s", "r")))
    client = SessionClient(paths, old_daemon=True)
    with pytest.raises(LlmCoordError, match="daemon restart") as failure:
        session.open_or_resume_session(
            cast(DaemonClient, client),
            repository=tmp_path,
            provider="openai",
            model=None,
            effort="high",
            resume_session_id=None,
        )
    assert failure.value.code is ErrorCode.PROTOCOL_MISMATCH
    assert session.session_resume_secret(paths, client.opened["session_id"])
