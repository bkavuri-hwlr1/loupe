"""The only module in this package permitted to import the Anthropic SDK.

The import is lazy so the core stays dependency-free: a build that never runs a
hosted model never needs the SDK installed, and one that does gets an actionable
error instead of an ImportError from somewhere deep in the execution path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.providers.accounts import load_api_key
from llm_cli.providers.base import (
    ModelTurn,
    StreamCallback,
    ToolCallRequest,
    ToolCallResult,
    context_overflow_error,
    mentions_any,
    shorten_tool_text,
)
from llm_cli.providers.catalog import model_option

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_FALLBACK_MODEL = "claude-opus-4-8"

# Streaming is not optional here.  An agent turn can emit a large amount of
# output, and a non-streaming request of this size hits the SDK's HTTP timeout
# before the model is finished.
_MAX_TOKENS = 64_000
# Used when account metadata does not publish the model's input limit.
_DEFAULT_CONTEXT_WINDOW = 200_000
# Automatic caching places the breakpoint on the last cacheable block, so each
# request reuses the system prompt, tools, and every earlier turn.
_CACHE_CONTROL = {"type": "ephemeral"}
_FALLBACK_BETA = "server-side-fallback-2026-06-01"
# Anthropic's explanations for a prompt that cannot fit: "prompt is too long:
# N tokens > M maximum" and "input length and `max_tokens` exceed context
# limit: N + M > L". Other 400s must not be mistaken for these.
_OVERFLOW_PHRASES = ("prompt is too long", "exceed context limit")
# Generation stopped because the context window filled before the reply ended.
_CONTEXT_STOP = "model_context_window_exceeded"


class _FallbackMode(Enum):
    AUTO = "automatic_default_model_fallback"


def _load_sdk() -> Any:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - exercised by install shape
        raise LlmCoordError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "the Anthropic adapter is not installed; reinstall with the "
            "'anthropic' extra, for example 'uv sync --extra anthropic'",
        ) from exc
    return anthropic


class AnthropicProvider:
    """Create Anthropic sessions for the provider-neutral coding harness."""

    name = "anthropic"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        fallback_model: str | _FallbackMode | None = _FallbackMode.AUTO,
        effort: str | None = None,
        client: Any | None = None,
        paths: AppPaths | None = None,
    ) -> None:
        self._model = model
        self._fallback_model = (
            (DEFAULT_FALLBACK_MODEL if model == DEFAULT_MODEL else None)
            if fallback_model is _FallbackMode.AUTO
            else fallback_model
        )
        self._effort = effort
        self._client = client
        self._paths = paths
        option = model_option(self.name, model, paths=paths)
        if effort is not None and effort not in option.efforts:
            raise ValueError(f"model {model!r} does not support effort {effort!r}")
        self._adaptive_thinking = option.adaptive_thinking
        # Account model metadata supplies exact limits. Preserve the modern
        # adaptive models' budget; older unknown models get a compatible cap.
        self._max_tokens = min(
            _MAX_TOKENS,
            option.max_output_tokens
            or (_MAX_TOKENS if option.adaptive_thinking else 4096),
        )
        self._context_window = option.context_window or _DEFAULT_CONTEXT_WINDOW

    @property
    def model(self) -> str:
        return self._model

    @property
    def input_token_budget(self) -> int:
        """Prompt tokens a request can carry while leaving room for output."""

        return max(1, self._context_window - self._max_tokens)

    def _ensure_client(self) -> Any:
        if self._client is None:
            anthropic = _load_sdk()
            # Resolve the profile file at task start, including logins made
            # after the daemon started. Preserve the SDK's native auth fallback.
            credential = load_api_key(self._paths, self.name)
            self._client = (
                anthropic.Anthropic(api_key=credential)
                if credential is not None
                else anthropic.Anthropic()
            )
        return self._client

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> AnthropicSession:
        return AnthropicSession(
            client=self._ensure_client(),
            model=self._model,
            fallback_model=self._fallback_model,
            effort=self._effort,
            adaptive_thinking=self._adaptive_thinking,
            max_tokens=self._max_tokens,
            system=system,
            tools=tools,
            state=state,
        )


class AnthropicSession:
    """One Claude conversation, owning its provider-native message history."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        fallback_model: str | None,
        effort: str | None,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
        adaptive_thinking: bool = False,
        max_tokens: int = _MAX_TOKENS,
    ) -> None:
        self._client = client
        self._model = model
        self._fallback_model = fallback_model
        self._effort = effort
        self._adaptive_thinking = adaptive_thinking
        self._max_tokens = max_tokens
        self._system = system
        self._tools = list(tools)
        self._messages = _messages_from_state(state)
        self._event_callback: StreamCallback | None = None

    def set_event_callback(self, callback: StreamCallback | None) -> None:
        self._event_callback = callback

    def snapshot(self) -> Mapping[str, object]:
        """Serialize provider-native blocks exactly as Claude returned them."""

        return {"messages": _json_value(self._messages)}

    def send_user(self, text: str) -> ModelTurn:
        restore = len(self._messages)
        self._messages.append({"role": "user", "content": text})
        return self._advance(restore)

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        restore = len(self._messages)
        self.record_tool_results(results)
        return self._advance(restore)

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        if not results:
            raise ValueError("a tool-result turn must carry at least one result")
        self._messages.append({"role": "user", "content": _result_blocks(results)})

    def summarize(
        self,
        instruction: str,
        *,
        max_tool_text: int | None = None,
        pending_results: Sequence[ToolCallResult] = (),
    ) -> ModelTurn:
        """Make one tools-disabled request without changing native history."""

        request: list[dict[str, Any]] = [
            *self._messages,
            {
                "role": "user",
                "content": [
                    *_result_blocks(pending_results),
                    {"type": "text", "text": instruction},
                ]
                if pending_results
                else instruction,
            },
        ]
        if max_tool_text is not None:
            request = [_shortened_message(item, max_tool_text) for item in request]
        callback, self._event_callback = self._event_callback, None
        try:
            # A summary is a side request: a rejection here must not change
            # how later task turns are sent.
            message = self._request(
                request, tool_choice={"type": "none"}, keep_fallback=True
            )
        finally:
            self._event_callback = callback
        return _turn_from_message(message)

    def replace_history(self, summary: str) -> None:
        # The API merges consecutive user turns, so the next prompt can follow
        # this message directly without an invented assistant reply.
        self._messages = [{"role": "user", "content": summary}]

    def _request_arguments(
        self,
        *,
        with_fallback: bool,
        messages: list[dict[str, Any]] | None = None,
        tool_choice: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": self._system,
            "messages": self._messages if messages is None else messages,
            "tools": self._tools,
            "cache_control": dict(_CACHE_CONTROL),
        }
        if tool_choice is not None:
            arguments["tool_choice"] = dict(tool_choice)
        if self._adaptive_thinking:
            arguments["thinking"] = {"type": "adaptive"}
        if self._effort is not None:
            arguments["output_config"] = {"effort": self._effort}
        if with_fallback and self._fallback_model is not None:
            arguments["betas"] = [_FALLBACK_BETA]
            arguments["fallbacks"] = [{"model": self._fallback_model}]
        return arguments

    def _advance(self, restore: int) -> ModelTurn:
        try:
            message = self._request(self._messages)
            if getattr(message, "stop_reason", None) == _CONTEXT_STOP:
                # The reply was cut off by the window, not finished. Treat it
                # like a rejected prompt so the caller can summarize and retry.
                raise context_overflow_error()
        except BaseException:
            # A failed request leaves native history as it was, so the caller
            # can summarize it and send the same prompt again.
            del self._messages[restore:]
            raise
        # Provider-native blocks -- thinking, tool_use, cache markers -- must be
        # replayed verbatim, so the assistant turn is appended as it arrived.
        self._messages.append({"role": "assistant", "content": message.content})
        return _turn_from_message(message)

    def _request(
        self,
        messages: list[dict[str, Any]],
        *,
        tool_choice: Mapping[str, object] | None = None,
        keep_fallback: bool = False,
    ) -> Any:
        anthropic = _load_sdk()
        try:
            try:
                return self._stream(
                    self._request_arguments(
                        with_fallback=True, messages=messages, tool_choice=tool_choice
                    )
                )
            except anthropic.BadRequestError as exc:
                # A refusal fallback is optional. Retry without the beta if
                # this deployment rejects it; both requests still need the
                # same provider-error translation below. An oversized prompt
                # would fail the same way without the beta.
                if self._fallback_model is None or _context_overflow(exc):
                    raise
                if not keep_fallback:
                    self._fallback_model = None
                return self._stream(
                    self._request_arguments(
                        with_fallback=False, messages=messages, tool_choice=tool_choice
                    )
                )
        except anthropic.APIStatusError as exc:
            if _context_overflow(exc):
                raise context_overflow_error(exc.status_code) from exc
            raise LlmCoordError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"the model provider returned HTTP {exc.status_code}",
                details=_status_error_details(exc, self._client),
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LlmCoordError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "the model provider could not be reached",
            ) from exc
        except TypeError as exc:
            # The SDK reports a missing credential as a bare TypeError at
            # request time. Only that specific message is translated; any other
            # TypeError is a real defect and must keep its traceback.
            if "authentication" not in str(exc).lower():
                raise
            raise LlmCoordError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "no Anthropic credential is configured; use /login anthropic "
                "or set ANTHROPIC_API_KEY",
            ) from exc

    def _stream(self, arguments: dict[str, Any]) -> Any:
        endpoint = (
            self._client.beta.messages
            if "betas" in arguments
            else self._client.messages
        )
        with endpoint.stream(**arguments) as stream:
            # Older injected clients may offer only the final-message helper.
            # Real SDK streams are iterable and yield deltas as they arrive.
            if self._event_callback is not None and hasattr(stream, "__iter__"):
                calls: dict[object, dict[str, object]] = {}
                for event in stream:
                    self._stream_event(event, calls)
            return stream.get_final_message()

    def _stream_event(self, event: Any, calls: dict[object, dict[str, object]]) -> None:
        assert self._event_callback is not None
        kind = _field(event, "type")
        index = _field(event, "index")
        if kind == "content_block_start":
            block = _field(event, "content_block")
            if _field(block, "type") == "tool_use":
                calls[index] = {
                    "call_id": _field(block, "id"),
                    "tool": _field(block, "name"),
                }
            return
        if kind != "content_block_delta":
            return
        delta = _field(event, "delta")
        delta_kind = _field(delta, "type")
        event_kind, field = {
            "text_delta": ("model.text.delta", "text"),
            "thinking_delta": ("model.reasoning.delta", "thinking"),
            "input_json_delta": ("model.tool.delta", "partial_json"),
        }.get(delta_kind, (None, None))
        # Signature/redacted-thinking blocks are intentionally never displayed.
        if event_kind is None or field is None:
            return
        text = _field(delta, field)
        if not isinstance(text, str) or not text:
            return
        payload: dict[str, object] = {"block_id": str(index)}
        if event_kind == "model.tool.delta":
            payload.update(calls.get(index, {}))
            payload["arguments_delta"] = text
        else:
            payload["text"] = text
        self._event_callback(event_kind, payload)


