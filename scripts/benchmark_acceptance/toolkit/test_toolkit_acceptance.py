"""Hidden acceptance for independent modules and shared registry publication."""

import importlib

import pytest


def test_text_normalizes_whitespace() -> None:
    from toolkit.text import normalize_space

    assert normalize_space("  Hello\t world\nagain ") == "Hello world again"


def test_text_handles_unicode_whitespace() -> None:
    from toolkit.text import normalize_space

    assert normalize_space("one\u2003two\u00a0three") == "one two three"


def test_text_normalizes_empty_strings() -> None:
    from toolkit.text import normalize_space

    assert normalize_space("\t\n  ") == normalize_space("") == ""


def test_text_slugs_ascii_words() -> None:
    from toolkit.text import slug

    assert slug(" Hello, WORLD! 42 ") == "hello-world-42"


def test_text_slugs_strip_repeated_separators() -> None:
    from toolkit.text import slug

    assert slug("__hello---world__") == "hello-world"
    assert slug("!!!") == ""


def test_text_slugs_are_ascii() -> None:
    from toolkit.text import slug

    assert slug("café 東京 tea") == "caf-tea"


def test_text_registry_is_enabled() -> None:
    from toolkit.registry import FEATURES

    assert FEATURES["text"] is True


def test_numbers_mean_is_float() -> None:
    from toolkit.numbers import mean

    assert mean([1, 2, 3, 4]) == 2.5
    assert isinstance(mean([2]), float)


def test_numbers_mean_accepts_iterators_and_negative_values() -> None:
    from toolkit.numbers import mean

    assert mean(iter([-4, 2, 8])) == 2.0


def test_numbers_empty_mean_is_refused() -> None:
    from toolkit.numbers import mean

    with pytest.raises(ValueError):
        mean([])


def test_numbers_clamp_inside_and_outside_bounds() -> None:
    from toolkit.numbers import clamp

    assert clamp(-3, 0, 10) == 0
    assert clamp(4, 0, 10) == 4
    assert clamp(14, 0, 10) == 10


def test_numbers_clamp_equal_and_float_bounds() -> None:
    from toolkit.numbers import clamp

    assert clamp(5, 2, 2) == 2
    assert clamp(0.75, 0.1, 0.5) == 0.5


def test_numbers_reversed_bounds_are_refused() -> None:
    from toolkit.numbers import clamp

    with pytest.raises(ValueError):
        clamp(2, 4, 1)


def test_numbers_registry_is_enabled() -> None:
    from toolkit.registry import FEATURES

    assert FEATURES["numbers"] is True


def test_both_features_survive_together() -> None:
    from toolkit.numbers import mean
    from toolkit.registry import FEATURES
    from toolkit.text import normalize_space

    assert FEATURES == {"text": True, "numbers": True}
    assert normalize_space(f"Mean:  {mean([2, 4])}") == "Mean: 3.0"


def test_operator_edit_survives() -> None:
    registry = importlib.import_module("toolkit.registry")
    assert registry.operator_note() == "registry edited outside Loupe"
