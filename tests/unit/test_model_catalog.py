from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from llm_cli.paths import AppPaths
from llm_cli.providers import catalog
from llm_cli.providers.accounts import save_api_key
from llm_cli.providers.codex_auth import Credentials, CredentialStore


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "LLM_COORD_CODEX_MODELS_CLIENT_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)

    def unavailable(provider: str) -> None:
        raise AssertionError("test attempted a live SDK call")

    monkeypatch.setattr(catalog, "_load_sdk", unavailable)


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    return AppPaths.resolve("catalog", environ={}, home=tmp_path)


def api_sdk(
    monkeypatch: pytest.MonkeyPatch, pages: list[dict[str, Any]]
) -> dict[str, Any]:
    seen: dict[str, Any] = {"requests": []}

    def listing(**arguments: Any) -> Any:
        seen["requests"].append(arguments)
        data = pages[len(seen["requests"]) - 1]
        return SimpleNamespace(
            data=data["data"],
            last_id=data.get("last_id"),
            has_next_page=lambda: data.get("has_more", False),
        )

    @contextmanager
    def client(**arguments: Any) -> Any:
        seen["client"] = arguments
        yield SimpleNamespace(models=SimpleNamespace(list=listing))

    def transport(**arguments: Any) -> Any:
        seen["transport"] = arguments
        return object()

    monkeypatch.setattr(
        catalog,
        "_load_sdk",
        lambda _: SimpleNamespace(
            OpenAI=client,
            Anthropic=client,
            DefaultHttpxClient=transport,
        ),
    )
    return seen


@pytest.mark.parametrize("provider", ["codex", "openai", "anthropic"])
def test_disconnected_choices_are_explicitly_reference_only(
    paths: AppPaths, provider: str
) -> None:
    result = catalog.list_models(paths, provider)
    assert result.source == "reference"
    assert "/login" in (result.notice or "")
    assert result.models
    if provider in {"codex", "openai"}:
        for option in result.models:
            assert "ultra" not in option.efforts
            assert "ultra" not in catalog.model_option(provider, option.id).efforts
    assert not paths.state_dir.exists()


