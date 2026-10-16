"""A deliberately incomplete public check: it misses a nonempty page's end."""

import pytest
from src.pager import page


def test_empty_page():
    assert page([], 0, 10) == []


def test_invalid_size():
    with pytest.raises(ValueError):
        page([1], 0, 0)
