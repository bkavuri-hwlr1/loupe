"""E2 evidence exercises real adapters with scripted transports and no billing."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_anthropic_provider import APIConnectionError as AnthropicConnectionError
from test_anthropic_provider import APIStatusError as AnthropicStatusError
from test_anthropic_provider import BadRequestError, _message
from test_anthropic_provider import Client as AnthropicClient
from test_openai_provider import APIConnectionError as OpenAIConnectionError
from test_openai_provider import APIStatusError as OpenAIStatusError
from test_openai_provider import Client as OpenAIClient
from test_openai_provider import _response

from llm_cli.paths import AppPaths
from llm_cli.providers import anthropic_provider, openai_provider
from llm_cli.providers.anthropic_provider import AnthropicProvider
from llm_cli.providers.openai_provider import OpenAIProvider

_script = Path(__file__).resolve().parents[2] / "scripts/provider_live_checks.py"
_spec = importlib.util.spec_from_file_location("provider_live_checks", _script)
assert _spec and _spec.loader
checks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checks)


@pytest.fixture(autouse=True)
def sdk_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        anthropic_provider,
        "_load_sdk",
        lambda: SimpleNamespace(
            APIConnectionError=AnthropicConnectionError,
            APIStatusError=AnthropicStatusError,
            BadRequestError=BadRequestError,
        ),
    )
    monkeypatch.setattr(
        openai_provider,
        "_load_sdk",
        lambda: SimpleNamespace(
            APIConnectionError=OpenAIConnectionError,
            APIStatusError=OpenAIStatusError,
        ),
    )


def _anthropic(*outcomes):
    client = AnthropicClient(*outcomes)
    return AnthropicProvider(
        model="synthetic-model", fallback_model=None, client=client
    ), client


def _cached(read: int):
    message = _message("OK")
    message.usage = SimpleNamespace(
        input_tokens=12,
        output_tokens=1,
        cache_creation_input_tokens=8192 if not read else 0,
        cache_read_input_tokens=read,
    )
    return message


def test_cache_evidence_uses_identical_real_adapter_requests_and_counts_hits() -> None:
    provider, client = _anthropic(_cached(0), _cached(8192))
    prefix = checks.cache_prefix()

    result = checks.cache_check(provider, prefix)

    assert result["passed"]
    assert len(client.calls) == 2 and client.calls[0] == client.calls[1]
    assert client.calls[0][1]["cache_control"] == {"type": "ephemeral"}
    assert client.calls[0][1]["messages"] == [{"role": "user", "content": prefix}]
    assert result["requests"][1]["usage"]["cache_read_input_tokens"] == 8192
    assert prefix not in json.dumps(result)
    assert "OK" not in json.dumps(result)


def test_cache_miss_cannot_be_reported_as_a_hit() -> None:
    provider, _ = _anthropic(_cached(0), _cached(0))
    assert not checks.cache_check(provider, "synthetic prefix")["passed"]


@pytest.mark.parametrize("name", ["anthropic", "openai"])
def test_actual_adapter_http_overflow_classification_and_history_rollback(name) -> None:
    body = {"error": {"message": "prompt is too long: 400000 tokens > 200000 maximum"}}
    if name == "anthropic":
        provider, client = _anthropic(BadRequestError(400, body))
    else:
        client = OpenAIClient(
            OpenAIStatusError(
                400,
                {"error": {"code": "context_length_exceeded", "message": "private"}},
            )
        )
        provider = OpenAIProvider(model="synthetic-model", client=client)
    prompt = checks.overflow_prompt(4096)

    result = checks.overflow_check(name, provider, prompt)

    assert result["passed"] and result["history_unchanged"]
    assert result["provider_error"] == "context_overflow"
    assert result["status_code"] == 400 and len(client.calls) == 1
    assert "private" not in json.dumps(result) and prompt not in json.dumps(result)


def test_unrelated_bad_request_never_counts_as_overflow() -> None:
    provider, _ = _anthropic(
        BadRequestError(
            400, {"error": {"message": "request contained private synthetic detail"}}
        )
    )
    result = checks.overflow_check("anthropic", provider, "synthetic")
    assert not result["passed"] and result["provider_error"] == "unclassified"
    assert "private synthetic detail" not in json.dumps(result)


def test_accepted_large_input_is_a_failure_with_usage_and_no_response_body() -> None:
    client = OpenAIClient(_response())
    provider = OpenAIProvider(model="synthetic-model", client=client)
    result = checks.overflow_check("openai", provider, "synthetic")
    assert not result["passed"] and result["state"] == "unexpected_acceptance"
    assert result["usage"]["prompt_tokens"] == 20
    assert "done" not in json.dumps(result)


def test_status_and_missing_credentials_cannot_construct_live_clients(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setattr(
        checks,
        "availability",
        lambda paths: {
            "anthropic": False,
            "openai": False,
        },
    )
    monkeypatch.setattr(checks, "live_providers", lambda *args: pytest.fail("billed"))
    monkeypatch.setattr(sys, "argv", ["check", "--profile", "scripted-e2"])
    assert checks.main() == 0
    assert json.loads(capsys.readouterr().out)["live_requests"] == 0
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check",
            "--profile",
            "scripted-e2",
            "--live",
            "--anthropic-model",
            "a",
            "--openai-model",
            "o",
            "--out",
            str(tmp_path / "result.json"),
        ],
    )
    with pytest.raises(SystemExit):
        checks.main()
    assert not (tmp_path / "result.json").exists()


def test_default_profile_is_refused_before_status_or_client_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checks, "account_status", lambda *args: pytest.fail("status"))
    with pytest.raises(ValueError, match="non-default"):
        checks.availability(AppPaths.resolve("default"))


def test_synthetic_payloads_are_fixed_bounded_and_not_copied_from_source() -> None:
    assert checks.cache_prefix() == checks.cache_prefix()
    assert len(checks.cache_prefix()) > 40_000
    assert len(checks.overflow_prompt(4096)) == 4096
    assert checks.overflow_prompt(4096) == checks.overflow_prompt(4096)
    assert set(checks.overflow_prompt(4096)) <= set("0123456789abcdef")
    for invalid in (True, 0, 16 * 1024 * 1024 + 1):
        with pytest.raises(ValueError):
            checks.overflow_prompt(invalid)


def test_results_are_private_complete_and_never_overwrite_prior_evidence(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "result.json"
    checks.write_result(destination, {"passed": False, "checks": []})
    assert json.loads(destination.read_text()) == {"passed": False, "checks": []}
    assert destination.stat().st_mode & 0o077 == 0
    before = destination.read_bytes()
    with pytest.raises(FileExistsError):
        checks.write_result(destination, {"passed": True})
    assert destination.read_bytes() == before
    assert not list(tmp_path.glob("*.pending"))


def test_full_scripted_run_has_four_actual_adapter_requests() -> None:
    anthropic, client = _anthropic(
        _cached(0),
        _cached(8192),
        BadRequestError(
            400, {"error": {"message": "prompt is too long: 400000 > 200000 maximum"}}
        ),
    )
    openai_client = OpenAIClient(
        OpenAIStatusError(400, {"error": {"code": "context_length_exceeded"}})
    )
    openai = OpenAIProvider(model="synthetic-model", client=openai_client)
    result = checks.checks(
        {"anthropic": anthropic, "openai": openai},
        "stable synthetic prefix",
        checks.overflow_prompt(4096),
    )
    assert result["passed"] and result["logical_requests"] == 4
    assert len(client.calls) == 3 and len(openai_client.calls) == 1


def test_unexpected_setup_exception_cannot_echo_credentials_or_prompt(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    def failure():
        raise RuntimeError("synthetic-private-marker must not appear")

    monkeypatch.setattr(checks, "main", failure)
    assert checks.cli() == 1
    output = capsys.readouterr()
    assert "synthetic-private-marker" not in output.out + output.err
    assert json.loads(output.out) == {"passed": False, "state": "check_setup_failed"}
