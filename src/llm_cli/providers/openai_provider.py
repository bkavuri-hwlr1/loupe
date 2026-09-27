"""Optional OpenAI Responses adapter with durable native conversation replay."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from typing import Any

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.providers.accounts import load_api_key
from llm_cli.providers.base import (
    ModelTurn,
    StreamCallback,
    ToolCallRequest,
    ToolCallResult,
)
from llm_cli.providers.catalog import model_option

DEFAULT_MODEL = "gpt-5.3-codex"
_MAX_OUTPUT_TOKENS = 32_000


def _load_sdk() -> Any:
    try:
        import openai
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise LlmCoordError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "the OpenAI adapter is not installed; reinstall with the 'openai' "
            "extra, for example 'uv sync --extra openai'",
        ) from exc
    return openai


class OpenAIProvider:
    """Construct without credentials; resolve the optional SDK at session start."""

    name = "openai"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        effort: str | None = None,
        client: Any | None = None,
        paths: AppPaths | None = None,
    ) -> None:
        self._model = model
        self._effort = effort
        self._client = client
        self._paths = paths
        option = model_option(self.name, model, paths=paths)
        if effort is not None and effort not in option.efforts:
            raise ValueError(f"model {model!r} does not support effort {effort!r}")
        self._reasoning_supported = bool(option.efforts)
        self._max_output_tokens = min(
            _MAX_OUTPUT_TOKENS,
            option.max_output_tokens
            or (_MAX_OUTPUT_TOKENS if self._reasoning_supported else 4096),
        )

    @property
    def model(self) -> str:
        return self._model

    def _ensure_client(self) -> Any:
        if self._client is None:
            sdk = _load_sdk()
            credential = load_api_key(self._paths, self.name)
            if not credential or not credential.strip():
                raise LlmCoordError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "no OpenAI credential is configured; use /login openai "
                    "or set OPENAI_API_KEY",
                )
            self._client = sdk.OpenAI(api_key=credential)
        return self._client

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> OpenAISession:
        return OpenAISession(
            client=self._ensure_client(),
            model=self._model,
            effort=self._effort,
            reasoning_supported=self._reasoning_supported,
            max_output_tokens=self._max_output_tokens,
            system=system,
            tools=tools,
            state=state,
        )


class OpenAISession:
    """Own Responses input items, including opaque encrypted reasoning blocks."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        effort: str | None,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
        max_output_tokens: int | None = _MAX_OUTPUT_TOKENS,
        reasoning_supported: bool = True,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._system = system
        self._tools = [_function_tool(tool) for tool in tools]
        self._input = _input_from_state(state)
        self._max_output_tokens = max_output_tokens
        self._reasoning_supported = reasoning_supported
        self._event_callback: StreamCallback | None = None

    def set_event_callback(self, callback: StreamCallback | None) -> None:
        self._event_callback = callback

    def snapshot(self) -> Mapping[str, object]:
        return {"input": _json_value(self._input)}

    def send_user(self, text: str) -> ModelTurn:
        if _pending_calls(self._input):
            raise ValueError("OpenAI conversation has unanswered tool calls")
        self._input.append({"role": "user", "content": text})
        return self._advance()

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        """Record actual outcomes without requesting an additional model turn."""

        pending = _pending_calls(self._input)
        result_ids = [result.call_id for result in results]
        if (
            not results
            or len(set(result_ids)) != len(result_ids)
            or set(result_ids) != pending
        ):
            raise ValueError("OpenAI tool results must answer each pending call once")
        self._input.extend(
            {
                "type": "function_call_output",
                "call_id": result.call_id,
                # Responses has no is_error field. An explicit envelope retains
                # the broker's distinction without depending on content wording.
                "output": json.dumps(
                    {"content": result.content, "is_error": result.is_error},
                    ensure_ascii=False,
                ),
            }
            for result in results
        )

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        self.record_tool_results(results)
        return self._advance()

    def _advance(self) -> ModelTurn:
        arguments = {
            "model": self._model,
            "instructions": self._system,
            "input": _json_value(self._input),
            "tools": _json_value(self._tools),
            "store": False,
        }
        if self._reasoning_supported:
            arguments["include"] = ["reasoning.encrypted_content"]
            reasoning = {}
            if self._effort is not None:
                reasoning["effort"] = self._effort
            if self._event_callback is not None:
                # Display only summaries; preserve opaque native replay separately.
                reasoning["summary"] = "auto"
            if reasoning:
                arguments["reasoning"] = reasoning
        if self._max_output_tokens is not None:
            arguments["max_output_tokens"] = self._max_output_tokens
        try:
            with self._client.responses.stream(**arguments) as stream:
                if self._event_callback is not None and hasattr(stream, "__iter__"):
                    calls: dict[str, dict[str, object]] = {}
                    for event in stream:
                        self._stream_event(event, calls)
                response = stream.get_final_response()
        except Exception as exc:
            failure = _provider_failure(exc, self._client)
            if failure is not None:
                raise failure from exc
            raise
        if _field(response, "status") != "completed":
            raise LlmCoordError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "the model provider did not complete its response; no tools were run",
                details={"provider_error": "incomplete_response"},
            )
        try:
            output = _json_value(_field(response, "output"))
            if not isinstance(output, list):
                raise ValueError("OpenAI response output must be a list")
            for item in output:
                _validate_item(item, output=True)
                _remove_sdk_annotations(item)
            # Validate the complete turn before returning any executable calls.
            _pending_calls([*self._input, *output])
            turn = _turn_from_output(output, response)
        except ValueError as exc:
            raise LlmCoordError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "the model provider returned an invalid response; no tools were run",
                details={"provider_error": "invalid_response"},
            ) from exc
        self._input.extend(output)
        return turn

    def _stream_event(self, event: Any, calls: dict[str, dict[str, object]]) -> None:
        assert self._event_callback is not None
        kind = _field(event, "type")
        if kind == "response.output_item.added":
            item = _field(event, "item")
            if _field(item, "type") == "function_call":
                item_id = _field(item, "id")
                if isinstance(item_id, str):
                    calls[item_id] = {
                        "call_id": _field(item, "call_id"),
                        "tool": _field(item, "name"),
                    }
            return
        event_kind = {
            "response.output_text.delta": "model.text.delta",
            "response.refusal.delta": "model.text.delta",
            "response.reasoning_summary_text.delta": "model.reasoning.delta",
            "response.function_call_arguments.delta": "model.tool.delta",
        }.get(kind)
        # Do not forward raw reasoning-text events or encrypted native content.
        delta = _field(event, "delta")
        if event_kind is None or not isinstance(delta, str) or not delta:
            return
        item_id = str(_field(event, "item_id") or "")
        part_index = _field(event, "summary_index")
        if part_index is None:
            part_index = _field(event, "content_index")
        payload: dict[str, object] = {
            "block_id": f"{item_id}:{part_index if part_index is not None else 0}"
        }
        if event_kind == "model.tool.delta":
            payload.update(calls.get(item_id, {}))
            payload["arguments_delta"] = delta
        else:
            payload["text"] = delta
        self._event_callback(event_kind, payload)


