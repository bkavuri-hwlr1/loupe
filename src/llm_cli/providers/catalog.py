"""Account model discovery, with offline capability metadata for task validation.

Model listing never generates tokens. API model IDs come from the account, while
OpenAI's list endpoint does not expose efforts: those annotations use the official
model pages at https://developers.openai.com/api/docs/models/<model> (2026-09-13).
Anthropic exposes capabilities at https://platform.claude.com/docs/en/api/models/list.
Its fallback effort matrix is documented at
https://platform.claude.com/docs/en/build-with-claude/effort.
Codex's models transport is the public openai/codex ModelsClient protocol; its
subscription capabilities are deliberately separate from OpenAI API capabilities.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from llm_cli import __version__
from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths, reject_symlink_components
from llm_cli.providers.accounts import _locked, _write, account_status, load_api_key
from llm_cli.providers.codex_auth import CredentialStore

_TIMEOUT = 8.0
_CACHE_SECONDS = 300
_MAX_MODELS = 1000
_MAX_CACHE_BYTES = 1024 * 1024
# Codex gates model visibility by compatible client version. 0.154.0 omits
# GPT-6 Sol/Luna even for accounts with access; verified with 0.155.0.
_CODEX_MODELS_CLIENT_VERSION = "0.155.0"
_CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
# The account's Codex metadata can describe capabilities beyond this transport.
# On 2026-09-13 /codex/responses rejected its advertised `ultra` with HTTP 400:
# supported values were none, minimal, low, medium, high, xhigh, and max.
_RESPONSES_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
_EFFORT_NOTICE = "Effort levels unsupported by this connection are hidden."


@dataclass(frozen=True)
class ModelOption:
    id: str
    name: str
    description: str = ""
    efforts: tuple[str, ...] = ()
    default_effort: str | None = None
    adaptive_thinking: bool = False
    max_output_tokens: int | None = None
    created_at: float | None = None
    priority: int | None = None
    # Maximum prompt tokens, when the provider publishes it.
    context_window: int | None = None


@dataclass(frozen=True)
class ModelCatalog:
    provider: str
    models: tuple[ModelOption, ...]
    source: str
    notice: str | None = None


_NORMAL = ("low", "medium", "high")
_EXTENDED = (*_NORMAL, "xhigh")
_MAXIMUM = (*_EXTENDED, "max")
# Reference choices are not claims about what any particular account can use.
_REFERENCE: dict[str, tuple[ModelOption, ...]] = {
    "openai": (
        ModelOption("gpt-6-astra", "GPT-6 Astra", efforts=_MAXIMUM),
        ModelOption(
            "gpt-5.6-sol",
            "GPT-5.6 Sol",
            efforts=("none", *_MAXIMUM),
            default_effort="medium",
        ),
        ModelOption(
            "gpt-5.6-terra",
            "GPT-5.6 Terra",
            efforts=("none", *_MAXIMUM),
            default_effort="medium",
        ),
        ModelOption(
            "gpt-5.6-luna",
            "GPT-5.6 Luna",
            efforts=("none", *_MAXIMUM),
            default_effort="medium",
        ),
        ModelOption(
            "gpt-5.5", "GPT-5.5", efforts=("none", *_EXTENDED), default_effort="medium"
        ),
        ModelOption(
            "gpt-5.4", "GPT-5.4", efforts=("none", *_EXTENDED), default_effort="none"
        ),
        ModelOption("gpt-5.3-codex", "GPT-5.3 Codex", efforts=_EXTENDED),
        ModelOption(
            "gpt-5.2", "GPT-5.2", efforts=("none", *_EXTENDED), default_effort="none"
        ),
        ModelOption(
            "gpt-5.1", "GPT-5.1", efforts=("none", *_NORMAL), default_effort="none"
        ),
        ModelOption("gpt-5", "GPT-5", efforts=("minimal", *_NORMAL)),
        ModelOption(
            "gpt-4.1",
            "GPT-4.1",
            max_output_tokens=32_768,
            context_window=1_047_576,
        ),
        ModelOption(
            "gpt-4.1-mini",
            "GPT-4.1 mini",
            max_output_tokens=32_768,
            context_window=1_047_576,
        ),
        ModelOption(
            "gpt-4o", "GPT-4o", max_output_tokens=16_384, context_window=128_000
        ),
    ),
    # Official Codex app metadata; GPT-6 Sol/Luna levels verified 2026-09-27.
    # Live capabilities supersede these offline reference choices.
    "codex": (
        ModelOption(
            "gpt-6-astra",
            "GPT-6 Astra",
            efforts=(*_MAXIMUM, "ultra"),
            default_effort="medium",
        ),
        ModelOption("gpt-6-sol", "GPT-6 Sol", efforts=(*_MAXIMUM, "ultra")),
        ModelOption("gpt-6-luna", "GPT-6 Luna", efforts=_MAXIMUM),
        ModelOption(
            "gpt-5.6-sol",
            "GPT-5.6 Sol",
            efforts=(*_MAXIMUM, "ultra"),
            default_effort="low",
        ),
        ModelOption(
            "gpt-5.6-terra",
            "GPT-5.6 Terra",
            efforts=(*_MAXIMUM, "ultra"),
            default_effort="medium",
        ),
        ModelOption(
            "gpt-5.6-luna", "GPT-5.6 Luna", efforts=_MAXIMUM, default_effort="medium"
        ),
        ModelOption("gpt-5.5", "GPT-5.5", efforts=_EXTENDED, default_effort="medium"),
        ModelOption(
            "gpt-5.3-codex-spark",
            "GPT-5.3 Codex Spark",
            efforts=_EXTENDED,
            default_effort="high",
        ),
    ),
    "anthropic": (
        ModelOption(
            "claude-opus-5",
            "Claude Opus 5",
            efforts=_MAXIMUM,
            default_effort="high",
            adaptive_thinking=True,
        ),
        ModelOption(
            "claude-opus-4-8",
            "Claude Opus 4.8",
            efforts=_MAXIMUM,
            default_effort="high",
            adaptive_thinking=True,
        ),
        ModelOption(
            "claude-sonnet-5",
            "Claude Sonnet 5",
            efforts=_MAXIMUM,
            default_effort="high",
            adaptive_thinking=True,
        ),
        ModelOption(
            "claude-sonnet-4-6",
            "Claude Sonnet 4.6",
            efforts=(*_NORMAL, "max"),
            default_effort="high",
            adaptive_thinking=True,
        ),
        ModelOption(
            "claude-opus-4-7",
            "Claude Opus 4.7",
            efforts=_MAXIMUM,
            default_effort="high",
            adaptive_thinking=True,
        ),
        ModelOption(
            "claude-opus-4-6",
            "Claude Opus 4.6",
            efforts=(*_NORMAL, "max"),
            default_effort="high",
            adaptive_thinking=True,
        ),
        ModelOption(
            "claude-opus-4-5", "Claude Opus 4.5", efforts=_NORMAL, default_effort="high"
        ),
        ModelOption("claude-haiku-4-5", "Claude Haiku 4.5"),
    ),
}

# Capability-only entries keep the offline starter menu concise. These are
# offered only when the account returns them (or an explicit model ID is used).
_OPENAI_METADATA = (
    ModelOption("gpt-5-mini", "GPT-5 mini", efforts=("minimal", *_NORMAL)),
    ModelOption("gpt-5-nano", "GPT-5 nano", efforts=("minimal", *_NORMAL)),
    ModelOption("gpt-5-pro", "GPT-5 Pro", efforts=("high",), default_effort="high"),
    ModelOption(
        "gpt-5.4-mini",
        "GPT-5.4 mini",
        efforts=("none", *_EXTENDED),
        default_effort="none",
    ),
    ModelOption(
        "gpt-5.4-nano",
        "GPT-5.4 nano",
        efforts=("none", *_EXTENDED),
        default_effort="none",
    ),
    ModelOption("o3", "o3", efforts=_NORMAL, default_effort="medium"),
    ModelOption("o3-mini", "o3 mini", efforts=_NORMAL, default_effort="medium"),
    ModelOption("o4-mini", "o4 mini", efforts=_NORMAL, default_effort="medium"),
)


def default_model(provider: str) -> str:
    return {
        "codex": "gpt-5.6-terra",
        "openai": "gpt-5.3-codex",
        "anthropic": "claude-opus-5",
    }.get(provider, "default")


def _transport_models(
    provider: str, models: tuple[ModelOption, ...]
) -> tuple[tuple[ModelOption, ...], bool]:
    if provider not in {"codex", "openai"}:
        return models, False
    result = []
    for option in models:
        efforts = tuple(
            level for level in option.efforts if level in _RESPONSES_EFFORTS
        )
        result.append(
            replace(
                option,
                efforts=efforts,
                default_effort=option.default_effort
                if option.default_effort in efforts
                else None,
            )
        )
    compatible = tuple(result)
    return compatible, compatible != models


def _catalog(
    provider: str,
    models: tuple[ModelOption, ...],
    source: str,
    notice: str | None = None,
    *,
    efforts_filtered: bool = False,
) -> ModelCatalog:
    compatible, changed = _transport_models(provider, models)
    if changed or efforts_filtered:
        notice = f"{notice} {_EFFORT_NOTICE}" if notice else _EFFORT_NOTICE
    return ModelCatalog(provider, _ordered_models(provider, compatible), source, notice)


def _created_at(value: object) -> float | None:
    """Normalize API creation/release dates; missing metadata is not a failure."""
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value)
            # Do not let the machine's timezone change model ordering.
            if date.tzinfo is None:
                return None
            value = date.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 < value < 253_402_300_800  # Before year 10000; epoch means unknown.
    ):
        return float(value)
    return None


def _priority(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**31:
        return value
    return None


def _model_version(model: str) -> tuple[int, int]:
    # A snapshot date is not a generation number. This fallback needs no release
    # allowlist and compares numeric versions correctly (e.g. 5.10 after 5.9).
    base = re.sub(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$", "", model)
    match = re.match(r"(?:gpt-|chatgpt-|o)(\d+)(?:\.(\d+))?", base)
    if match is None:
        match = re.match(r"claude-(?:[a-z]+-)?(\d+)(?:[.-](\d+))?", base)
    if match is None:
        return (0, 0)
    return (int(match[1]), int(match[2] or 0))


def _ordered_models(
    provider: str, models: tuple[ModelOption, ...]
) -> tuple[ModelOption, ...]:
    """Honor Codex ranking, then newest API dates and numeric generations.

    Keep provider order for ties, including variants with the same generation.
    Sorting here also upgrades old caches that lack the new ordering metadata.
    """

    def key(option: ModelOption) -> tuple[int, float, tuple[int, int]]:
        priority = (
            option.priority
            if provider == "codex" and option.priority is not None
            else 2**31
        )
        major, minor = _model_version(option.id)
        return (priority, -(option.created_at or 0), (-major, -minor))

    return tuple(sorted(models, key=key))


def _known(provider: str, model: str) -> ModelOption:
    # Only strip documented snapshot date forms, never arbitrary model suffixes.
    base = re.sub(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$", "", model)
    if provider == "openai" and base == "gpt-5.6":
        base = "gpt-5.6-sol"
    choices = _REFERENCE.get(provider, ())
    if provider == "openai":
        choices = (*choices, *_OPENAI_METADATA)
    for option in choices:
        if base == option.id:
            return _transport_models(provider, (replace(option, id=model),))[0][0]
    return ModelOption(model, model)


def _fingerprint(paths: AppPaths, provider: str) -> str:
    if provider == "codex":
        # OAuth rotation replaces the credential file between prompts. Model
        # capabilities belong to the account, so token/file metadata must not
        # invalidate an effort level that model discovery just offered. Loading
        # directly also keeps capability validation offline (no token refresh).
        credentials = CredentialStore(paths)._load()
        value = f"codex\0{credentials.account_id}\0{credentials.residency or ''}"
    else:
        value = load_api_key(paths, provider) or os.environ.get(
            "ANTHROPIC_AUTH_TOKEN", ""
        )
        if provider == "openai":
            value += "\0" + os.environ.get("OPENAI_ORG_ID", "")
            value += "\0" + os.environ.get("OPENAI_PROJECT_ID", "")
    return hashlib.sha256(value.encode()).hexdigest()


def _cache_path(paths: AppPaths, provider: str) -> Path:
    return paths.state_dir / "models" / f"{provider}.json"


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    # Remote labels must not inject terminal escapes, newlines or bidi controls.
    return "".join(c for c in value[:limit] if c.isprintable()).strip()


def _token_count(value: object) -> int | None:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value <= 10_000_000
    ):
        return value
    return None


def _from_cache(value: object) -> ModelOption | None:
    if not isinstance(value, dict):
        return None
    model = value.get("id")
    if not isinstance(model, str) or not _ID.fullmatch(model):
        return None
    efforts = value.get("efforts", [])
    if not isinstance(efforts, list) or any(e not in _LEVELS for e in efforts):
        return None
    default = value.get("default_effort")
    if default is not None and default not in efforts:
        return None
    maximum = value.get("max_output_tokens")
    if maximum is not None and (
        not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or not 0 < maximum <= 10_000_000
    ):
        return None
    window = value.get("context_window")
    if window is not None and _token_count(window) is None:
        return None
    return ModelOption(
        model,
        _text(value.get("name"), 160) or model,
        _text(value.get("description"), 400),
        tuple(dict.fromkeys(efforts)),
        default,
        value.get("adaptive_thinking") is True,
        maximum,
        created_at=_created_at(value.get("created_at")),
        priority=_priority(value.get("priority")),
        context_window=_token_count(window),
    )


def _read_cache(
    paths: AppPaths, provider: str
) -> tuple[tuple[ModelOption, ...], float, bool] | None:
    try:
        path = _cache_path(paths, provider)
        reject_symlink_components(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                return None
            raw = handle.read(_MAX_CACHE_BYTES + 1)
        if len(raw) > _MAX_CACHE_BYTES:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("fingerprint") != _fingerprint(
            paths, provider
        ):
            return None
        fetched = value.get("fetched_at")
        rows = value.get("models")
        if (
            not isinstance(fetched, (int, float))
            or isinstance(fetched, bool)
            or not math.isfinite(fetched)
            or fetched > time.time() + 60
            or not isinstance(rows, list)
            or len(rows) > _MAX_MODELS
        ):
            return None
        models = tuple(_from_cache(row) for row in rows)
        if any(m is None for m in models):
            return None
        compatible, changed = _transport_models(
            provider, tuple(m for m in models if m is not None)
        )
        return (
            compatible,
            float(fetched),
            changed or value.get("efforts_filtered") is True,
        )
    except (OSError, ValueError, TypeError, LlmCoordError):
        return None


def model_option(
    provider: str, model: str, *, paths: AppPaths | None = None
) -> ModelOption:
    """Offline capabilities only. Unknown models get no assumed effort support."""
    if paths is not None and provider in _REFERENCE:
        cached = _read_cache(paths, provider)
        if cached is not None:
            for option in cached[0]:
                if option.id == model:
                    return option
    return _known(provider, model)


def _load_sdk(provider: str) -> Any:
    # Keep SDK imports behind the existing lazy adapter dependency boundary.
    if provider == "anthropic":
        from llm_cli.providers.anthropic_provider import _load_sdk as load
    else:
        from llm_cli.providers.openai_provider import _load_sdk as load
    return load()


def _dict(value: Any) -> dict[str, Any]:
    result = value if isinstance(value, dict) else value.model_dump(mode="json")
    if not isinstance(result, dict):
        raise ValueError("invalid model record")
    return result


def _usable_openai(model: str) -> bool:
    base = model.split(":")[1] if model.startswith("ft:") and ":" in model else model
    if base in {"gpt-4", "gpt-4-0314", "gpt-4-0613"} or base.startswith(
        (
            "gpt-3.5",
            "gpt-4-turbo",
            "gpt-4-32k",
            "gpt-4-1106",
            "gpt-4-0125",
            "o1-mini",
            "o1-preview",
        )
    ):
        return False
    text_model = base.startswith(("gpt-", "chatgpt-")) or bool(
        re.match(r"o\d+(?:[.-]|$)", base)
    )
    return text_model and not any(
        part in base
        for part in (
            "audio",
            "realtime",
            "transcribe",
            "image",
            "search",
            "deep-research",
            "tts",
            "moderation",
            "embedding",
            "live",
            "instruct",
        )
    )


def _api_models(paths: AppPaths, provider: str) -> tuple[ModelOption, ...]:
    sdk = _load_sdk(provider)
    options: dict[str, Any] = {
        "api_key": load_api_key(paths, provider),
        "max_retries": 0,
        "timeout": _TIMEOUT,
        "http_client": sdk.DefaultHttpxClient(follow_redirects=False, trust_env=False),
    }
    if provider == "openai":
        options.update(base_url="https://api.openai.com/v1")
        factory = sdk.OpenAI
    else:
        options.update(
            base_url="https://api.anthropic.com",
            auth_token=None
            if options["api_key"]
            else os.environ.get("ANTHROPIC_AUTH_TOKEN"),
        )
        factory = sdk.Anthropic
    result: dict[str, ModelOption] = {}
    deadline = time.monotonic() + _TIMEOUT
    with factory(**options) as client:
        page = client.models.list(
            **({"limit": 1000} if provider == "anthropic" else {})
        )
        for page_index in range(3):
            rows = page.data
            if not isinstance(rows, list) or len(rows) > _MAX_MODELS:
                raise ValueError("invalid model list")
            for row in rows:
                data = _dict(row)
                model = data.get("id")
                if not isinstance(model, str) or not _ID.fullmatch(model):
                    continue
                if provider == "openai":
                    if _usable_openai(model):
                        result[model] = replace(
                            _known(provider, model),
                            created_at=_created_at(data.get("created")),
                        )
                else:
                    option = _known(provider, model)
                    capabilities = data.get("capabilities")
                    if isinstance(capabilities, dict):
                        effort = capabilities.get("effort")
                        levels = tuple(
                            level
                            for level in _LEVELS
                            if isinstance(effort, dict)
                            and effort.get("supported") is True
                            and isinstance(effort.get(level), dict)
                            and effort[level].get("supported") is True
                        )
                        thinking = capabilities.get("thinking", {})
                        types = (
                            thinking.get("types", {})
                            if isinstance(thinking, dict)
                            else {}
                        )
                        adaptive = (
                            types.get("adaptive", {}) if isinstance(types, dict) else {}
                        )
                        option = replace(
                            option,
                            efforts=levels,
                            default_effort="high" if "high" in levels else None,
                            adaptive_thinking=isinstance(adaptive, dict)
                            and adaptive.get("supported") is True,
                        )
                    maximum = data.get("max_tokens")
                    result[model] = replace(
                        option,
                        name=_text(data.get("display_name"), 160) or option.name,
                        created_at=_created_at(data.get("created_at")),
                        context_window=_token_count(data.get("max_input_tokens")),
                        max_output_tokens=maximum
                        if (
                            isinstance(maximum, int)
                            and not isinstance(maximum, bool)
                            and 0 < maximum <= 10_000_000
                        )
                        else None,
                    )
                if len(result) >= _MAX_MODELS:
                    raise ValueError("too many models")
            if provider != "anthropic" or not page.has_next_page():
                return tuple(result.values())
            if page_index == 2:
                raise ValueError("model list exceeds page limit")
            if time.monotonic() >= deadline:
                raise TimeoutError("model listing timed out")
            # SDK pagination uses the provider cursor, never a remote next URL.
            after_id = getattr(page, "last_id", None)
            if not isinstance(after_id, str) or not _ID.fullmatch(after_id):
                raise ValueError("invalid model cursor")
            page = client.models.list(
                limit=1000,
                after_id=after_id,
                timeout=max(0.1, deadline - time.monotonic()),
            )
    raise ValueError("model list exceeds page limit")


def _codex_models(paths: AppPaths) -> tuple[ModelOption, ...]:
    version = os.environ.get(
        "LLM_COORD_CODEX_MODELS_CLIENT_VERSION", _CODEX_MODELS_CLIENT_VERSION
    )
    if not re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", version):
        raise ValueError("invalid Codex model catalog client version")
    sdk = _load_sdk("codex")
    credentials = CredentialStore(paths).credentials()
    headers = {
        "Authorization": f"Bearer {credentials.access_token}",
        "ChatGPT-Account-Id": credentials.account_id,
        "originator": "llm-coord",
        "User-Agent": f"llm-coord/{__version__}",
    }
    if credentials.residency:
        headers["x-openai-internal-codex-residency"] = credentials.residency
    with (
        sdk.DefaultHttpxClient(follow_redirects=False, trust_env=False) as client,
        client.stream(
            "GET",
            f"{_CODEX_MODELS_URL}?client_version={version}",
            headers=headers,
            timeout=_TIMEOUT,
        ) as response,
    ):
        response.raise_for_status()
        chunks = bytearray()
        for chunk in response.iter_bytes():
            chunks.extend(chunk)
            if len(chunks) > 4 * _MAX_CACHE_BYTES:
                raise ValueError("model response is too large")
        payload = json.loads(chunks)
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > _MAX_MODELS:
        raise ValueError("invalid model list")
    result: dict[str, ModelOption] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("visibility") not in (None, "list"):
            continue
        model = row.get("slug")
        if not isinstance(model, str) or not _ID.fullmatch(model):
            continue
        levels = row.get("supported_reasoning_levels", [])
        efforts = (
            tuple(
                dict.fromkeys(
                    str(level["effort"])
                    for level in levels
                    if isinstance(level, dict) and level.get("effort") in _LEVELS
                )
            )
            if isinstance(levels, list)
            else ()
        )
        default = row.get("default_reasoning_level")
        result[model] = ModelOption(
            model,
            _text(row.get("display_name"), 160) or model,
            _text(row.get("description"), 400),
            efforts,
            default if default in efforts else None,
            priority=_priority(row.get("priority")),
        )
    return tuple(result.values())


def list_models(
    paths: AppPaths, provider: str, *, refresh: bool = False
) -> ModelCatalog:
    """Read account availability, falling back visibly if it cannot be refreshed."""
    if provider not in _REFERENCE:
        return _catalog(
            provider,
            (),
            "unsupported",
            "This provider does not expose model discovery.",
        )
    try:
        connected = bool(account_status(paths, provider)["authenticated"])
    except (LlmCoordError, OSError):
        connected = False
    if not connected:
        return _catalog(
            provider,
            _REFERENCE[provider],
            "reference",
            "Sign in with /login to check account availability. "
            "These are reference models.",
        )
    cached = _read_cache(paths, provider)
    if not refresh and cached is not None and time.time() - cached[1] < _CACHE_SECONDS:
        return _catalog(
            provider,
            cached[0],
            "cache",
            "Recently fetched for this account; /model --refresh checks again.",
            efforts_filtered=cached[2],
        )
    try:
        models = (
            _codex_models(paths)
            if provider == "codex"
            else _api_models(paths, provider)
        )
        models, efforts_filtered = _transport_models(provider, models)
        notice = (
            None
            if models
            else "No compatible text models were returned for this account."
        )
        try:
            path = _cache_path(paths, provider)
            value = {
                "fingerprint": _fingerprint(paths, provider),
                "fetched_at": time.time(),
                "models": [asdict(option) for option in models],
                "efforts_filtered": efforts_filtered,
            }
            with _locked(path):
                _write(path, value)
        except (OSError, LlmCoordError):
            notice = "Models loaded; the local model cache could not be saved."
        return _catalog(
            provider, models, "account", notice, efforts_filtered=efforts_filtered
        )
    except Exception:
        # SDK exceptions and remote bodies may contain keys or account identity.
        # Never pass their text into the transcript or cache.
        if cached is not None:
            return _catalog(
                provider,
                cached[0],
                "stale-cache",
                "Could not refresh models. Showing the previous account list; "
                "availability may have changed.",
                efforts_filtered=cached[2],
            )
        return _catalog(
            provider,
            _REFERENCE[provider],
            "reference",
            "Could not fetch account models. Showing reference models; "
            "account availability is unconfirmed. Try /model --refresh.",
        )


__all__ = [
    "ModelCatalog",
    "ModelOption",
    "default_model",
    "list_models",
    "model_option",
]
