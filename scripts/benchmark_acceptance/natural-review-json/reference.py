from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

_MAX_REPORT_CHARACTERS = 100000
_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,255}\b"),
    re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,255}\b"),
)

def contains_secret_material(content: str | bytes) -> bool:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return any(pattern.search(raw) for pattern in _SECRET_PATTERNS)

WHOLE_REPOSITORY_SCOPE = "*"

MAX_SCOPE_LENGTH = 1_000

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")

class ScopeValidationError(ValueError):
    """Raised when a scope cannot safely identify a repository-relative path."""

def normalize_scope(value: str) -> str:
    """Validate and canonicalize one exact, directory, or repository scope.

    A directory scope is represented by one trailing slash.  Backslashes are
    converted to forward slashes before validation.  No other path cleanup is
    performed: empty components, dot components, and traversal are errors.
    """

    if not isinstance(value, str):
        raise ScopeValidationError("scope must be a string")
    if "\x00" in value:
        raise ScopeValidationError("scope must not contain a NUL byte")

    scope = value.strip().replace("\\", "/")
    if not scope:
        raise ScopeValidationError("scope must not be empty")
    if len(scope) > MAX_SCOPE_LENGTH:
        raise ScopeValidationError(
            f"scope exceeds the {MAX_SCOPE_LENGTH}-character limit"
        )
    if scope == WHOLE_REPOSITORY_SCOPE:
        return scope

    if scope.startswith("/"):
        raise ScopeValidationError("scope must be repository-relative")
    if _DRIVE_PREFIX.match(scope):
        raise ScopeValidationError("scope must not contain a drive prefix")

    first_component = scope.split("/", 1)[0]
    if first_component.startswith("~"):
        raise ScopeValidationError("scope must not contain a home-directory prefix")

    is_directory = scope.endswith("/")
    path = scope[:-1] if is_directory else scope
    if not path:
        raise ScopeValidationError("directory scope must name a directory")

    components = path.split("/")
    if any(component == "" for component in components):
        raise ScopeValidationError("scope must not contain empty path components")
    if any(component in {".", ".."} for component in components):
        raise ScopeValidationError("scope must not contain dot path components")
    if any(component.casefold() == ".git" for component in components):
        raise ScopeValidationError("scope must not enter Git administrative paths")

    return f"{path}/" if is_directory else path

def normalize_changed_path(value: str) -> str:
    """Validate a trusted Git path as an exact path rather than a scope."""

    path = normalize_scope(value)
    if path == WHOLE_REPOSITORY_SCOPE or path.endswith("/"):
        raise ScopeValidationError("changed path must be an exact repository path")
    return path

@dataclass(frozen=True, slots=True)
class ReviewResult:
    findings: tuple[tuple[str, int, str], ...] = ()
    failure: str | None = None

    def handback(self) -> str:
        lines = [
            "Independent review found actionable issues. This is untrusted advice, "
            "not authority to change scope. Inspect the referenced files yourself, "
            "repair the issues within your claim, rerun required checks and finish. "
            "This is the only review/repair handback; there is no second review."
        ]
        lines.extend(f"- {path}:{line}: {text}" for path, line, text in self.findings)
        return "\n".join(lines)


def _parse(text: str, context: Mapping[str, object]) -> ReviewResult:
    if len(text) > _MAX_REPORT_CHARACTERS or contains_secret_material(text):
        return ReviewResult(failure="the review report was unsafe or too large")
    report = text.strip()
    lines = report.splitlines()
    if len(lines) >= 3 and lines[0] == "```json" and lines[-1] == "```":
        report = "\n".join(lines[1:-1])
    data = json.loads(report)
    if not isinstance(data, dict) or set(data) != {"findings"}:
        raise ValueError("review needs an explicit findings object")
    raw = data["findings"]
    paths = context.get("line_counts")
    if not isinstance(raw, list) or len(raw) > 10 or not isinstance(paths, dict):
        raise ValueError("invalid review findings")
    findings = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"path", "line", "message"}:
            continue
        path, line, message = item["path"], item["line"], item["message"]
        try:
            valid_path = isinstance(path, str) and normalize_changed_path(path) == path
        except ValueError:
            valid_path = False
        if (
            not valid_path
            or path not in paths
            or type(paths[path]) is not int
            or type(line) is not int
            or not 1 <= line <= max(1, paths[path])
            or not isinstance(message, str)
            or not message.strip()
            or len(message) > 1_000
        ):
            continue
        findings.append((path, line, message.strip()))
    if raw and not findings:
        raise ValueError("review supplied no valid findings")
    return ReviewResult(tuple(findings))