def _context_overflow(error: Any) -> bool:
    status = getattr(error, "status_code", None)
    if status == 413:
        # request_too_large: the request body itself exceeds the size limit.
        return True
    body = getattr(error, "body", None)
    details = body.get("error") if isinstance(body, Mapping) else None
    return (
        status == 400
        and isinstance(details, Mapping)
        and mentions_any(details.get("message"), _OVERFLOW_PHRASES)
    )


def _result_blocks(results: Sequence[ToolCallResult]) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_result",
            "tool_use_id": result.call_id,
            "content": result.content,
            "is_error": result.is_error,
        }
        for result in results
    ]


def _shortened_message(message: dict[str, Any], limit: int) -> dict[str, Any]:
    """Shorten tool payloads in a copy of one message for a summary request."""

    content = message.get("content")
    if not isinstance(content, list):
        return message
    blocks: list[Any] = []
    for block in content:
        kind = _field(block, "type")
        if kind == "tool_result" and isinstance(_field(block, "content"), str):
            data = dict(block) if isinstance(block, Mapping) else _json_value(block)
            data["content"] = shorten_tool_text(data["content"], limit)
            blocks.append(data)
        elif kind == "tool_use" and isinstance(_field(block, "input"), Mapping):
            data = dict(block) if isinstance(block, Mapping) else _json_value(block)
            data["input"] = {
                key: (
                    shorten_tool_text(value, limit) if isinstance(value, str) else value
                )
                for key, value in data["input"].items()
            }
            blocks.append(data)
        else:
            blocks.append(block)
    return {**message, "content": blocks}


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _status_error_details(error: Any, client: Any) -> dict[str, object]:
    """Keep private diagnostics separate from the locally authored error message."""

    result: dict[str, object] = {"status_code": error.status_code}
    body = error.body
    if not isinstance(body, Mapping):
        return result
    details = body.get("error")
    if not isinstance(details, Mapping):
        return result
    detail = details.get("message")
    if not isinstance(detail, str):
        return result
    # Never render the exception itself: it may contain request information.
    # A provider can also echo a rejected static credential in its explanation.
    credentials = {
        value
        for field in ("api_key", "auth_token")
        if isinstance(value := getattr(client, field, None), str) and value
    }
    for credential in sorted(credentials, key=len, reverse=True):
        detail = detail.replace(credential, "[redacted]")
    detail = " ".join(
        "".join(char if char.isprintable() else " " for char in detail).split()
    )
    if len(detail) > 500:
        detail = detail[:497] + "..."
    if detail:
        result["provider_message"] = detail
    return result


