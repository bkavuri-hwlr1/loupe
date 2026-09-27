"""Canonical repository path scopes used by the coordination authority.

The coordinator deliberately treats scopes as repository-relative Git path
spellings, not operating-system paths.  In particular, this module does not
call ``resolve()``, ``normpath()``, or otherwise let the host filesystem turn
an unsafe spelling into a safe-looking one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

WHOLE_REPOSITORY_SCOPE = "*"
MAX_SCOPE_LENGTH = 1_000
MAX_SCOPES_PER_CLAIM = 500

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class ScopeValidationError(ValueError):
    """Raised when a scope cannot safely identify a repository-relative path."""


class CaseFoldAliasError(ScopeValidationError):
    """Raised when case-folding would change overlap or coverage semantics."""


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


def normalize_scopes(
    values: Iterable[str],
    *,
    case_insensitive_filesystem: bool = False,
) -> tuple[str, ...]:
    """Return a deterministic, minimal tuple of canonical scopes.

    Empty scope collections conservatively fall back to ``("*",)``.  Unsafe
    non-empty values are rejected rather than silently broadened; the planning
    layer may explicitly choose a whole-repository fallback after recording
    why its model output was invalid.

    The input-count limit is enforced before deduplication so duplicate input
    cannot bypass the per-claim resource bound.
    """

    normalized: list[str] = []
    for count, value in enumerate(values, start=1):
        if count > MAX_SCOPES_PER_CLAIM:
            raise ScopeValidationError(
                f"claim exceeds the {MAX_SCOPES_PER_CLAIM}-scope limit"
            )
        normalized.append(normalize_scope(value))

    if not normalized or WHOLE_REPOSITORY_SCOPE in normalized:
        return (WHOLE_REPOSITORY_SCOPE,)

    unique = sorted(set(normalized))
    if case_insensitive_filesystem:
        aliases = casefold_aliases(unique)
        if aliases:
            left, right = aliases[0]
            raise CaseFoldAliasError(
                f"scope spellings are ambiguous on a case-insensitive filesystem: "
                f"{left!r} and {right!r}"
            )

    directory_scopes = tuple(scope for scope in unique if scope.endswith("/"))
    return tuple(
        scope
        for scope in unique
        if not any(
            parent != scope and _scope_covers_normalized(parent, scope)
            for parent in directory_scopes
        )
    )


def scopes_overlap(
    left: str,
    right: str,
    *,
    case_insensitive_filesystem: bool = False,
) -> bool:
    """Return whether two individual scopes contend for any changed path."""

    canonical_left = normalize_scope(left)
    canonical_right = normalize_scope(right)
    if case_insensitive_filesystem:
        canonical_left = canonical_left.casefold()
        canonical_right = canonical_right.casefold()
    return _scopes_overlap_normalized(canonical_left, canonical_right)


def scope_sets_overlap(
    left: Iterable[str],
    right: Iterable[str],
    *,
    case_insensitive_filesystem: bool = False,
) -> bool:
    """Return whether any scope in two canonicalized claim scope sets overlaps.

    On a case-insensitive working tree two spellings that differ only by case
    name the same physical path, so they must contend.  Comparing the canonical
    spellings alone would grant two agents simultaneous authority over one
    directory, which is precisely the overwrite this module exists to prevent.
    """

    left_scopes = normalize_scopes(left)
    right_scopes = normalize_scopes(right)
    if case_insensitive_filesystem:
        left_scopes = tuple(scope.casefold() for scope in left_scopes)
        right_scopes = tuple(scope.casefold() for scope in right_scopes)
    return any(
        _scopes_overlap_normalized(left_scope, right_scope)
        for left_scope in left_scopes
        for right_scope in right_scopes
    )


def scope_covers_path(scope: str, changed_path: str) -> bool:
    """Return whether one scope covers one exact Git changed path."""

    canonical_scope = normalize_scope(scope)
    canonical_path = normalize_changed_path(changed_path)
    return _scope_covers_normalized(canonical_scope, canonical_path)


def uncovered_paths(
    scopes: Iterable[str],
    changed_paths: Iterable[str],
    *,
    case_insensitive_filesystem: bool = False,
) -> tuple[str, ...]:
    """Return deterministic exact changed paths not covered by ``scopes``.

    Rename and copy validation should pass both source and destination names.
    When case-insensitive validation is requested, a path that would be covered
    only after case-folding is rejected as an alias instead of being granted
    authority under a spelling the claim did not reserve.
    """

    canonical_scopes = normalize_scopes(
        scopes, case_insensitive_filesystem=case_insensitive_filesystem
    )
    canonical_paths = tuple(
        sorted({normalize_changed_path(path) for path in changed_paths})
    )

    uncovered: list[str] = []
    for path in canonical_paths:
        if any(_scope_covers_normalized(scope, path) for scope in canonical_scopes):
            continue
        if case_insensitive_filesystem and any(
            _scope_covers_normalized(scope.casefold(), path.casefold())
            for scope in canonical_scopes
        ):
            raise CaseFoldAliasError(
                f"changed path {path!r} is covered only by a case-folded scope alias"
            )
        uncovered.append(path)
    return tuple(uncovered)


def scopes_cover_paths(
    scopes: Iterable[str],
    changed_paths: Iterable[str],
    *,
    case_insensitive_filesystem: bool = False,
) -> bool:
    """Return whether all exact Git changed paths are covered by the claim."""

    return not uncovered_paths(
        scopes,
        changed_paths,
        case_insensitive_filesystem=case_insensitive_filesystem,
    )


def changed_paths_covered(
    scopes: Iterable[str],
    changed_paths: Iterable[str],
    *,
    case_insensitive_filesystem: bool = False,
) -> bool:
    """Compatibility name for the coordinator's changed-path coverage check."""

    return scopes_cover_paths(
        scopes,
        changed_paths,
        case_insensitive_filesystem=case_insensitive_filesystem,
    )


