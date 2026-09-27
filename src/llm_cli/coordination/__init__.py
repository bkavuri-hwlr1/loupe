"""Repository coordination primitives."""

from llm_cli.coordination.scopes import (
    MAX_SCOPE_LENGTH,
    MAX_SCOPES_PER_CLAIM,
    WHOLE_REPOSITORY_SCOPE,
    CaseFoldAliasError,
    ScopeValidationError,
    casefold_aliases,
    changed_paths_covered,
    normalize_changed_path,
    normalize_scope,
    normalize_scopes,
    scope_covers_path,
    scope_sets_overlap,
    scopes_cover_paths,
    scopes_overlap,
    uncovered_paths,
)

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
