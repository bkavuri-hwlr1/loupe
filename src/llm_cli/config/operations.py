"""Validated atomic configuration edits and redacted daemon settings views."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from llm_cli.agent.source_policy import contains_secret_material
from llm_cli.config.loader import load_settings
from llm_cli.config.models import Settings
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import reject_symlink_components

_SENSITIVE = re.compile(
    r"secret|password|token|api[_-]?key|authorization|credential", re.I
)
_REDACTED = "[redacted]"
_ALIASES = {
    "profile_id": "profile",
    "coordination_mode": "core.coordination_mode",
    "refuse_staged_paths": "core.refuse_staged_paths",
    "knowledge_index": "knowledge.index",
    "workspace_watchers": "workspace.watchers",
    "isolated_rebuild": "isolated.rebuild",
    "launch_lease_ms": "leases.launch_ms",
    "work_lease_ms": "leases.work_ms",
    "renewal_interval_ms": "leases.renewal_interval_ms",
    "reconciliation_interval_ms": "leases.reconciliation_interval_ms",
    "mcp_servers": "mcp.servers",
}


def _setting_key(attribute: str) -> str:
    key = "agent." + attribute[6:] if attribute.startswith("agent_") else attribute
    return _ALIASES.get(attribute, key)


def _read_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise _invalid("configuration could not be read") from error


def _sources(document: Mapping[str, Any], settings: Settings) -> dict[str, str]:
    sources = {}
    for field in fields(settings):
        key = _setting_key(field.name)
        value: object = document
        present = True
        for part in key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                present = False
                break
            value = value[part]
        sources[key] = (
            "profile" if key == "profile" else "config file" if present else "default"
        )
    return sources


def _validated_settings(
    path: Path, *, profile_id: str, loader: Callable[..., Settings]
) -> Settings:
    try:
        return loader(path, profile_id=profile_id)
    except (TypeError, AttributeError, ValueError) as error:
        raise _invalid("configuration has invalid value types") from error


def _key_parts(key: str) -> list[str]:
    if len(key) > 512:
        raise _invalid("configuration key is too long")
    try:
        value: object = tomllib.loads(key + " = true")
    except tomllib.TOMLDecodeError as error:
        raise _invalid(
            "use a dotted configuration key, such as agent.commands"
        ) from error
    parts: list[str] = []
    while isinstance(value, dict) and len(value) == 1 and len(parts) < 8:
        part, value = next(iter(value.items()))
        if (
            not part
            or len(part) > 128
            or any(ord(character) < 32 for character in part)
        ):
            raise _invalid("configuration key has an invalid part")
        parts.append(part)
    if not 2 <= len(parts) <= 8 or value is not True:
        raise _invalid("provide one dotted configuration key")
    return parts


def load_settings_snapshot(
    path: Path,
    *,
    profile_id: str,
    loader: Callable[..., Settings] = load_settings,
) -> tuple[Settings, dict[str, str]]:
    """Load settings and provenance from one immutable copy of file bytes.

    The existing loader remains the validation authority. The daemon freezes
    these sources with its settings rather than attributing later file edits to
    the already-running process. Like the loader, reading follows a symlinked
    file, as dotfile managers create; only ``set`` refuses to write through one.
    """

    try:
        raw = path.read_bytes() if path.exists() else b""
        document = tomllib.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as error:
        raise _invalid("configuration could not be read") from error
    with tempfile.TemporaryDirectory(prefix="loupe-config-") as directory:
        snapshot = Path(directory) / "config.toml"
        snapshot.write_bytes(raw)
        snapshot.chmod(0o600)
        settings = _validated_settings(snapshot, profile_id=profile_id, loader=loader)
    return settings, _sources(document, settings)


def settings_view(
    settings: Settings, sources: Mapping[str, str]
) -> list[dict[str, object]]:
    return [
        {
            "key": _setting_key(field.name),
            "value": _redact(getattr(settings, field.name), field.name),
            "source": sources.get(_setting_key(field.name), "default"),
        }
        for field in fields(settings)
    ]


def effective_config(
    path: Path, settings: Settings, sources: Mapping[str, str]
) -> dict[str, object]:
    """Show active daemon values, separately reporting saved pending changes."""

    result: dict[str, object] = {
        "config_file": str(path),
        "settings": settings_view(settings, sources),
    }
    try:
        saved, saved_sources = load_settings_snapshot(
            path, profile_id=settings.profile_id
        )
    except LlmCoordError:
        result.update(
            saved_pending_restart=None, saved_error="saved configuration is invalid"
        )
        return result
    changed = {
        _setting_key(field.name)
        for field in fields(settings)
        if getattr(saved, field.name) != getattr(settings, field.name)
    }
    result.update(
        saved_pending_restart=bool(changed),
        pending_settings=[
            item
            for item in settings_view(saved, saved_sources)
            if item["key"] in changed
        ],
    )
    return result


def initial_sources(path: Path, settings: Settings) -> dict[str, str]:
    """Fallback for embedders constructing a service with explicit settings."""

    return _sources(_read_document(path), settings)


def set_config_value(
    path: Path, *, profile_id: str, key: str, value: str
) -> dict[str, object]:
    """Validate a candidate with the loader, then replace config atomically.

    Setters in different profile daemons serialize with an owner-only file lock.
    Invalid candidates never replace the original file. File and parent fsync
    make a successful response follow the durable rename.
    """

    parts = _key_parts(key)
    try:
        parsed_document = tomllib.loads("value = " + value)
        if set(parsed_document) != {"value"}:
            raise _invalid("provide one TOML value")
        parsed: object = parsed_document["value"]
    except tomllib.TOMLDecodeError:
        parsed = value
    header_reference = (
        len(parts) == 5
        and parts[:2] == ["mcp", "servers"]
        and parts[3] == "headers"
        and isinstance(parsed, str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", parsed)
    )
    if contains_secret_material(value) or (
        any(_SENSITIVE.search(part) for part in parts) and not header_reference
    ):
        raise _invalid("secrets belong in provider login or environment variables")
    if path.is_symlink():
        raise _invalid(
            "the configuration file is a symlink; edit the file it points to"
        )
    reject_symlink_components(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    reject_symlink_components(path)
    parent_stat = path.parent.stat()
    if parent_stat.st_uid != os.getuid() or stat.S_IMODE(parent_stat.st_mode) & 0o077:
        raise _invalid("configuration directory must be owner-only")
    lock_path = path.parent / ".config.lock"
    reject_symlink_components(lock_path)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        lock_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.getuid()
            or lock_stat.st_nlink != 1
        ):
            raise _invalid("configuration lock must be an owner-local regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        reject_symlink_components(path)
        if path.exists():
            file_stat = path.stat()
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.getuid()
                or file_stat.st_nlink != 1
            ):
                raise _invalid("configuration must be an owner-local regular file")
        document = _read_document(path)
        table = document
        for part in parts[:-1]:
            child = table.setdefault(part, {})
            if not isinstance(child, dict):
                raise _invalid("configuration key crosses a scalar value")
            table = child
        table[parts[-1]] = parsed
        temporary: Path | None = None
        try:
            handle, name = tempfile.mkstemp(
                prefix=".config-", suffix=".toml", dir=path.parent
            )
            temporary = Path(name)
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                output.write(_toml(document))
                output.flush()
                os.fsync(output.fileno())
            candidate = _validated_settings(
                temporary, profile_id=profile_id, loader=load_settings
            )
            os.replace(temporary, path)
            temporary = None
            parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except OSError as error:
        raise _invalid("configuration could not be updated") from error
    finally:
        os.close(descriptor)
    return {
        "key": key,
        "value": _redact(parsed, "env" if "env" in parts else parts[-1]),
        "config_file": str(path),
        "saved": True,
        "restart_required": True,
        "profile": candidate.profile_id,
    }


def _toml(document: dict[str, Any]) -> str:
    lines: list[str] = []

    def table(data: dict[str, Any], path: tuple[str, ...], header: str = "") -> None:
        if header:
            lines.extend(("", header))
        for value in data.values():
            if (
                isinstance(value, list)
                and any(isinstance(item, dict) for item in value)
                and not all(isinstance(item, dict) for item in value)
            ):
                raise _invalid("configuration table arrays cannot mix value types")
        for key, value in data.items():
            if not isinstance(value, dict) and not (
                isinstance(value, list) and value and isinstance(value[0], dict)
            ):
                lines.append(f"{json.dumps(key)} = {_toml_value(value)}")
        for key, value in data.items():
            nested = (*path, key)
            name = ".".join(json.dumps(part) for part in nested)
            if isinstance(value, dict):
                table(value, nested, f"[{name}]")
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                for item in value:
                    table(item, nested, f"[[{name}]]")

    table(document, ())
    return "\n".join(lines).lstrip() + "\n"


def _toml_value(value: object) -> str:
    if isinstance(value, (str, bool, int, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise _invalid(
                "configuration value has an unsupported TOML type"
            ) from error
    raise _invalid("configuration value has an unsupported TOML type")


def _redact(value: object, key: str = "") -> object:
    if _SENSITIVE.search(key) or key in {"env", "headers"}:
        return _REDACTED
    if is_dataclass(value) and not isinstance(value, type):
        return _redact(asdict(value))
    if isinstance(value, Mapping):
        return {str(name): _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, (tuple, list)):
        result = []
        secret_next = False
        for item in value:
            result.append(_REDACTED if secret_next else _redact(item))
            secret_next = isinstance(item, str) and bool(_SENSITIVE.search(item))
        return result
    if isinstance(value, Path):
        return _redact(str(value))
    if isinstance(value, str):
        if contains_secret_material(value) or _SENSITIVE.search(value):
            return _REDACTED
        try:
            if urlsplit(value).password is not None:
                return _REDACTED
        except ValueError:
            return _REDACTED
    return value


def _invalid(message: str) -> LlmCoordError:
    return LlmCoordError(ErrorCode.CONFIG_INVALID, message)
