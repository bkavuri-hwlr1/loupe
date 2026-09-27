"""Validated user-facing agent modes and legacy publication-policy mapping."""

from __future__ import annotations

AGENT_MODES = ("plan", "normal", "auto")


def validate_agent_mode(value: object) -> str:
    if not isinstance(value, str) or value not in AGENT_MODES:
        raise ValueError("mode must be plan, normal, or auto")
    return value


def resolve_agent_mode(
    mode: object = None, publish: object = None, *, default: str = "normal"
) -> str:
    """Resolve an explicit mode or the older auto/review publication option."""

    selected = validate_agent_mode(mode) if mode is not None else None
    legacy: str | None = None
    if publish is not None:
        if not isinstance(publish, str) or publish not in {"auto", "review"}:
            raise ValueError("publish must be auto or review")
        legacy = "normal" if publish == "review" else "auto"
    if selected is not None and legacy is not None and selected != legacy:
        raise ValueError("mode conflicts with the requested publication policy")
    return selected or legacy or validate_agent_mode(default)


def publication_mode(mode: str) -> str:
    return "review" if validate_agent_mode(mode) == "normal" else "auto"


__all__ = [
    "AGENT_MODES",
    "publication_mode",
    "resolve_agent_mode",
    "validate_agent_mode",
]
