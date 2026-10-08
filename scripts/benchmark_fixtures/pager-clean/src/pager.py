"""Split a list into numbered pages."""


def page(items: list[str], number: int, size: int = 10) -> list[str]:
    """Return page ``number`` (counting from 1), with ``size`` items per page.

    A page past the end is empty.
    """

    if number < 1 or size < 1:
        raise ValueError("number and size must be positive")
    start = (number - 1) * size
    return items[start : start + size]


def page_count(items: list[str], size: int = 10) -> int:
    """How many pages ``items`` fills, with ``size`` items per page."""

    if size < 1:
        raise ValueError("size must be positive")
    return (len(items) + size - 1) // size
