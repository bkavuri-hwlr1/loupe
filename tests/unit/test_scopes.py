from __future__ import annotations

import pytest

from llm_cli.coordination.scopes import (
    MAX_SCOPE_LENGTH,
    MAX_SCOPES_PER_CLAIM,
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


def test_normalize_scope_preserves_exact_and_directory_meaning() -> None:
    assert normalize_scope("  src/parser.py  ") == "src/parser.py"
    assert normalize_scope("src\\parsing\\") == "src/parsing/"
    assert normalize_scope("docs/a file.md") == "docs/a file.md"
    assert normalize_scope("*") == "*"


@pytest.mark.parametrize(
    "unsafe",
    [
        "",
        "   ",
        "/absolute/path",
        "\\\\server\\share",
        "C:/repository/file.py",
        "d:\\repository\\file.py",
        "~/file.py",
        "~someone/file.py",
        "src//file.py",
        "src/./file.py",
        "src/../file.py",
        ".git/config",
        "src/.GIT/config",
        "src/\x00file.py",
        "src///",
    ],
)
def test_normalize_scope_rejects_unsafe_forms(unsafe: str) -> None:
    with pytest.raises(ScopeValidationError):
        normalize_scope(unsafe)


def test_normalize_scope_rejects_non_string_and_overlong_input() -> None:
    with pytest.raises(ScopeValidationError):
        normalize_scope(123)  # type: ignore[arg-type]
    assert len("a" * MAX_SCOPE_LENGTH) == MAX_SCOPE_LENGTH
    assert normalize_scope("a" * MAX_SCOPE_LENGTH) == "a" * MAX_SCOPE_LENGTH
    with pytest.raises(ScopeValidationError):
        normalize_scope("a" * (MAX_SCOPE_LENGTH + 1))


def test_normalize_scopes_collapses_duplicates_children_and_whole_repo() -> None:
    assert normalize_scopes(
        ["tests/unit/test_b.py", "src/z.py", "src/", "src/a.py", "src/"]
    ) == ("src/", "tests/unit/test_b.py")
    assert normalize_scopes(["src/a.py", "*", "tests/a.py"]) == ("*",)


def test_normalize_scopes_empty_input_falls_back_to_whole_repo() -> None:
    assert normalize_scopes([]) == ("*",)


def test_normalize_scopes_is_canonical_and_idempotent() -> None:
    first = normalize_scopes(["z.py", "a/b.py", "a/", "m.py", "z.py"])
    assert first == ("a/", "m.py", "z.py")
    assert normalize_scopes(first) == first
    assert normalize_scopes(reversed(first)) == first


def test_scope_limit_is_enforced_before_deduplication() -> None:
    assert (
        len(
            normalize_scopes(
                f"file-{index}.py" for index in range(MAX_SCOPES_PER_CLAIM)
            )
        )
        == MAX_SCOPES_PER_CLAIM
    )
    with pytest.raises(ScopeValidationError):
        normalize_scopes("same.py" for _ in range(MAX_SCOPES_PER_CLAIM + 1))


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("*", "any/path.py", True),
        ("src/a.py", "src/a.py", True),
        ("src/", "src/a.py", True),
        ("src/", "src/nested/", True),
        ("src/nested/", "src/", True),
        ("src/", "source/a.py", False),
        ("src/a.py", "src/b.py", False),
        ("src", "src/", False),
        ("Src/", "src/a.py", False),
    ],
)
def test_scope_overlap_is_symmetric(left: str, right: str, expected: bool) -> None:
    assert scopes_overlap(left, right) is expected
    assert scopes_overlap(right, left) is expected


def test_scope_set_overlap_uses_collapsed_claim_semantics() -> None:
    assert scope_sets_overlap(["src/", "tests/a.py"], ["docs/", "src/a.py"])
    assert not scope_sets_overlap(["src/"], ["tests/", "docs/a.md"])


def test_path_coverage_handles_exact_directory_and_whole_repo_scopes() -> None:
    assert scope_covers_path("src/a.py", "src/a.py")
    assert not scope_covers_path("src/a.py", "src/b.py")
    assert scope_covers_path("src/", "src/nested/a.py")
    assert not scope_covers_path("src/", "source/a.py")
    assert scope_covers_path("*", "any/file.py")


def test_coverage_reports_canonical_uncovered_paths() -> None:
    scopes = ["src/", "README.md"]
    changed = ["tests/test_app.py", "src/app.py", "README.md", "docs/guide.md"]
    assert uncovered_paths(scopes, changed) == ("docs/guide.md", "tests/test_app.py")
    assert not scopes_cover_paths(scopes, changed)
    assert not changed_paths_covered(scopes, changed)
    assert scopes_cover_paths(scopes, ["src/app.py", "README.md"])
    assert changed_paths_covered(scopes, ["src/app.py", "README.md"])
    assert scopes_cover_paths(scopes, [])


def test_rename_requires_source_and_destination_coverage() -> None:
    assert scopes_cover_paths(["old/", "new/"], ["old/name.py", "new/name.py"])
    assert not scopes_cover_paths(["new/"], ["old/name.py", "new/name.py"])


@pytest.mark.parametrize("changed", ["*", "src/", "../escape.py", ".git/index"])
def test_changed_paths_must_be_exact_and_safe(changed: str) -> None:
    with pytest.raises(ScopeValidationError):
        normalize_changed_path(changed)


def test_casefold_aliases_are_detected_without_changing_git_spelling() -> None:
    assert casefold_aliases(["Src/", "src/a.py"]) == (("Src/", "src/a.py"),)
    assert casefold_aliases(["src/", "src/a.py"]) == ()
    with pytest.raises(CaseFoldAliasError):
        normalize_scopes(["Src/", "src/a.py"], case_insensitive_filesystem=True)


def test_casefold_only_coverage_fails_closed() -> None:
    with pytest.raises(CaseFoldAliasError):
        scopes_cover_paths(
            ["Src/"],
            ["src/a.py"],
            case_insensitive_filesystem=True,
        )
