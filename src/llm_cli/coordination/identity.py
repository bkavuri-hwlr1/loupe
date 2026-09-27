"""Canonical local/remote repository coordination identity."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

_SCP_REMOTE = re.compile(r"^(?:(?P<user>[^/@:]+)@)?(?P<host>[^/:]+):(?P<path>[^/].*)$")


def normalize_remote_identity(remote: str | None) -> str | None:
    if remote is None:
        return None
    candidate = remote.strip()
    if not candidate:
        return None

    scp = _SCP_REMOTE.match(candidate)
    if scp and "://" not in candidate:
        host = scp.group("host").lower()
        path = _normalize_remote_path(scp.group("path"))
        return f"{host}/{path}" if path else None

    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https", "ssh", "git"}:
        return None
    if not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if parsed.port is not None and not (
        (parsed.scheme.lower() == "https" and parsed.port == 443)
        or (parsed.scheme.lower() == "http" and parsed.port == 80)
        or (parsed.scheme.lower() == "ssh" and parsed.port == 22)
    ):
        host = f"{host}:{parsed.port}"
    path = _normalize_remote_path(unquote(parsed.path))
    return f"{host}/{path}" if path else None


def _normalize_remote_path(path: str) -> str:
    normalized = path.strip().strip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return "/".join(part for part in normalized.split("/") if part)


def repository_key(
    *,
    profile_id: str,
    integration_adapter: str,
    target_ref: str,
    common_git_dir: Path,
    normalized_remote: str | None,
    coordinate_by_remote: bool = True,
) -> str:
    if coordinate_by_remote and normalized_remote:
        parts = (
            profile_id,
            integration_adapter,
            normalized_remote,
            target_ref,
        )
    else:
        parts = (profile_id, "local", str(common_git_dir.resolve()), target_ref)
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return digest


__all__ = ["normalize_remote_identity", "repository_key"]