def _turn_from_message(message: Any) -> ModelTurn:
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: list[ToolCallRequest] = []
    for block in message.content:
        kind = getattr(block, "type", None)
        if kind == "text":
            text_parts.append(block.text)
        elif kind == "thinking" and isinstance(_field(block, "thinking"), str):
            reasoning_parts.append(_field(block, "thinking"))
        elif kind == "tool_use":
            arguments = block.input if isinstance(block.input, Mapping) else {}
            calls.append(
                ToolCallRequest(
                    call_id=str(block.id),
                    name=str(block.name),
                    arguments=dict(arguments),
                )
            )
    stop_details = getattr(message, "stop_details", None)
    usage = _usage(message)
    return ModelTurn(
        text="\n".join(text_parts).strip(),
        tool_calls=tuple(calls),
        stop_reason=str(getattr(message, "stop_reason", "end_turn")),
        usage=usage,
        # input_tokens excludes cached prompt tokens; the window holds them all.
        context_tokens=sum(usage.values()) if "input_tokens" in usage else None,
        reasoning="\n".join(reasoning_parts).strip(),
        refusal_category=(
            str(getattr(stop_details, "category", None))
            if stop_details is not None
            else None
        ),
    )


def _usage(message: Any) -> dict[str, int]:
    usage = getattr(message, "usage", None)
    if usage is None:
        return {}
    counts: dict[str, int] = {}
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        value = getattr(usage, field, None)
        if isinstance(value, int):
            counts[field] = value
    return counts


def _messages_from_state(state: Mapping[str, object] | None) -> list[dict[str, Any]]:
    """Validate a persisted native history before handing it to the SDK."""

    if state is None:
        return []
    messages = state.get("messages")
    if not isinstance(messages, list) or not all(
        isinstance(message, dict) for message in messages
    ):
        raise ValueError("Anthropic conversation state does not contain messages")
    restored: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role not in {"user", "assistant"} or content is None:
            raise ValueError("Anthropic conversation message is malformed")
        restored.append(dict(message))
    return restored


def _json_value(value: Any) -> Any:
    """Convert SDK models to JSON without flattening native content blocks."""

    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("Anthropic response contains a non-serializable content block")


__all__ = ["DEFAULT_FALLBACK_MODEL", "DEFAULT_MODEL", "AnthropicProvider"]