def _function_tool(tool: Mapping[str, object]) -> dict[str, object]:
    name = tool.get("name")
    description = tool.get("description")
    schema = tool.get("input_schema")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(description, str)
        or not isinstance(schema, Mapping)
    ):
        raise ValueError("OpenAI function tool schema is malformed")
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": _json_value(schema),
        # Broker tools have optional fields. Responses otherwise normalizes a
        # missing strict setting into a schema requiring every property.
        "strict": False,
    }


def _remove_sdk_annotations(item: dict[str, Any]) -> None:
    """The stream helper adds parsed views that are not Responses input fields."""

    if item["type"] == "function_call":
        item.pop("parsed_arguments", None)
    elif item["type"] == "message":
        for part in item["content"]:
            if part["type"] == "output_text":
                part.pop("parsed", None)


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _json_value(value: Any) -> Any:
    """Copy JSON data and SDK models without flattening provider-native items."""

    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("OpenAI native object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("OpenAI native history contains a non-finite number")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("OpenAI native history contains a non-JSON value")


def _input_from_state(state: Mapping[str, object] | None) -> list[dict[str, Any]]:
    if state is None:
        return []
    items = _json_value(state.get("input"))
    if not isinstance(items, list):
        raise ValueError("OpenAI conversation state does not contain input items")
    for item in items:
        _validate_item(item)
    _pending_calls(items)
    return items


def _validate_item(item: Any, *, output: bool = False) -> None:
    if not isinstance(item, dict):
        raise ValueError("OpenAI native input item must be an object")
    kind = item.get("type")
    if kind is None and not output:
        if item.get("role") == "user" and isinstance(item.get("content"), str):
            return
        raise ValueError("OpenAI conversation input message is malformed")
    if not isinstance(kind, str) or not kind:
        raise ValueError("OpenAI native input item has no type")
    status = item.get("status")
    if status is not None and status != "completed":
        raise ValueError("OpenAI native output item is not complete")
    if kind == "function_call":
        for key in ("call_id", "name", "arguments"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError("OpenAI function call is malformed")
        _arguments(item["arguments"])
    elif kind == "function_call_output":
        if output or not isinstance(item.get("call_id"), str):
            raise ValueError("OpenAI function output is malformed")
        if not isinstance(item.get("output"), str):
            raise ValueError("OpenAI function output must be text")
    elif kind == "message":
        if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
            raise ValueError("OpenAI assistant message is malformed")
        for part in item["content"]:
            if not isinstance(part, dict):
                raise ValueError("OpenAI assistant message content is malformed")
            part_type = part.get("type")
            field = "text" if part_type == "output_text" else "refusal"
            if part_type not in {"output_text", "refusal"} or not isinstance(
                part.get(field), str
            ):
                raise ValueError("OpenAI assistant message content is malformed")
    elif kind == "reasoning":
        if not isinstance(item.get("summary"), list):
            raise ValueError("OpenAI reasoning item is malformed")
        encrypted = item.get("encrypted_content")
        if encrypted is not None and not isinstance(encrypted, str):
            raise ValueError("OpenAI encrypted reasoning is malformed")
    else:
        # Only function tools are offered. Reject provider-hosted tool output
        # instead of accepting actions outside the broker's authority.
        raise ValueError("OpenAI response contains an unsupported native item")


def _arguments(encoded: str) -> dict[str, object]:
    def constant(value: str) -> None:
        raise ValueError("OpenAI function arguments contain a non-JSON number")

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("OpenAI function arguments contain duplicate keys")
            result[key] = value
        return result

    parsed = json.loads(
        encoded, parse_constant=constant, object_pairs_hook=object_pairs
    )
    if not isinstance(parsed, dict):
        raise ValueError("OpenAI function arguments must be an object")
    _json_value(parsed)
    return parsed


def _pending_calls(items: Sequence[Mapping[str, Any]]) -> set[str]:
    seen: set[str] = set()
    pending: set[str] = set()
    answering = False
    for item in items:
        kind = item.get("type")
        if kind == "function_call":
            call_id = item["call_id"]
            if call_id in seen or answering:
                raise ValueError(
                    "OpenAI conversation has duplicate or unanswered calls"
                )
            seen.add(call_id)
            pending.add(call_id)
        elif kind == "function_call_output":
            call_id = item["call_id"]
            if call_id not in pending:
                raise ValueError("OpenAI tool output does not match a pending call")
            pending.remove(call_id)
            answering = bool(pending)
        elif answering or (item.get("role") == "user" and pending):
            raise ValueError("OpenAI conversation has unanswered tool calls")
    if answering:
        raise ValueError("OpenAI conversation contains a partial tool-result batch")
    return pending


def _turn_from_output(output: list[dict[str, Any]], response: Any) -> ModelTurn:
    text: list[str] = []
    reasoning: list[str] = []
    calls: list[ToolCallRequest] = []
    refused = False
    for item in output:
        if item["type"] == "function_call":
            calls.append(
                ToolCallRequest(
                    item["call_id"], item["name"], _arguments(item["arguments"])
                )
            )
        elif item["type"] == "message":
            for part in item["content"]:
                if part["type"] == "refusal":
                    refused = True
                    text.append(part["refusal"])
                elif part["type"] == "output_text":
                    text.append(part["text"])
        elif item["type"] == "reasoning":
            for part in item["summary"]:
                if (
                    isinstance(part, Mapping)
                    and part.get("type") == "summary_text"
                    and isinstance(part.get("text"), str)
                ):
                    reasoning.append(part["text"])
    return ModelTurn(
        text="\n".join(text).strip(),
        tool_calls=() if refused else tuple(calls),
        stop_reason="refusal" if refused else "tool_use" if calls else "end_turn",
        usage=_usage(response),
        refusal_category="provider_refusal" if refused else None,
        reasoning="\n".join(reasoning).strip(),
    )


def _usage(response: Any) -> dict[str, int]:
    usage = _field(response, "usage")
    counts: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens"):
        value = _field(usage, name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            counts[name] = value
    for detail_name, field, name in (
        ("input_tokens_details", "cached_tokens", "cache_read_input_tokens"),
        ("output_tokens_details", "reasoning_tokens", "reasoning_tokens"),
    ):
        value = _field(_field(usage, detail_name), field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            # These are subsets of the top-level counts, never added to them.
            counts[name] = value
    return counts


def _provider_failure(error: Exception, client: Any) -> LlmCoordError | None:
    """Recognize SDK errors without importing an optional SDK for injected clients."""

    names = {kind.__name__ for kind in type(error).__mro__}
    status = getattr(error, "status_code", None)
    if "APIStatusError" in names and isinstance(status, int):
        return LlmCoordError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            f"the model provider returned HTTP {status}",
            details=_status_error_details(error, client),
        )
    if "APITimeoutError" in names:
        return LlmCoordError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "the model provider timed out",
            details={"provider_error": "timeout"},
        )
    if "APIConnectionError" in names:
        return LlmCoordError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "the model provider could not be reached",
            details={"provider_error": "connection"},
        )
    if "APIError" in names or (
        isinstance(error, RuntimeError)
        and (
            "response.completed" in str(error)
            or str(error).startswith(
                "Expected to have received `response.created` before `"
            )
        )
    ):
        return LlmCoordError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "the model provider interrupted its response; no tools were run",
            details={"provider_error": "incomplete_response"},
        )
    return None


def _status_error_details(error: Any, client: Any) -> dict[str, object]:
    details: dict[str, object] = {"status_code": error.status_code}
    body = getattr(error, "body", None)
    if not isinstance(body, Mapping):
        return details
    # SDK APIStatusError.body can be the inner error, or the full envelope.
    body = body.get("error", body)
    if not isinstance(body, Mapping) or not isinstance(body.get("message"), str):
        return details
    message = body["message"]
    credentials = {
        value
        for value in (
            getattr(client, "api_key", None),
            os.environ.get("OPENAI_API_KEY"),
        )
        if isinstance(value, str) and value
    }
    for credential in sorted(credentials, key=len, reverse=True):
        message = message.replace(credential, "[redacted]")
    message = " ".join(
        "".join(char if char.isprintable() else " " for char in message).split()
    )
    if len(message) > 500:
        message = message[:497] + "..."
    if message:
        details["provider_message"] = message
    return details


__all__ = ["DEFAULT_MODEL", "OpenAIProvider"]