def test_openai_ids_and_capabilities_are_separate_from_subscription(
    paths: AppPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_api_key(paths, "openai", "secret-openai-test")
    seen = api_sdk(
        monkeypatch,
        [
            {
                "data": [
                    {"id": "gpt-5.6-terra"},
                    {"id": "gpt-5.3-codex"},
                    {"id": "gpt-4.1"},
                    {"id": "gpt-future"},
                    {"id": "ft:gpt-4.1:custom:example"},
                    {"id": "gpt-image-2"},
                    {"id": "gpt-4o-audio-preview"},
                    {"id": "gpt-realtime"},
                    {"id": "text-embedding-3-large"},
                    {"id": "gpt-4o-mini-search-preview"},
                    {"id": "bad\x1bname"},
                ]
            }
        ],
    )
    result = catalog.list_models(paths, "openai")
    assert result.source == "account"
    models = {m.id: m for m in result.models}
    assert set(models) == {
        "gpt-5.6-terra",
        "gpt-5.3-codex",
        "gpt-4.1",
        "gpt-future",
        "ft:gpt-4.1:custom:example",
    }
    assert models["gpt-5.6-terra"].efforts == (
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )
    subscription = catalog.model_option("codex", "gpt-5.6-terra")
    assert "none" not in subscription.efforts
    assert "ultra" not in subscription.efforts
    assert models["gpt-4.1"].efforts == models["gpt-future"].efforts == ()
    assert seen["client"]["api_key"] == "secret-openai-test"
    assert seen["client"]["base_url"] == "https://api.openai.com/v1"
    assert seen["client"]["timeout"] == 8
    assert seen["client"]["max_retries"] == 0
    assert seen["transport"] == {"follow_redirects": False, "trust_env": False}


def test_api_cache_is_private_offline_and_invalidated_by_login(
    paths: AppPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_api_key(paths, "openai", "first-account-key")
    seen = api_sdk(monkeypatch, [{"data": [{"id": "gpt-5.6-terra"}]}])
    assert catalog.list_models(paths, "openai").source == "account"
    assert catalog.list_models(paths, "openai").source == "cache"
    assert len(seen["requests"]) == 1
    cached = paths.state_dir / "models" / "openai.json"
    assert cached.stat().st_mode & 0o777 == 0o600
    assert cached.parent.stat().st_mode & 0o777 == 0o700
    assert "first-account-key" not in cached.read_text()
    assert (
        catalog.model_option("openai", "gpt-5.6-terra", paths=paths).default_effort
        == "medium"
    )
    save_api_key(paths, "openai", "second-account-key")
    assert catalog.list_models(paths, "openai").source == "reference"
    assert len(seen["requests"]) == 2


def test_anthropic_live_capabilities_override_reference_and_paginate(
    paths: AppPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_api_key(paths, "anthropic", "test-anthropic-key")
    seen = api_sdk(
        monkeypatch,
        [
            {
                "data": [
                    {
                        "id": "claude-new",
                        "display_name": "Claude New\n\x1b",
                        "capabilities": {
                            "effort": {
                                "supported": True,
                                "low": {"supported": True},
                                "max": {"supported": True},
                                "ultra": {"supported": True},
                            },
                            "thinking": {"types": {"adaptive": {"supported": True}}},
                        },
                    }
                ],
                "has_more": True,
                "last_id": "claude-new",
            },
            {
                "data": [
                    {
                        "id": "claude-opus-5",
                        "capabilities": {"effort": {"supported": False}},
                    }
                ]
            },
        ],
    )
    result = catalog.list_models(paths, "anthropic")
    assert result.source == "account"
    available = {option.id: option for option in result.models}
    first, second = available["claude-new"], available["claude-opus-5"]
    assert first.name == "Claude New"
    # Responses transport limits must not alter Anthropic capability discovery.
    assert first.efforts == ("low", "max", "ultra")
    assert first.default_effort is None
    assert first.adaptive_thinking is True
    assert second.efforts == ()
    assert second.adaptive_thinking is False
    assert seen["requests"][1]["after_id"] == "claude-new"
    assert 0 < seen["requests"][1]["timeout"] <= 8
    assert catalog.model_option("anthropic", "claude-new", paths=paths) == first


def test_anthropic_environment_token_uses_bearer_auth(
    paths: AppPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-anthropic-token")
    seen = api_sdk(monkeypatch, [{"data": [{"id": "claude-opus-5"}]}])
    assert catalog.list_models(paths, "anthropic").source == "account"
    assert seen["client"]["api_key"] is None
    assert seen["client"]["auth_token"] == "test-anthropic-token"


def test_refresh_failure_is_sanitized_and_preserves_previous_list(
    paths: AppPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_api_key(paths, "openai", "test-api-key")
    api_sdk(monkeypatch, [{"data": [{"id": "gpt-4.1"}]}])
    catalog.list_models(paths, "openai")

    def fail(*_: object) -> Any:
        raise RuntimeError("remote body contains test-api-key and private account")

    monkeypatch.setattr(catalog, "_load_sdk", fail)
    result = catalog.list_models(paths, "openai", refresh=True)
    assert result.source == "stale-cache"
    assert [m.id for m in result.models] == ["gpt-4.1"]
    assert "test-api-key" not in repr(result)
    assert "private account" not in repr(result)


def test_empty_account_is_not_replaced_with_reference_models(
    paths: AppPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_api_key(paths, "openai", "test-api-key")
    api_sdk(monkeypatch, [{"data": [{"id": "text-embedding-3-large"}]}])
    result = catalog.list_models(paths, "openai")
    assert result.source == "account"
    assert result.models == ()
    assert "No compatible" in (result.notice or "")


@pytest.mark.parametrize(
    "unsafe", ["symlink", "world-readable", "malformed", "oversized"]
)
def test_unsafe_cache_is_ignored(
    paths: AppPaths,
    monkeypatch: pytest.MonkeyPatch,
    unsafe: str,
) -> None:
    save_api_key(paths, "openai", "test-api-key")
    api_sdk(monkeypatch, [{"data": [{"id": "gpt-private-special"}]}])
    catalog.list_models(paths, "openai")
    path = paths.state_dir / "models" / "openai.json"
    if unsafe == "symlink":
        target = path.with_suffix(".other")
        path.rename(target)
        path.symlink_to(target)
    elif unsafe == "world-readable":
        path.chmod(0o644)
    elif unsafe == "malformed":
        path.write_text("{invalid")
    else:
        path.write_bytes(b"x" * (catalog._MAX_CACHE_BYTES + 1))
    assert catalog.list_models(paths, "openai").source == "reference"


def test_codex_fixed_host_transport_filters_unsupported_subscription_capabilities(
    paths: AppPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    CredentialStore(paths).save(
        Credentials(
            "test-access-token",
            None,
            time.time() + 3600,
            "test-account",
            "eu",
        )
    )
    seen: dict[str, Any] = {}
    payload = {
        "models": [
            {
                "slug": "gpt-new",
                "display_name": "New Model",
                "visibility": "list",
                "supported_in_api": False,
                "default_reasoning_level": "ultra",
                # This mismatch was observed live on 2026-09-13: models listed
                # ultra, but /codex/responses returned HTTP 400 with enum values
                # none, minimal, low, medium, high, xhigh, and max.
                "supported_reasoning_levels": [
                    {"effort": "low"},
                    {"effort": "max"},
                    {"effort": "ultra"},
                ],
            },
            {"slug": "hidden-model", "visibility": "hide"},
        ]
    }

    @contextmanager
    def stream(*args: Any, **kwargs: Any) -> Any:
        seen["request"] = (args, kwargs)
        yield SimpleNamespace(
            raise_for_status=lambda: None,
            iter_bytes=lambda: iter([json.dumps(payload).encode()]),
        )

    @contextmanager
    def transport(**arguments: Any) -> Any:
        seen["transport"] = arguments
        yield SimpleNamespace(stream=stream)

    monkeypatch.setenv("OPENAI_BASE_URL", "https://untrusted.example")
    monkeypatch.setattr(
        catalog, "_load_sdk", lambda _: SimpleNamespace(DefaultHttpxClient=transport)
    )
    result = catalog.list_models(paths, "codex")
    assert result.source == "account"
    assert len(result.models) == 1
    option = result.models[0]
    assert option.efforts == ("low", "max")
    assert option.default_effort is None
    assert "unsupported" in (result.notice or "")
    assert seen["transport"] == {"follow_redirects": False, "trust_env": False}
    args, kwargs = seen["request"]
    assert args[0] == "GET"
    assert (
        args[1] == "https://chatgpt.com/backend-api/codex/models?client_version=0.155.0"
    )
    assert kwargs["headers"]["ChatGPT-Account-Id"] == "test-account"
    assert kwargs["headers"]["x-openai-internal-codex-residency"] == "eu"
    cache = paths.state_dir / "models" / "codex.json"
    assert "test-access-token" not in cache.read_text()
    assert "test-account" not in cache.read_text()
    assert catalog.model_option("codex", "gpt-new", paths=paths) == option
    cached = catalog.list_models(paths, "codex")
    assert cached.source == "cache"
    assert cached.models == result.models
    assert "unsupported" in (cached.notice or "")


@pytest.mark.parametrize("provider", ["codex", "openai"])
@pytest.mark.parametrize("age", [0, 600])
def test_old_account_cache_cannot_restore_unsupported_efforts(
    paths: AppPaths, provider: str, age: int
) -> None:
    if provider == "codex":
        CredentialStore(paths).save(
            Credentials("test-access", None, time.time() + 3600, "test-account")
        )
    else:
        save_api_key(paths, provider, "test-api-key")
    path = catalog._cache_path(paths, provider)
    with catalog._locked(path):
        catalog._write(
            path,
            {
                "fingerprint": catalog._fingerprint(paths, provider),
                "fetched_at": time.time() - age,
                "models": [
                    {
                        "id": "gpt-6-astra",
                        "efforts": ["low", "medium", "max", "ultra"],
                        "default_effort": "ultra",
                    }
                ],
            },
        )
    result = catalog.list_models(paths, provider)
    assert result.source == ("cache" if age == 0 else "stale-cache")
    assert result.models[0].efforts == ("low", "medium", "max")
    assert result.models[0].default_effort is None
    assert "unsupported" in (result.notice or "")
    assert (
        catalog.model_option(provider, "gpt-6-astra", paths=paths) == result.models[0]
    )

    # Constructors use offline model_option, so an explicitly requested stale
    # effort must fail before any request instead of silently becoming max.
    if provider == "codex":
        from llm_cli.providers.codex_provider import CodexProvider

        factory = CodexProvider
    else:
        from llm_cli.providers.openai_provider import OpenAIProvider

        factory = OpenAIProvider
    with pytest.raises(ValueError, match="does not support effort 'ultra'"):
        factory(paths=paths, model="gpt-6-astra", effort="ultra")


def test_codex_reference_fallback_omits_unsupported_efforts(paths: AppPaths) -> None:
    CredentialStore(paths).save(
        Credentials("test-access", None, time.time() + 3600, "test-account")
    )
    result = catalog.list_models(paths, "codex")
    assert result.source == "reference"
    assert "unsupported" in (result.notice or "")
    assert all("ultra" not in option.efforts for option in result.models)


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-6-luna"])
def test_codex_gpt6_fallback_preserves_supported_saved_effort(
    paths: AppPaths, model: str
) -> None:
    from llm_cli.providers.codex_provider import CodexProvider

    # No account cache is available, as after credential rotation or offline use.
    option = catalog.model_option("codex", model, paths=paths)
    assert option.efforts == ("low", "medium", "high", "xhigh", "max")
    listing = catalog.list_models(paths, "codex")
    assert listing.source == "reference"
    assert option in listing.models
    provider = CodexProvider(paths=paths, model=model, effort="xhigh")
    assert provider.model == model
    with pytest.raises(ValueError, match="does not support effort 'ultra'"):
        CodexProvider(paths=paths, model=model, effort="ultra")


def test_known_snapshot_metadata_does_not_guess_unknown_suffixes() -> None:
    assert catalog.model_option("openai", "gpt-5.2-2025-12-11").efforts
    assert catalog.model_option("anthropic", "claude-opus-4-5-20251101").efforts
    assert catalog.model_option("openai", "gpt-5.2-new-family").efforts == ()
    assert catalog.model_option("anthropic", "claude-unknown").efforts == ()


def test_unknown_provider_is_offline(paths: AppPaths) -> None:
    assert catalog.list_models(paths, "custom").source == "unsupported"
    assert catalog.model_option("custom", "something").efforts == ()


@pytest.mark.parametrize(
    "model,maximum",
    [
        ("gpt-4o", 16_384),
        ("gpt-4.1", 32_768),
        ("gpt-4.1-mini", 32_768),
    ],
)
def test_verified_openai_output_limits(model: str, maximum: int) -> None:
    assert catalog.model_option("openai", model).max_output_tokens == maximum


@pytest.mark.parametrize(
    "model,efforts",
    [
        ("gpt-5-mini", ("minimal", "low", "medium", "high")),
        ("gpt-5-nano", ("minimal", "low", "medium", "high")),
        ("gpt-5.1", ("none", "low", "medium", "high")),
        ("gpt-5.2", ("none", "low", "medium", "high", "xhigh")),
        ("gpt-5-pro", ("high",)),
        ("o3", ("low", "medium", "high")),
        ("o4-mini", ("low", "medium", "high")),
    ],
)
def test_common_reasoning_models_have_verified_effort_metadata(
    model: str,
    efforts: tuple[str, ...],
) -> None:
    assert catalog.model_option("openai", model).efforts == efforts


def test_live_anthropic_output_limit_survives_offline_cache(
    paths: AppPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_api_key(paths, "anthropic", "test-key")
    api_sdk(
        monkeypatch,
        [
            {
                "data": [
                    {
                        "id": "claude-small",
                        "max_tokens": 4096,
                        "capabilities": {"effort": {"supported": False}},
                    },
                    {"id": "claude-invalid", "max_tokens": True},
                ]
            }
        ],
    )
    result = catalog.list_models(paths, "anthropic")
    assert result.models[0].max_output_tokens == 4096
    assert result.models[1].max_output_tokens is None
    assert (
        catalog.model_option("anthropic", "claude-small", paths=paths).max_output_tokens
        == 4096
    )


def test_anthropic_pagination_is_bounded(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_api_key(paths, "anthropic", "test-key")
    seen = api_sdk(
        monkeypatch,
        [
            {
                "data": [{"id": f"claude-page-{i}"}],
                "has_more": True,
                "last_id": f"claude-page-{i}",
            }
            for i in range(3)
        ],
    )
    assert catalog.list_models(paths, "anthropic").source == "reference"
    assert len(seen["requests"]) == 3


@pytest.mark.parametrize(
    "model", ["gpt-3.5-turbo", "gpt-4", "gpt-4-turbo", "gpt-4-32k", "o1-preview"]
)
def test_legacy_chat_models_are_not_offered_as_compatible(model: str) -> None:
    assert catalog._usable_openai(model) is False
