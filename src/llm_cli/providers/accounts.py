"""Private local sign-ins and the remembered provider for one Magnifio profile.

Only metadata crosses this module's UI boundary. Provider adapters read API keys
locally when each task starts; keys never need to be sent through daemon RPC.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths, reject_symlink_components
from llm_cli.providers.codex_auth import CredentialStore

SUPPORTED_PROVIDERS = ("codex", "anthropic", "openai")
_API_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
_MAX_PRIVATE_BYTES = 16 * 1024
_EFFORTS = frozenset(
    {"default", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)


def _failure(message: str) -> LlmCoordError:
    return LlmCoordError(ErrorCode.PROVIDER_UNAVAILABLE, message)


def _check_provider(provider: str, *, api_key: bool = False) -> None:
    if provider not in (_API_KEY_ENV if api_key else SUPPORTED_PROVIDERS):
        raise _failure("choose Codex, Anthropic, or OpenAI from /login")


def _credential_path(paths: AppPaths, provider: str) -> Path:
    _check_provider(provider, api_key=True)
    return paths.state_dir / "auth" / f"{provider}.json"


def _valid_key(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 8192
        and all(33 <= ord(char) <= 126 for char in value)
    )


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    directory = path.parent
    reject_symlink_components(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    reject_symlink_components(directory)
    directory.chmod(0o700)
    lock_path = path.with_suffix(".lock")
    reject_symlink_components(lock_path)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise _failure("local account lock must be a private regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _read(path: Path) -> dict[str, Any] | None:
    reject_symlink_components(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise _failure("cannot read local account settings; use /login again") from None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise _failure("local account file must be owner-only (mode 600)")
        raw = handle.read(_MAX_PRIVATE_BYTES + 1)
    if len(raw) > _MAX_PRIVATE_BYTES:
        raise _failure("local account file is too large; use /login again")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (ValueError, UnicodeError):
        raise _failure("local account file is malformed; use /login again") from None


def _write(path: Path, value: Mapping[str, object]) -> None:
    reject_symlink_components(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _saved_api_key(paths: AppPaths, provider: str) -> str | None:
    value = _read(_credential_path(paths, provider))
    if value is None:
        return None
    key = value.get("api_key")
    if set(value) != {"api_key"} or not _valid_key(key):
        raise _failure("local API key is malformed; use /login again")
    assert isinstance(key, str)
    return key


def load_api_key(paths: AppPaths | None, provider: str) -> str | None:
    """Resolve fresh credentials for an adapter, preferring an explicit local login."""
    _check_provider(provider, api_key=True)
    saved = _saved_api_key(paths, provider) if paths is not None else None
    if saved is not None:
        return saved
    value = os.environ.get(_API_KEY_ENV[provider])
    return value if value and value.strip() else None


def account_status(paths: AppPaths, provider: str) -> dict[str, object]:
    """Return availability only; do not contact a provider or expose any secrets."""
    _check_provider(provider)
    if provider == "codex":
        status = CredentialStore(paths).status()
        return {
            **status,
            "authenticated": bool(status["authenticated"] or status.get("refreshable")),
            "source": "saved"
            if status.get("refreshable") or status["authenticated"]
            else None,
        }
    saved = _saved_api_key(paths, provider)
    environment = bool(os.environ.get(_API_KEY_ENV[provider], "").strip())
    if provider == "anthropic":
        environment = environment or bool(
            os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        )
    return {
        "provider": provider,
        "billing": "api",
        "authenticated": saved is not None or environment,
        "source": "saved"
        if saved is not None
        else "environment"
        if environment
        else None,
    }


def list_accounts(paths: AppPaths) -> list[dict[str, object]]:
    return [account_status(paths, provider) for provider in SUPPORTED_PROVIDERS]


def save_api_key(paths: AppPaths, provider: str, key: str) -> dict[str, object]:
    path = _credential_path(paths, provider)
    key = key.strip()
    if not _valid_key(key):
        raise _failure("enter a non-empty API key without spaces or control characters")
    with _locked(path):
        _write(path, {"api_key": key})
    return account_status(paths, provider)


def logout_account(paths: AppPaths, provider: str) -> dict[str, object]:
    _check_provider(provider)
    if provider == "codex":
        return CredentialStore(paths).logout()
    path = _credential_path(paths, provider)
    with _locked(path):
        reject_symlink_components(path)
        path.unlink(missing_ok=True)
    # Environment credentials may still be configured; report that truthfully.
    return account_status(paths, provider)


def load_preference(paths: AppPaths) -> dict[str, object]:
    value = _read(paths.state_dir / "preferences.json")
    if value is None:
        return {}
    provider, model, effort = (
        value.get("provider"),
        value.get("model"),
        value.get("effort"),
    )
    if (
        set(value) not in ({"provider", "model"}, {"provider", "model", "effort"})
        or not isinstance(provider, str)
        or provider not in SUPPORTED_PROVIDERS
        or not _valid_model(model)
        or not _valid_effort(effort)
    ):
        raise _failure(
            "saved provider preference is malformed; select a provider again"
        )
    result: dict[str, object] = {"provider": provider, "model": model}
    if effort not in {None, "default"}:
        result["effort"] = effort
    return result


def _valid_model(model: object) -> bool:
    return model is None or (
        isinstance(model, str)
        and 0 < len(model) <= 256
        and model == model.strip()
        and all(33 <= ord(char) <= 126 for char in model)
    )


def _valid_effort(effort: object) -> bool:
    return effort is None or (isinstance(effort, str) and effort in _EFFORTS)


def save_preference(
    paths: AppPaths,
    provider: str,
    model: str | None = None,
    effort: str | None = None,
) -> dict[str, object]:
    _check_provider(provider)
    if not _valid_model(model):
        raise _failure("choose a model name without spaces or control characters")
    if not _valid_effort(effort):
        raise _failure("choose a supported effort level from /effort")
    value: dict[str, object] = {"provider": provider, "model": model}
    if effort not in {None, "default"}:
        value["effort"] = effort
    path = paths.state_dir / "preferences.json"
    with _locked(path):
        _write(path, value)
    return value


__all__ = [
    "SUPPORTED_PROVIDERS",
    "account_status",
    "list_accounts",
    "load_api_key",
    "load_preference",
    "logout_account",
    "save_api_key",
    "save_preference",
]
