"""A small application component with a prescribed review proposal."""


def page(items, start, size):
    if start < 0 or size < 1:
        raise ValueError("invalid page")
    return list(items)[start : start + size]
