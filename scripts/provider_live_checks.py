"""Explicit E2 adapter checks using synthetic text and metadata-only evidence.

Without --live, only credential availability is inspected. Live requests never
read a repository, alter a profile, or print provider response/error bodies.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.providers.accounts import account_status
from llm_cli.providers.anthropic_provider import AnthropicProvider
from llm_cli.providers.base import is_context_overflow
from llm_cli.providers.openai_provider import OpenAIProvider

_SYSTEM = "This is a synthetic adapter check. Reply only OK. Do not use tools."
_USAGE = frozenset(
    {
        "input_tokens",
        "prompt_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "reasoning_tokens",
    }
)


def availability(paths: AppPaths) -> dict[str, bool]:
    if paths.profile_id == "default":
        raise ValueError("E2 requires a non-default profile")
    return {
        provider: bool(account_status(paths, provider)["authenticated"])
        for provider in ("anthropic", "openai")
    }


def cache_prefix() -> str:
    # Distinct numbered rows keep a sufficiently long stable prefix without
    # embedding any source, user data, credentials or provider-specific token.
    return (
        "\n".join(
            f"Synthetic record {index:05}: violet amber cedar river orbit meadow."
            for index in range(1_024)
        )
        + "\nReply only OK."
    )


def overflow_prompt(size: int) -> str:
    if type(size) is not int or not 1_024 <= size <= 16 * 1024 * 1024:
        raise ValueError("overflow bytes must be between 1,024 and 16 MiB")
    return hashlib.shake_256(b"Loupe E2 synthetic overflow version 1").hexdigest(
        (size + 1) // 2
    )[:size]


def admission(prefix: str, overflow: str, budget: int) -> dict[str, int]:
    if type(budget) is not int or budget < 1:
        raise ValueError("token admission budget must be positive")
    # ASCII byte counts conservatively cover the synthetic input, with the
    # largest production adapter output reservation and framing slack per call.
    planned = 2 * len(prefix.encode()) + 2 * len(overflow.encode())
    planned += 4 * (len(_SYSTEM.encode()) + 64_000 + 4_096)
    if planned > budget:
        raise ValueError("token admission budget is below the planned checks")
    return {"planned_token_estimate": planned, "admission_budget_tokens": budget}


def _usage(turn: Any) -> dict[str, int]:
    return {
        key: value
        for key, value in turn.usage.items()
        if key in _USAGE and type(value) is int and value >= 0
    }


def _failure(error: LlmCoordError) -> dict[str, object]:
    details = error.details or {}
    category = details.get("provider_error")
    known = {
        "context_overflow",
        "authentication",
        "rate_limit",
        "connection",
        "timeout",
        "incomplete_response",
    }
    result: dict[str, object] = {
        "provider_error": (
            category
            if isinstance(category, str) and category in known
            else "unclassified"
        ),
    }
    status = details.get("status_code")
    if type(status) is int and 100 <= status <= 599:
        result["status_code"] = status
    return result


def cache_check(provider: Any, prefix: str) -> dict[str, Any]:
    requests = []
    for _ in range(2):
        started = time.monotonic()
        attempted = False
        try:
            # Identical fresh histories exercise only the stable prompt prefix.
            session = provider.session(system=_SYSTEM, tools=[])
            attempted = True
            turn = session.send_user(prefix)
            row = {
                "state": "completed",
                "usage": _usage(turn),
                "answer_complete": turn.answer_complete,
            }
        except LlmCoordError as error:
            row = {"state": "rejected", "usage": None, **_failure(error)}
        except Exception:
            row = {"state": "unexpected_failure", "usage": None}
        requests.append(
            {"seconds": time.monotonic() - started, "attempted": attempted, **row}
        )
    passed = all(row.get("answer_complete", False) for row in requests) and (
        requests[1]["usage"].get("cache_read_input_tokens", 0) > 0
    )
    return {"check": "anthropic_cache", "passed": passed, "requests": requests}


def overflow_check(name: str, provider: Any, prompt: str) -> dict[str, Any]:
    started = time.monotonic()
    attempted = False
    session = before = None
    try:
        session = provider.session(system=_SYSTEM, tools=[])
        before = session.snapshot()
        attempted = True
        turn = session.send_user(prompt)
        result = {
            "passed": False,
            "state": "unexpected_acceptance",
            "usage": _usage(turn),
            "answer_complete": turn.answer_complete,
        }
    except LlmCoordError as error:
        failure = _failure(error)
        status = failure.get("status_code")
        try:
            unchanged = session is not None and session.snapshot() == before
        except Exception:
            unchanged = None
        result = {
            "passed": attempted
            and is_context_overflow(error)
            and type(status) is int
            and 400 <= status < 500
            and unchanged is True,
            "state": "rejected",
            "usage": None,
            "history_unchanged": unchanged,
            **failure,
        }
    except Exception:
        result = {"passed": False, "state": "unexpected_failure", "usage": None}
    return {
        "check": name + "_overflow",
        "seconds": time.monotonic() - started,
        "attempted": attempted,
        **result,
    }


def checks(providers: Mapping[str, Any], prefix: str, overflow: str) -> dict[str, Any]:
    rows = [cache_check(providers["anthropic"], prefix)]
    rows.extend(
        overflow_check(name, providers[name], overflow)
        for name in ("anthropic", "openai")
    )
    requests = [*rows[0]["requests"], *rows[1:]]
    attempted_rows = [row for row in requests if row["attempted"]]
    reported = [
        row["usage"] for row in attempted_rows if isinstance(row.get("usage"), dict)
    ]
    observed = {
        field: sum(values) if values else None
        for field in ("prompt_tokens", "output_tokens")
        for values in [[usage[field] for usage in reported if field in usage]]
    }
    observed["requests_with_reported_usage"] = len(reported)
    observed["requests_with_unknown_usage"] = len(attempted_rows) - len(reported)
    return {
        "observed_reported_tokens": observed,
        "passed": all(row["passed"] for row in rows),
        "logical_requests": sum(bool(row["attempted"]) for row in requests),
        "checks": rows,
    }


def write_result(destination: Path, result: Mapping[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=destination.parent,
        prefix="." + destination.name + ".",
        suffix=".pending",
        delete=False,
    ) as handle:
        pending = Path(handle.name)
        try:
            json.dump(result, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            pending.unlink(missing_ok=True)
            raise
    try:
        os.link(pending, destination)
    finally:
        pending.unlink(missing_ok=True)


def live_providers(paths: AppPaths, models: Mapping[str, str]) -> dict[str, Any]:
    providers = {
        "anthropic": AnthropicProvider(
            model=models["anthropic"], fallback_model=None, paths=paths
        ),
        "openai": OpenAIProvider(model=models["openai"], paths=paths),
    }
    # Keep authentication inside the production adapters. Disable SDK retries
    # and bound each request, so the evidence has exactly four logical requests
    # and no hidden retry loop. This script never handles API key values.
    for provider in providers.values():
        provider._client = provider._ensure_client().with_options(
            max_retries=0, timeout=120.0
        )
    return providers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--anthropic-model")
    parser.add_argument("--openai-model")
    parser.add_argument("--overflow-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--budget-tokens", type=int)
    parser.add_argument("--out", type=Path)
    options = parser.parse_args()
    if options.profile == "default":
        parser.error("E2 requires a non-default profile")
    paths = AppPaths.resolve(options.profile)
    authenticated = availability(paths)
    if not options.live:
        print(
            json.dumps(
                {
                    "profile": options.profile,
                    "authenticated": authenticated,
                    "live_requests": 0,
                }
            )
        )
        return 0
    if (
        not options.out
        or not options.anthropic_model
        or not options.openai_model
        or options.budget_tokens is None
        or options.budget_tokens < 1
    ):
        parser.error("--live requires --out, both models and positive --budget-tokens")
    if options.out.exists():
        parser.error("refusing to replace an earlier result")
    if not all(authenticated.values()):
        parser.error("sign both API providers in through /login before live checks")
    models = {
        name: getattr(options, name + "_model") for name in ("anthropic", "openai")
    }
    prefix, overflow = cache_prefix(), overflow_prompt(options.overflow_bytes)
    planned = admission(prefix, overflow, options.budget_tokens)
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    # SDK debug logging may include request metadata. This standalone command
    # emits only its explicit sanitized report, regardless of inherited logs.
    logging.disable(logging.CRITICAL)
    providers = live_providers(paths, models)
    result = checks(providers, prefix, overflow)
    cleanup_failures = []
    for name, provider in providers.items():
        try:
            provider._client.close()
        except Exception:
            cleanup_failures.append(name)
    if cleanup_failures:
        result["passed"] = False
        result["cleanup_failures"] = cleanup_failures
    document = {
        "version": 1,
        "script_sha256": script_hash,
        **planned,
        "mode": "live",
        "profile": options.profile,
        "models": models,
        "synthetic_only": True,
        "prefix_bytes": len(prefix.encode()),
        "prefix_sha256": hashlib.sha256(prefix.encode()).hexdigest(),
        "overflow_bytes": len(overflow.encode()),
        "overflow_sha256": hashlib.sha256(overflow.encode()).hexdigest(),
        "sdk_versions": {
            name: importlib.metadata.version(name) for name in ("anthropic", "openai")
        },
        "python": platform.python_version(),
        **result,
    }
    write_result(options.out, document)
    print(json.dumps(document, indent=2))
    return 0 if result["passed"] else 1


def cli() -> int:
    try:
        return main()
    except Exception:
        # Never print SDK text, chained exceptions, authentication material,
        # response bodies, prompts or profile filesystem paths on failure.
        print(json.dumps({"passed": False, "state": "check_setup_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
