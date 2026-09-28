"""Subscription-backed model transport; execution stays in CodingAgentHarness."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from time import sleep
from types import SimpleNamespace
from typing import Any

from llm_cli import __version__
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.providers.catalog import model_option
from llm_cli.providers.codex_auth import CredentialStore
from llm_cli.providers.openai_provider import (
    OpenAISession,
    _json_value,
    _load_sdk,
    _provider_failure,
)

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_CODEX_MODEL = "gpt-5.6-terra"


class CodexProvider:
    name = "codex"

    def __init__(
        self,
        *,
        paths: AppPaths,
        model: str = DEFAULT_CODEX_MODEL,
        effort: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._paths = paths
        self._model = model
        self._effort = effort
        self._client = client
        option = model_option(self.name, model, paths=paths)
        if effort is not None and effort not in option.efforts:
            raise ValueError(
                f"model {model!r} does not support effort {effort!r}; "
                "use /effort to choose a supported level"
            )
        self._reasoning_supported = bool(option.efforts)

    @property
    def model(self) -> str:
        return self._model

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> OpenAISession:
        return OpenAISession(
            client=self._client or _SubscriptionClient(CredentialStore(self._paths)),
            model=self._model,
            effort=self._effort,
            reasoning_supported=self._reasoning_supported,
            system=system,
            tools=tools,
            state=state,
            # The subscription endpoint rejects max_output_tokens. The harness
            # continues to enforce its own turn/tool limits.
            max_output_tokens=None,
        )


class _SubscriptionClient:
    def __init__(self, store: CredentialStore) -> None:
        self.store = store
        self.responses = self

    @contextmanager
    def stream(self, **arguments: Any) -> Iterator[Any]:
        # Resolve/refresh before every request so daemon workers observe login,
        # logout and token rotation without a daemon restart. Never fall back
        # to OPENAI_API_KEY or an environment-selected URL/organization.
        sdk = _load_sdk()
        credentials = self.store.credentials()
        headers = {
            "ChatGPT-Account-Id": credentials.account_id,
            "originator": "llm-coord",
            "User-Agent": f"llm-coord/{__version__}",
        }
        if credentials.residency:
            headers["x-openai-internal-codex-residency"] = credentials.residency
        try:
            with (
                sdk.OpenAI(
                    api_key=credentials.access_token,
                    base_url=CODEX_BASE_URL,
                    organization="",
                    project="",
                    default_headers=headers,
                    http_client=sdk.DefaultHttpxClient(
                        follow_redirects=False, trust_env=False
                    ),
                    max_retries=0,
                    timeout=120,
                ) as client,
                _create_subscription_stream(client, arguments) as stream,
            ):
                yield _SubscriptionStream(stream)
        except Exception as exc:
            failure = _provider_failure(exc, None)
            if failure is None:
                raise
            status = getattr(exc, "status_code", None)
            message = failure.message
            category = _subscription_error_category(status, getattr(exc, "body", None))
            details: dict[str, object] = {}
            if failure.details and "provider_error" in failure.details:
                details["provider_error"] = failure.details["provider_error"]
            if type(status) is int:
                details["status_code"] = status
            if category is not None:
                message = _SUBSCRIPTION_ERROR_MESSAGES[category]
                details["provider_error"] = category
            # Subscription errors never include remote bodies, which may echo
            # headers. Tokens/identity never enter checkpoints or task events.
            raise LlmCoordError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                message,
                details=details,
            ) from None


def _create_subscription_stream(client: Any, arguments: dict[str, Any]) -> Any:
    # A transport failure before response headers can be transient. Retry only
    # opening the stream: once it starts, never replay deltas or tool requests.
    # Keep SDK retries disabled so HTTP rejections and timeouts are not retried.
    retries = 0
    while True:
        try:
            return client.responses.create(**arguments, stream=True)
        except Exception as exc:
            failure = _provider_failure(exc, None)
            if (
                retries >= 2
                or failure is None
                or not failure.details
                or failure.details.get("provider_error") != "connection"
            ):
                raise
            sleep(0.5 * 2**retries)
            retries += 1


_SUBSCRIPTION_ERROR_MESSAGES = {
    "unsupported_effort": (
        "the selected reasoning effort is not accepted by Codex; "
        "use /effort to choose a supported level"
    ),
    "unsupported_model": (
        "the selected model is unavailable through ChatGPT; "
        "choose a supported subscription model with /model"
    ),
    "unsupported_parameter": (
        "Codex rejected an unsupported request setting; "
        "refresh available models with /model --refresh"
    ),
    "request_rejected": (
        "Codex rejected the request; check the selected /model and /effort"
    ),
    "authentication": "ChatGPT authorization was rejected; run /login codex",
    "rate_limit": (
        "ChatGPT subscription is rate limited or has reached its usage limit; "
        "retry later"
    ),
}


def _subscription_error_category(status: object, body: object) -> str | None:
    """Recognize fixed diagnostics without returning any remote response text."""

    if status == 401:
        return "authentication"
    if status == 429:
        return "rate_limit"
    if status != 400:
        return None
    if isinstance(body, Mapping):
        # SDK versions expose either the inner error or its enclosing object.
        body = body.get("error", body)
    if not isinstance(body, Mapping):
        return "request_rejected"
    if body.get("param") in ("reasoning.effort", "reasoning[effort]"):
        return "unsupported_effort"
    for field in ("message", "detail"):
        value = body.get(field)
        if not isinstance(value, str):
            continue
        message = value[:2000].lower()
        # The subscription endpoint's enum rejection omits the parameter name.
        # Match its specific effort vocabulary rather than arbitrary echoed text.
        if (
            message.startswith("invalid value:")
            and "supported values are:" in message
            and all(f"'{level}'" in message for level in ("minimal", "xhigh", "max"))
        ):
            return "unsupported_effort"
        if "model is not supported" in message:
            return "unsupported_model"
        if message.startswith("unsupported parameter:"):
            return "unsupported_parameter"
    return "request_rejected"


class _SubscriptionStream:
    def __init__(self, events: Iterator[Any]) -> None:
        self.events = events
        self._final_response: Any | None = None

    def get_final_response(self) -> Any:
        if self._final_response is None:
            for _ in self:
                pass
        if self._final_response is None:
            raise self._invalid()
        return self._final_response

    def __iter__(self) -> Iterator[dict[str, Any]]:
        # Codex sends full native items in output_item.done, but its terminal
        # response.completed can have output=[]. The SDK's Responses stream
        # accumulator replaces its assembled output with that empty list.
        # Keep completed items ourselves while allowing public display deltas
        # through. Executable calls remain gated on a valid complete response.
        if self._final_response is not None:
            return
        items: dict[int, Any] = {}
        for event in self.events:
            # Preserve wire fields and aliases. SDK defaults such as reasoning
            # status=None and function_call async_=None are not accepted as
            # subscription input fields on the next turn.
            data = _json_value(
                event.model_dump(mode="json", exclude_unset=True, by_alias=True)
            )
            kind = data.get("type")
            if kind == "response.output_item.done":
                index = data.get("output_index")
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or not 0 <= index < 1024
                    or index in items
                ):
                    raise self._invalid()
                items[index] = data.get("item")
            elif kind in {"error", "response.failed", "response.incomplete"}:
                raise self._invalid()
            elif kind == "response.completed":
                response = data.get("response")
                if (
                    not isinstance(response, dict)
                    or response.get("status") != "completed"
                ):
                    raise self._invalid()
                output = response.get("output")
                if items:
                    if sorted(items) != list(range(len(items))):
                        raise self._invalid()
                    completed = [items[index] for index in range(len(items))]
                    if output and output != completed:
                        raise self._invalid()
                    output = completed
                if not isinstance(output, list) or not output:
                    raise self._invalid()
                self._final_response = SimpleNamespace(
                    status="completed", output=output, usage=response.get("usage")
                )
                yield data
                return
            yield data
        raise self._invalid()

    @staticmethod
    def _invalid() -> LlmCoordError:
        return LlmCoordError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "the subscription provider returned an incomplete or invalid stream; "
            "no tools were run",
            details={"provider_error": "incomplete_response"},
        )


__all__ = ["CodexProvider"]