def normalize_changed_path(value: str) -> str:
    """Validate a trusted Git path as an exact path rather than a scope."""

    path = normalize_scope(value)
    if path == WHOLE_REPOSITORY_SCOPE or path.endswith("/"):
        raise ScopeValidationError("changed path must be an exact repository path")
    return path


def casefold_aliases(values: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """Find pairs whose overlap semantics appear only after case-folding."""

    aliases: list[tuple[str, str]] = []
    for index, left in enumerate(values):
        canonical_left = normalize_scope(left)
        for right in values[index + 1 :]:
            canonical_right = normalize_scope(right)
            if _scopes_overlap_normalized(canonical_left, canonical_right):
                continue
            if _scopes_overlap_normalized(
                canonical_left.casefold(), canonical_right.casefold()
            ):
                aliases.append((canonical_left, canonical_right))
    return tuple(aliases)


def _scope_covers_normalized(scope: str, path_or_scope: str) -> bool:
    if scope == WHOLE_REPOSITORY_SCOPE:
        return True
    if scope.endswith("/"):
        return path_or_scope.startswith(scope)
    return scope == path_or_scope


def _scopes_overlap_normalized(left: str, right: str) -> bool:
    if WHOLE_REPOSITORY_SCOPE in {left, right}:
        return True
    if left == right:
        return True
    if left.endswith("/") and right.startswith(left):
        return True
    return right.endswith("/") and left.startswith(right)


__all__ = [
    "MAX_SCOPES_PER_CLAIM",
    "MAX_SCOPE_LENGTH",
    "WHOLE_REPOSITORY_SCOPE",
    "CaseFoldAliasError",
    "ScopeValidationError",
    "casefold_aliases",
    "changed_paths_covered",
    "normalize_changed_path",
    "normalize_scope",
    "normalize_scopes",
    "scope_covers_path",
    "scope_sets_overlap",
    "scopes_cover_paths",
    "scopes_overlap",
    "uncovered_paths",
]
