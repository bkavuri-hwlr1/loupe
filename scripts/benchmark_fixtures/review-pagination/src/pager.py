"""A small paginator whose proposed replacement is independently reviewed."""


def page(items, start, size):
    if start < 0 or size < 1:
        raise ValueError("start must be nonnegative and size must be positive")
    return [
        item for position, item in enumerate(items) if start <= position < start + size
    ]
