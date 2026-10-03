"""Subscription transport with the real SDK, then our actual shared harness."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_openai_sdk_transport import _call, _message, _response, httpx, openai
from test_shared_agent_execution import _assert_completed, _request, _start

from llm_cli.daemon.service import DaemonService
from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.providers import catalog, codex_auth, codex_provider
from llm_cli.providers.base import ToolCallResult
from llm_cli.providers.codex_auth import Credentials, CredentialStore
from llm_cli.providers.codex_provider import CodexProvider


def _codex_events(response: dict[str, Any], failure: str | None = None) -> bytes:
    terminal = copy.deepcopy(response)
    terminal["output"] = []
    events = [
        {
            "type": "response.output_item.done",
            "sequence_number": index,
            "output_index": index,
            "item": item,
        }
        for index, item in enumerate(response["output"])
    ]
    if failure == "duplicate":
        events.append(events[0])
    if failure == "gap":
        events[0]["output_index"] = 1
    if failure == "empty":
        events = []
    if failure == "mismatch":
        terminal["output"] = [_message()]
    if failure != "truncated":
        kind = "incomplete" if failure == "incomplete" else "completed"
        terminal["status"] = kind
        events.append(
            {
                "type": f"response.{kind}",
                "sequence_number": len(events),
                "response": terminal,
            }
        )
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
    ).encode()


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[..., Any]
) -> None:
    def http_client(**kwargs: Any) -> Any:
        assert kwargs == {"follow_redirects": False, "trust_env": False}
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(
        codex_provider,
        "_load_sdk",
        lambda: SimpleNamespace(
            OpenAI=openai.OpenAI,
            DefaultHttpxClient=http_client,
        ),
    )


def _store(paths: AppPaths) -> CredentialStore:
    store = CredentialStore(paths)
    store.save(
        Credentials(
            "access-private",
            "refresh-private",
            time.time() + 3600,
            "account-private",
            "eu",
        )
    )
    return store


def test_subscription_sdk_request_is_pinned_and_checkpoint_has_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths(
        "test",
        tmp_path / "config",
        tmp_path / "data",
        tmp_path / "state",
        tmp_path / "run",
    )
    store = _store(paths)
    requests = []
    monkeypatch.setenv("OPENAI_API_KEY", "paid-api-must-not-be-used")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong.example/v1")
    monkeypatch.setenv("OPENAI_ORG_ID", "wrong-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "wrong-project")

    def handle(request: Any) -> Any:
        assert str(request.url) == "https://chatgpt.com/backend-api/codex/responses"
        assert request.headers["authorization"] == "Bearer access-private"
        assert request.headers["chatgpt-account-id"] == "account-private"
        assert request.headers["x-openai-internal-codex-residency"] == "eu"
        assert request.headers["originator"] == "llm-coord"
        assert not request.headers.get("openai-organization")
        assert not request.headers.get("openai-project")
        body = json.loads(request.content)
        requests.append(body)
        assert body["store"] is False and body["stream"] is True
        assert "max_output_tokens" not in body
        if len(requests) == 1:
            output = [
                {
                    "type": "reasoning",
                    "id": "rs",
                    "summary": [],
                    "encrypted_content": "opaque-reasoning",
                },
                _call("read", "read_file", {"path": "docs/a.md"}),
            ]
        else:
            assert body["input"][-1]["call_id"] == "read"
            reasoning = next(
                item for item in body["input"] if item.get("type") == "reasoning"
            )
            assert "status" not in reasoning and "content" not in reasoning
            function = next(
                item for item in body["input"] if item.get("type") == "function_call"
            )
            assert "async_" not in function and "namespace" not in function
            assert any(
                item.get("encrypted_content") == "opaque-reasoning"
                for item in body["input"]
            )
            output = [_message()]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_codex_events(_response(output)),
        )

    _install_transport(monkeypatch, handle)
    provider = CodexProvider(paths=paths)
    first = provider.session(system="broker instructions", tools=[])
    assert first.send_user("inspect").tool_calls[0].name == "read_file"
    restarted = provider.session(
        system="broker instructions", tools=[], state=first.snapshot()
    )
    assert (
        restarted.send_tool_results([ToolCallResult("read", "content")]).text == "done"
    )
    encoded = json.dumps(restarted.snapshot())
    assert all(
        secret not in encoded
        for secret in ("access-private", "refresh-private", "account-private")
    )
    store.logout()
    with pytest.raises(LlmCoordError, match="no ChatGPT login"):
        provider.session(system="test", tools=[]).send_user("must not fall back")
    assert len(requests) == 2


def test_discovered_effort_survives_token_refresh_between_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.resolve("effort-refresh", environ={}, home=tmp_path)
    store = CredentialStore(paths)
    store.save(
        Credentials(
            "access-before-refresh",
            "refresh-private",
            time.time() + 30,
            "account-private",
            None,
        )
    )
    # Model discovery must stay authoritative for models released after Loupe.
    option = catalog.ModelOption(
        "gpt-future-codex", "Future Codex", efforts=("low", "high")
    )
    monkeypatch.setattr(catalog, "_codex_models", lambda _: (option,))
    assert catalog.list_models(paths, "codex").models == (option,)
    monkeypatch.setattr(
        codex_auth,
        "_post",
        lambda *args, **kwargs: {
            "access_token": "access-after-refresh",
            "refresh_token": "refresh-after-refresh",
            "expires_in": 3600,
        },
    )
    requests: list[dict[str, Any]] = []

    def handle(request: Any) -> Any:
        assert request.headers["authorization"] == "Bearer access-after-refresh"
        body = json.loads(request.content)
        assert body["model"] == option.id
        assert body["reasoning"]["effort"] == "high"
        requests.append(body)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_codex_events(_response([_message()])),
        )

    _install_transport(monkeypatch, handle)
    first = CodexProvider(paths=paths, model=option.id, effort="high").session(
        system="test", tools=[]
    )
    assert first.send_user("first prompt").text == "done"
    # The daemon constructs the provider again for the next task. Rotation of
    # credentials must not turn the same selected effort into an invalid choice.
    second = CodexProvider(paths=paths, model=option.id, effort="high").session(
        system="test", tools=[], state=first.snapshot()
    )
    assert second.send_user("second prompt").text == "done"
    assert len(requests) == 2
    assert catalog.model_option("codex", option.id, paths=paths) == option


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_subscription_failures_do_not_follow_redirects_or_leak_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    paths = AppPaths(
        "test",
        tmp_path / "config",
        tmp_path / "data",
        tmp_path / "state",
        tmp_path / "run",
    )
    _store(paths)
    calls = []

    def handle(request: Any) -> Any:
        calls.append(request)
        return httpx.Response(
            status,
            headers={"location": "https://wrong.example"},
            json={
                "error": {"message": "access-private refresh-private account-private"}
            },
        )

    _install_transport(monkeypatch, handle)
    with pytest.raises(LlmCoordError) as caught:
        CodexProvider(paths=paths).session(system="test", tools=[]).send_user("test")
    expected: dict[str, object] = {"status_code": status}
    if status in {401, 429}:
        expected["provider_error"] = "authentication" if status == 401 else "rate_limit"
    assert caught.value.details == expected
    assert "private" not in str(caught.value) + json.dumps(caught.value.details)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("error", "category", "hint"),
    [
        (
            {
                "message": "Invalid value: 'ultra'. Supported values are: "
                "'none', 'minimal', 'low', 'medium', 'high', 'xhigh', and 'max'.",
            },
            "unsupported_effort",
            "/effort",
        ),
        (
            {"message": "unsupported value", "param": "reasoning.effort"},
            "unsupported_effort",
            "/effort",
        ),
        (
            {"detail": "The selected model is not supported with this account"},
            "unsupported_model",
            "/model",
        ),
        (
            {"message": "Unsupported parameter: 'max_output_tokens'."},
            "unsupported_parameter",
            "request",
        ),
        ({"message": "Invalid input"}, "request_rejected", "request"),
    ],
)
def test_subscription_rejections_explain_next_step_without_remote_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: dict[str, str],
    category: str,
    hint: str,
) -> None:
    paths = AppPaths(
        "test",
        tmp_path / "config",
        tmp_path / "data",
        tmp_path / "state",
        tmp_path / "run",
    )
    _store(paths)
    remote = {**error, "request": "access-private prompt-private"}

    def handle(request: Any) -> Any:
        return httpx.Response(400, json={"error": remote})

    _install_transport(monkeypatch, handle)
    with pytest.raises(LlmCoordError) as caught:
        CodexProvider(paths=paths).session(system="test", tools=[]).send_user("test")
    assert caught.value.details == {
        "status_code": 400,
        "provider_error": category,
    }
    assert hint in str(caught.value)
    assert "private" not in str(caught.value) + json.dumps(caught.value.details)
    assert error.get("message", error.get("detail")) not in str(caught.value)


def test_subscription_max_effort_uses_accepted_wire_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        assert json.loads(request.content)["reasoning"]["effort"] == "max"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_codex_events(_response([_message()])),
        )

    _install_transport(monkeypatch, handle)
    session = CodexProvider(paths=paths, model="gpt-6-astra", effort="max").session(
        system="test", tools=[]
    )
    assert session.send_user("test").text == "done"


def test_subscription_provider_shared_publication_and_durable_followup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    git_run: Callable[..., str],
) -> None:
    async def scenario() -> None:
        repo = repository_factory(tmp_path, {"docs/a.md": "base\n"})
        service = service_factory(tmp_path)
        _store(service.paths)
        requests = []

        def handle(request: Any) -> Any:
            body = json.loads(request.content)
            requests.append(body)
            outputs = [
                [_call("read-first", "read_file", {"path": "docs/a.md"})],
                [
                    _call(
                        "write-first",
                        "write_file",
                        {"path": "docs/a.md", "content": "subscription change\n"},
                    )
                ],
                [
                    _call(
                        "finish-first",
                        "finish_task",
                        {"answer": "changed", "summary": "changed"},
                    )
                ],
                [_call("read-next", "read_file", {"path": "docs/a.md"})],
                [
                    _call(
                        "finish-next",
                        "finish_task",
                        {"answer": "verified", "summary": "verified"},
                    )
                ],
            ]
            if len(requests) == 3:
                assert (repo / "docs/a.md").read_text() == "base\n"
            if len(requests) == 5:
                assert (
                    json.loads(body["input"][-1]["output"])["content"]
                    == "subscription change\n"
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_codex_events(_response(outputs[len(requests) - 1])),
            )

        _install_transport(monkeypatch, handle)
        service.initialize()
        try:
            await service.handle(_request("repo.add", {"path": str(repo)}))
            credentials = {
                "session_id": "subscription-test",
                "resume_secret": "resume-private",
            }
            opened = await service.handle(
                _request(
                    "session.open",
                    {
                        "path": str(repo),
                        "session_id": credentials["session_id"],
                        "resume_token_hash": hashlib.sha256(
                            credentials["resume_secret"].encode()
                        ).hexdigest(),
                        "provider": "codex",
                        "workspace": "shared",
                    },
                )
            )
            await service.handle(
                _request(
                    "session.ack",
                    {**credentials, "sequence": opened["bootstrap_sequence"]},
                )
            )
            refs, index = git_run(repo, "show-ref"), git_run(repo, "write-tree")
            task = await _start(
                service, repo, credentials, "subscription-edit", ("docs/",)
            )
            await asyncio.wait_for(task, 20)
            _assert_completed(service, "subscription-edit")
            assert (repo / "docs/a.md").read_text() == "subscription change\n"
            task = await _start(
                service, repo, credentials, "subscription-followup", ("docs/",)
            )
            await asyncio.wait_for(task, 20)
            _assert_completed(service, "subscription-followup")
            stored = service.store.session_conversation(credentials["session_id"])
            assert stored is not None and stored[0] == "codex"
            assert "access-private" not in json.dumps(stored)
            assert "refresh-private" not in json.dumps(stored)
            assert git_run(repo, "show-ref") == refs
            assert git_run(repo, "write-tree") == index
        finally:
            await service.drain()
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", ["truncated", "incomplete", "duplicate", "gap", "mismatch", "empty"]
)
def test_invalid_subscription_stream_never_exposes_completed_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
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
        response = _response(
            [
                _call(
                    "write", "write_file", {"path": "a.md", "content": "must not write"}
                )
            ]
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_codex_events(response, failure),
        )

    _install_transport(monkeypatch, handle)
    session = CodexProvider(paths=paths).session(system="test", tools=[])
    with pytest.raises(LlmCoordError) as caught:
        session.send_user("edit")
    assert caught.value.details == {"provider_error": "incomplete_response"}
    assert session.snapshot() == {"input": []}


@pytest.mark.parametrize(
    ("error_type", "category"),
    [(httpx.ConnectError, "connection"), (httpx.ReadTimeout, "timeout")],
)
def test_subscription_transport_failure_retains_only_safe_category(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[httpx.TransportError],
    category: str,
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
        raise error_type("access-private request-private", request=request)

    _install_transport(monkeypatch, handle)
    with pytest.raises(LlmCoordError) as caught:
        CodexProvider(paths=paths).session(system="test", tools=[]).send_user("test")
    assert caught.value.details == {"provider_error": category}
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("recovers", [True, False])
def test_subscription_connection_retries_are_bounded_and_preserve_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovers: bool
) -> None:
    paths = AppPaths(
        "test",
        tmp_path / "config",
        tmp_path / "data",
        tmp_path / "state",
        tmp_path / "run",
    )
    _store(paths)
    requests = []

    def handle(request: Any) -> Any:
        requests.append(json.loads(request.content))
        if len(requests) < 3 or not recovers:
            raise httpx.ReadError("access-private request-private", request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_codex_events(_response([_message()])),
        )

    _install_transport(monkeypatch, handle)
    session = CodexProvider(paths=paths).session(system="test", tools=[])
    if recovers:
        assert session.send_user("test").text == "done"
    else:
        with pytest.raises(LlmCoordError) as caught:
            session.send_user("test")
        assert caught.value.details == {"provider_error": "connection"}
        assert "private" not in str(caught.value)
        assert session.snapshot() == {"input": []}
    assert len(requests) == 3
    assert requests[0] == requests[1] == requests[2]


def test_subscription_does_not_retry_a_stream_that_has_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths(
        "test",
        tmp_path / "config",
        tmp_path / "data",
        tmp_path / "state",
        tmp_path / "run",
    )
    _store(paths)
    requests = []

    class InterruptedStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield _codex_events(
                _response([_call("write", "write_file", {"path": "a.md"})]),
                "truncated",
            )
            raise httpx.ReadError("stream interrupted")

    def handle(request: Any) -> Any:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=InterruptedStream(),
        )

    _install_transport(monkeypatch, handle)
    session = CodexProvider(paths=paths).session(system="test", tools=[])
    with pytest.raises(httpx.ReadError):
        session.send_user("edit")
    assert len(requests) == 1
    assert session.snapshot() == {"input": []}
