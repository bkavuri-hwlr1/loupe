from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime
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
    return AppPaths.resolve("discovery-order", environ={}, home=tmp_path)


def api_sdk(
    monkeypatch: pytest.MonkeyPatch, responses: list[list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    def listing(**arguments: Any) -> Any:
        rows = responses[len(requests)]
        requests.append(arguments)
        return SimpleNamespace(data=rows, has_next_page=lambda: False)

    @contextmanager
    def client(**arguments: Any) -> Any:
        yield SimpleNamespace(models=SimpleNamespace(list=listing))

    monkeypatch.setattr(
        catalog,
        "_load_sdk",
        lambda _: SimpleNamespace(
            OpenAI=client,
            Anthropic=client,
            DefaultHttpxClient=lambda **_: object(),
        ),
    )
    return requests


def codex_sdk(
    paths: AppPaths,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]],
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    CredentialStore(paths).save(
        Credentials("test-token", None, time.time() + 3600, "test-account")
    )
    requests: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    @contextmanager
    def stream(*args: Any, **kwargs: Any) -> Any:
        requests.append((args, kwargs))
        yield SimpleNamespace(
            raise_for_status=lambda: None,
            iter_bytes=lambda: iter([json.dumps({"models": rows}).encode()]),
        )

    @contextmanager
    def transport(**arguments: Any) -> Any:
        yield SimpleNamespace(stream=stream)

    monkeypatch.setattr(
        catalog, "_load_sdk", lambda _: SimpleNamespace(DefaultHttpxClient=transport)
    )
    return requests


def write_cache(
    paths: AppPaths,
    provider: str,
    rows: list[dict[str, Any]],
    *,
    age: int = 0,
) -> None:
    path = catalog._cache_path(paths, provider)
    with catalog._locked(path):
        catalog._write(
            path,
            {
                "fingerprint": catalog._fingerprint(paths, provider),
                "fetched_at": time.time() - age,
                "models": rows,
            },
        )


def ids(result: catalog.ModelCatalog) -> list[str]:
    return [option.id for option in result.models]


def test_openai_discovery_orders_by_creation_and_roundtrips_cache(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_api_key(paths, "openai", "test-key")
    requests = api_sdk(
        monkeypatch,
        [
            [
                {"id": "gpt-5.6-terra", "created": 1700000000},
                {"id": "gpt-99-future", "created": 1800000000},
                {"id": "gpt-new-family", "created": 1900000000},
                {"id": "gpt-image-future", "created": 2000000000},
            ]
        ],
    )
    result = catalog.list_models(paths, "openai")
    assert result.source == "account"
    assert ids(result) == ["gpt-new-family", "gpt-99-future", "gpt-5.6-terra"]
    assert [option.created_at for option in result.models] == [
        1900000000,
        1800000000,
        1700000000,
    ]
    assert result.models[0].efforts == ()
    cached = catalog.list_models(paths, "openai")
    assert cached.source == "cache"
    assert cached.models == result.models
    assert len(requests) == 1


def test_openai_discovers_future_reasoning_families_without_allowlist(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_api_key(paths, "openai", "test-key")
    api_sdk(
        monkeypatch,
        [
            [
                {"id": "o4-mini"},
                {"id": "o9"},
                {"id": "o9-mini"},
                {"id": "o1-mini"},
                {"id": "o9-deep-research"},
                {"id": "other-text-model"},
            ]
        ],
    )
    result = catalog.list_models(paths, "openai")
    assert result.source == "account"
    assert ids(result) == ["o9", "o9-mini", "o4-mini"]
    assert result.models[0].efforts == result.models[1].efforts == ()


@pytest.mark.parametrize("age", [0, 600])
@pytest.mark.parametrize(
    "provider,models,expected",
    [
        (
            "openai",
            ["gpt-5.9", "gpt-5.10", "gpt-6-sol", "gpt-6-astra"],
            ["gpt-6-sol", "gpt-6-astra", "gpt-5.10", "gpt-5.9"],
        ),
        (
            "anthropic",
            ["claude-opus-4-9", "claude-opus-4-10", "claude-opus-5"],
            ["claude-opus-5", "claude-opus-4-10", "claude-opus-4-9"],
        ),
    ],
)
def test_legacy_cache_uses_numeric_versions_and_stable_ties(
    paths: AppPaths, provider: str, models: list[str], expected: list[str], age: int
) -> None:
    save_api_key(paths, provider, "test-key")
    write_cache(paths, provider, [{"id": model} for model in models], age=age)
    result = catalog.list_models(paths, provider)
    assert result.source == ("cache" if age == 0 else "stale-cache")
    assert ids(result) == expected
    assert all(option.created_at is None for option in result.models)
    assert all(option.priority is None for option in result.models)


def test_reference_choices_also_use_version_order(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        catalog._REFERENCE,
        "openai",
        tuple(
            catalog.ModelOption(model, model)
            for model in ["gpt-5.9", "gpt-5.10", "gpt-6-sol", "gpt-6-luna"]
        ),
    )
    result = catalog.list_models(paths, "openai")
    assert result.source == "reference"
    assert ids(result) == ["gpt-6-sol", "gpt-6-luna", "gpt-5.10", "gpt-5.9"]


def test_codex_priority_beats_version_and_hidden_models_stay_excluded(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_sdk(
        paths,
        monkeypatch,
        [
            {"slug": "gpt-99-future", "priority": 20},
            {"slug": "gpt-5.6-terra", "priority": 10},
            {"slug": "gpt-future-special", "priority": 0},
            {"slug": "gpt-hidden", "priority": 0, "visibility": "hide"},
            {"slug": "gpt-99-unranked"},
        ],
    )
    result = catalog.list_models(paths, "codex")
    assert result.source == "account"
    assert ids(result) == [
        "gpt-future-special",
        "gpt-5.6-terra",
        "gpt-99-future",
        "gpt-99-unranked",
    ]
    assert [option.priority for option in result.models] == [0, 10, 20, None]
    cached = catalog.list_models(paths, "codex")
    assert cached.source == "cache"
    assert cached.models == result.models


@pytest.mark.parametrize("override", [None, "0.156.0"])
def test_codex_client_version_uses_fixed_host_and_keeps_account_metadata(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch, override: str | None
) -> None:
    if override is not None:
        monkeypatch.setenv("LLM_COORD_CODEX_MODELS_CLIENT_VERSION", override)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://untrusted.example")
    requests = codex_sdk(
        paths,
        monkeypatch,
        [
            {"slug": "gpt-99-future", "priority": 10},
            {
                "slug": "gpt-new-family",
                "priority": 0,
                "default_reasoning_level": "max",
                "supported_reasoning_levels": [
                    {"effort": "low"},
                    {"effort": "max"},
                    {"effort": "ultra"},
                ],
            },
        ],
    )
    result = catalog.list_models(paths, "codex")
    assert result.source == "account"
    assert ids(result) == ["gpt-new-family", "gpt-99-future"]
    option = result.models[0]
    assert option.priority == 0
    assert option.efforts == ("low", "max")
    assert option.default_effort == "max"
    assert len(requests) == 1
    args, _kwargs = requests[0]
    assert args == (
        "GET",
        "https://chatgpt.com/backend-api/codex/models?client_version="
        + (override or "0.155.0"),
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "https://untrusted.example",
        "0.156.0&target=https://untrusted.example",
        "0.156.0#fragment",
        "0.156",
        "0.156.00000",
        "0.156.0\n",
        " 0.156.0",
    ],
)
def test_invalid_codex_version_refresh_preserves_cache_without_request(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    requests = codex_sdk(paths, monkeypatch, [{"slug": "gpt-new", "priority": 0}])
    initial = catalog.list_models(paths, "codex")
    assert initial.source == "account"
    monkeypatch.setenv("LLM_COORD_CODEX_MODELS_CLIENT_VERSION", invalid)
    result = catalog.list_models(paths, "codex", refresh=True)
    assert result.source == "stale-cache"
    assert result.models == initial.models
    assert "Could not refresh" in (result.notice or "")
    assert len(requests) == 1


def test_anthropic_creation_dates_normalize_timezones_and_survive_cache(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_api_key(paths, "anthropic", "test-key")
    api_sdk(
        monkeypatch,
        [
            [
                {"id": "claude-opus-99", "created_at": "2026-01-01T00:00:00Z"},
                {"id": "claude-new", "created_at": "2026-09-01T12:00:00+02:00"},
                {"id": "claude-latest", "created_at": "2026-09-01T10:30:00Z"},
            ]
        ],
    )
    result = catalog.list_models(paths, "anthropic")
    assert result.source == "account"
    assert ids(result) == ["claude-latest", "claude-new", "claude-opus-99"]
    assert (
        result.models[1].created_at
        == datetime.fromisoformat("2026-09-01T10:00:00+00:00").timestamp()
    )
    assert result.models[0].efforts == ()
    assert catalog.list_models(paths, "anthropic").models == result.models


@pytest.mark.parametrize("invalid", [True, "1900000000", -1, float("inf"), None])
def test_invalid_openai_creation_metadata_does_not_hide_model(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch, invalid: object
) -> None:
    save_api_key(paths, "openai", "test-key")
    api_sdk(monkeypatch, [[{"id": "gpt-new", "created": invalid}]])
    result = catalog.list_models(paths, "openai")
    assert result.source == "account"
    assert ids(result) == ["gpt-new"]
    assert result.models[0].created_at is None


@pytest.mark.parametrize("invalid", [True, "0", -1, 1.5, None])
def test_invalid_codex_priority_does_not_hide_model(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch, invalid: object
) -> None:
    codex_sdk(paths, monkeypatch, [{"slug": "gpt-new", "priority": invalid}])
    result = catalog.list_models(paths, "codex")
    assert result.source == "account"
    assert ids(result) == ["gpt-new"]
    assert result.models[0].priority is None


@pytest.mark.parametrize("invalid", [True, "not-a-date", "2026-09-01T12:00:00", None])
def test_invalid_anthropic_creation_metadata_does_not_hide_model(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch, invalid: object
) -> None:
    save_api_key(paths, "anthropic", "test-key")
    api_sdk(monkeypatch, [[{"id": "claude-new", "created_at": invalid}]])
    result = catalog.list_models(paths, "anthropic")
    assert result.source == "account"
    assert ids(result) == ["claude-new"]
    assert result.models[0].created_at is None


def test_invalid_cached_ordering_metadata_does_not_invalidate_model_list(
    paths: AppPaths,
) -> None:
    save_api_key(paths, "openai", "test-key")
    write_cache(
        paths,
        "openai",
        [
            {"id": "gpt-5.9", "created_at": "invalid", "priority": True},
            {"id": "gpt-5.10", "created_at": float("inf"), "priority": -1},
        ],
    )
    result = catalog.list_models(paths, "openai")
    assert result.source == "cache"
    assert ids(result) == ["gpt-5.10", "gpt-5.9"]
    assert all(option.created_at is None for option in result.models)
    assert all(option.priority is None for option in result.models)


def test_expiry_discovers_releases_and_refresh_replaces_removed_models(
    paths: AppPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1900000000.0]
    monkeypatch.setattr(catalog.time, "time", lambda: now[0])
    save_api_key(paths, "openai", "test-key")
    requests = api_sdk(
        monkeypatch,
        [
            [{"id": "gpt-5.6-terra"}],
            [{"id": "gpt-5.6-terra"}, {"id": "gpt-7-next"}],
            [{"id": "gpt-7-next"}],
        ],
    )
    assert ids(catalog.list_models(paths, "openai")) == ["gpt-5.6-terra"]
    now[0] += 299
    assert catalog.list_models(paths, "openai").source == "cache"
    assert len(requests) == 1
    now[0] += 1
    discovered = catalog.list_models(paths, "openai")
    assert discovered.source == "account"
    assert ids(discovered) == ["gpt-7-next", "gpt-5.6-terra"]
    assert len(requests) == 2
    refreshed = catalog.list_models(paths, "openai", refresh=True)
    assert refreshed.source == "account"
    assert ids(refreshed) == ["gpt-7-next"]
    assert len(requests) == 3
    cached = catalog.list_models(paths, "openai")
    assert cached.source == "cache"
    assert cached.models == refreshed.models
