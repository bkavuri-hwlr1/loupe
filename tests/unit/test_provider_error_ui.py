"""Provider failures retain safe recovery hints through durable event replay."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from llm_cli.agent import harness
from llm_cli.cli.render import EventRenderer, render_event
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.protocol.envelopes import Request
from llm_cli.providers.base import ModelTurn, ToolCallResult


class _RejectedProvider:
    name = "rejected"
    model = "fixture-model"

    def __init__(self, category: object) -> None:
        self.category = category

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> _RejectedProvider:
        return self

    def snapshot(self) -> Mapping[str, object]:
        return {}

    def send_user(self, text: str) -> ModelTurn:
        raise LlmCoordError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "PRIVATE_RESPONSE_TEXT",
            {
                "status_code": 400,
                "provider_error": self.category,
                "provider_message": "PRIVATE_RESPONSE_TEXT",
                "authorization": "PRIVATE_API_KEY",
            },
        )

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        raise AssertionError("No tools should run for a rejected request")

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        raise AssertionError("No tools should run for a rejected request")


def _event(kind: str, **payload: object) -> dict[str, Any]:
    return {"event_type": kind, "payload": payload, "task_id": "task-error"}


@pytest.mark.parametrize(
    ("category", "hint"),
    [
        ("unsupported_effort", "/effort to choose a supported level"),
        ("unsupported_model", "/model --refresh"),
        ("unsupported_parameter", "Loupe may need an update"),
        ("authentication", "/login to reconnect"),
        ("rate_limit", "Wait before retrying"),
        ("request_rejected", "Try /model or /effort"),
        ("connection", "Check your connection"),
        ("timeout", "timed out"),
        ("incomplete_response", "ended before it completed"),
        ("invalid_response", "choose another model"),
    ],
)
def test_shared_request_failure_replays_one_actionable_private_safe_error(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    request_factory: Callable[[str, dict[str, Any]], Request],
    monkeypatch: pytest.MonkeyPatch,
    category: str,
    hint: str,
) -> None:
    # Failures in transit are retried first; keep the waits out of the test.
    monkeypatch.setattr(harness, "_RETRY_DELAYS", (0.0, 0.0, 0.0))
    transient = category in {"connection", "timeout", "incomplete_response"}

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"README.md": "base\n"})
        service = service_factory(tmp_path)
        provider = _RejectedProvider(category)
        service.providers.register(provider.name, lambda model: provider)
        service.initialize()
        try:
            await service.handle(request_factory("repo.add", {"path": str(repository)}))
            secret = "fixture-resume-secret"
            opened = await service.handle(
                request_factory(
                    "session.open",
                    {
                        "session_id": "session-error",
                        "path": str(repository),
                        "resume_token_hash": hashlib.sha256(
                            secret.encode()
                        ).hexdigest(),
                        "provider": provider.name,
                        "model": provider.model,
                        "workspace": "shared",
                    },
                )
            )
            await service.handle(
                request_factory(
                    "session.ack",
                    {
                        "session_id": "session-error",
                        "resume_secret": secret,
                        "sequence": opened["bootstrap_sequence"],
                    },
                )
            )
            await service.handle(
                request_factory(
                    "task.run",
                    {
                        "session_id": "session-error",
                        "resume_secret": secret,
                        "path": str(repository),
                        "task_id": "task-error",
                        "title": "Inspect the readme",
                        "scopes": ["README.md"],
                    },
                )
            )
            await asyncio.wait_for(service._background_tasks[("task-error", 1)], 10)
            events = await service.handle(
                request_factory("task.events", {"task_id": "task-error"})
            )
            failures = [
                event for event in events if event["event_type"].endswith("failed")
            ]
            assert [event["event_type"] for event in failures] == [
                "execution.failed",
                "execution.background_failed",
            ]
            assert failures[-1]["payload"] == {
                "failure_code": "PROVIDER_UNAVAILABLE",
                "provider_status": 400,
                "provider_error": category,
            }
            retries = [e for e in events if e["event_type"] == "model.retrying"]
            assert len(retries) == (3 if transient else 0)
            before = copy.deepcopy(events)
            for plain in (True, False):
                output = io.StringIO()
                renderer = EventRenderer(output, plain=plain)
                for event in events:
                    renderer.render(event)
                renderer.finish()
                text = output.getvalue()
                assert text.count("✗") == 1
                assert "HTTP 400" in text
                assert hint in text
                assert "Done " not in text
                assert "PRIVATE_RESPONSE_TEXT" not in text
                assert "PRIVATE_API_KEY" not in text
            assert events == before
            assert "PRIVATE_RESPONSE_TEXT" not in json.dumps(events)
            assert "PRIVATE_API_KEY" not in json.dumps(events)
            assert render_event(failures[-1]) == (
                "  ✗ run failed (PROVIDER_UNAVAILABLE; HTTP 400)"
            )
            task = service.store.get_task("task-error")
            assert task is not None and task.state == "failed"
            assert (repository / "README.md").read_text() == "base\n"
        finally:
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "category",
    [
        "PRIVATE_RESPONSE_TEXT",
        ["authentication"],
        {"message": "PRIVATE_RESPONSE_TEXT"},
        None,
    ],
)
def test_unknown_provider_classification_is_not_persisted(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    request_factory: Callable[[str, dict[str, Any]], Request],
    category: object,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"README.md": "base\n"})
        service = service_factory(tmp_path)
        service.initialize()
        try:
            await service.handle(request_factory("repo.add", {"path": str(repository)}))
            accepted = await service.handle(
                request_factory(
                    "task.run",
                    {
                        "path": str(repository),
                        "task_id": "task-error",
                        "title": "Inspect the readme",
                        "scopes": ["README.md"],
                        "claim_only": True,
                    },
                )
            )
            task = service.store.get_task("task-error")
            claim = service.store.get_claim(accepted["claim"]["claim_id"])
            assert task is not None and claim is not None
            service._fail_unstarted_launch(
                task,
                claim,
                LlmCoordError(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "PRIVATE_RESPONSE_TEXT",
                    {"status_code": 400, "provider_error": category},
                ),
            )
            events = await service.handle(
                request_factory("task.events", {"task_id": "task-error"})
            )
            failure = next(
                event
                for event in events
                if event["event_type"] == "execution.background_failed"
            )
            assert failure["payload"] == {
                "failure_code": "PROVIDER_UNAVAILABLE",
                "provider_status": 400,
            }
            assert "PRIVATE_RESPONSE_TEXT" not in json.dumps(events)
        finally:
            service.close()

    asyncio.run(scenario())


def test_an_unclassified_failure_records_its_type_but_not_its_text(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    request_factory: Callable[[str, dict[str, Any]], Request],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"README.md": "base\n"})
        service = service_factory(tmp_path)
        service.initialize()
        try:
            await service.handle(request_factory("repo.add", {"path": str(repository)}))
            accepted = await service.handle(
                request_factory(
                    "task.run",
                    {
                        "path": str(repository),
                        "task_id": "task-error",
                        "title": "Inspect the readme",
                        "scopes": ["README.md"],
                        "claim_only": True,
                    },
                )
            )
            task = service.store.get_task("task-error")
            claim = service.store.get_claim(accepted["claim"]["claim_id"])
            assert task is not None and claim is not None
            service._fail_unstarted_launch(
                task, claim, RuntimeError("PRIVATE_RESPONSE_TEXT")
            )
            events = await service.handle(
                request_factory("task.events", {"task_id": "task-error"})
            )
            failure = next(
                event
                for event in events
                if event["event_type"] == "execution.background_failed"
            )
            assert failure["payload"] == {
                "failure_code": "EXECUTION_FAILED",
                "error_type": "RuntimeError",
            }
            assert "PRIVATE_RESPONSE_TEXT" not in json.dumps(events)
        finally:
            service.close()

    asyncio.run(scenario())


def test_old_provider_failure_records_deduplicate_and_offer_recovery() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output, plain=True)
    renderer.render(_event("execution.failed", failure_code="PROVIDER_UNAVAILABLE"))
    renderer.render(_event("claim.released"))
    renderer.render(
        _event(
            "execution.background_failed",
            failure_code="PROVIDER_UNAVAILABLE",
            provider_status=400,
        )
    )
    renderer.render(
        _event(
            "execution.background_failed",
            failure_code="PROVIDER_UNAVAILABLE",
            provider_status=400,
        )
    )
    renderer.finish()
    assert output.getvalue().count("✗") == 1
    assert "The provider rejected this request (HTTP 400)." in output.getvalue()
    assert "Try /model or /effort" in output.getvalue()


def test_interruption_flushes_provider_failure_once_and_retry_is_visible() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output, plain=True)
    renderer.render(_event("execution.failed", failure_code="PROVIDER_UNAVAILABLE"))
    renderer.finish()
    renderer.finish()
    assert output.getvalue().count("✗") == 1
    renderer.render(_event("execution.preparing"))
    renderer.render(
        _event(
            "execution.background_failed",
            failure_code="PROVIDER_UNAVAILABLE",
            provider_status=401,
        )
    )
    renderer.finish()
    assert output.getvalue().count("✗") == 2
    assert "/login" in output.getvalue()


def test_other_failures_keep_their_order_and_are_not_suppressed() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output, plain=True)
    renderer.render(_event("execution.failed", failure_code="PROVIDER_UNAVAILABLE"))
    renderer.render(_event("execution.failed", failure_code="TARGET_MOVED"))
    renderer.finish()
    assert output.getvalue().count("✗") == 2
    assert output.getvalue().index("PROVIDER_UNAVAILABLE") < output.getvalue().index(
        "TARGET_MOVED"
    )


def test_next_task_can_report_its_own_provider_failure() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output, plain=True)
    renderer.render(_event("execution.failed", failure_code="PROVIDER_UNAVAILABLE"))
    renderer.render(
        {
            "task_id": "next-task",
            "event_type": "execution.background_failed",
            "payload": {"failure_code": "PROVIDER_UNAVAILABLE", "provider_status": 429},
        }
    )
    renderer.finish()
    assert output.getvalue().count("✗") == 2
    assert "Wait before retrying" in output.getvalue()


def test_new_claim_can_fail_before_provider_start_on_a_retried_task() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output, plain=True)
    for claim_id in ("first-attempt", "second-attempt"):
        renderer.render(
            {
                **_event("claim.granted"),
                "claim_id": claim_id,
            }
        )
        renderer.render(
            {
                **_event(
                    "execution.background_failed",
                    failure_code="PROVIDER_UNAVAILABLE",
                    provider_error="authentication",
                ),
                "claim_id": claim_id,
            }
        )
    renderer.finish()
    assert output.getvalue().count("✗") == 2


def test_unknown_replayed_provider_category_uses_safe_status_fallback() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output, plain=True)
    renderer.render(
        _event(
            "execution.background_failed",
            failure_code="PROVIDER_UNAVAILABLE",
            provider_status=400,
            provider_error="PRIVATE_RESPONSE_TEXT",
            provider_message="PRIVATE_API_KEY",
        )
    )
    renderer.finish()
    assert "The provider rejected this request" in output.getvalue()
    assert "PRIVATE_RESPONSE_TEXT" not in output.getvalue()
    assert "PRIVATE_API_KEY" not in output.getvalue()
