"""Versioned RPC request and response envelopes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from llm_cli import PROTOCOL_VERSION, __version__


class EnvelopeError(ValueError):
    pass


def _required_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise EnvelopeError(f"{key} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class Request:
    protocol_version: int
    request_id: str
    method: str
    params: dict[str, Any]
    idempotency_key: str | None
    client_version: str
    profile_id: str

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        method: str,
        params: Mapping[str, Any] | None,
        profile_id: str,
        idempotency_key: str | None = None,
    ) -> Request:
        return cls(
            protocol_version=PROTOCOL_VERSION,
            request_id=request_id,
            method=method,
            params=dict(params or {}),
            idempotency_key=idempotency_key,
            client_version=__version__,
            profile_id=profile_id,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Request:
        version = data.get("protocol_version")
        if not isinstance(version, int):
            raise EnvelopeError("protocol_version must be an integer")
        params = data.get("params")
        if not isinstance(params, dict):
            raise EnvelopeError("params must be an object")
        idempotency_key = data.get("idempotency_key")
        if idempotency_key is not None and not isinstance(idempotency_key, str):
            raise EnvelopeError("idempotency_key must be a string or null")
        return cls(
            protocol_version=version,
            request_id=_required_string(data, "request_id"),
            method=_required_string(data, "method"),
            params=params,
            idempotency_key=idempotency_key,
            client_version=_required_string(data, "client_version"),
            profile_id=_required_string(data, "profile_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Response:
    request_id: str
    ok: bool
    result: dict[str, Any] | list[Any] | str | int | bool | None = None
    error: dict[str, Any] | None = None
    daemon_revision: int | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Response:
        ok = data.get("ok")
        if not isinstance(ok, bool):
            raise EnvelopeError("ok must be a boolean")
        error = data.get("error")
        if error is not None and not isinstance(error, dict):
            raise EnvelopeError("error must be an object or null")
        revision = data.get("daemon_revision")
        if revision is not None and not isinstance(revision, int):
            raise EnvelopeError("daemon_revision must be an integer or null")
        return cls(
            request_id=_required_string(data, "request_id"),
            ok=ok,
            result=data.get("result"),
            error=error,
            daemon_revision=revision,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["EnvelopeError", "Request", "Response"]
