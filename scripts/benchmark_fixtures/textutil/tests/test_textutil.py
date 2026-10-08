from textutil import slugify


def test_words_are_joined_by_hyphens() -> None:
    assert slugify("Hello World") == "hello-world"


def test_punctuation_is_dropped() -> None:
    assert slugify("Hello, World!") == "hello-world"


def test_runs_of_spaces_collapse() -> None:
    assert slugify("  many   spaces ") == "many-spaces"


def test_existing_hyphens_are_kept_single() -> None:
    assert slugify("a - b") == "a-b"
