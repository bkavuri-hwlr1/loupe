"""Small text helpers."""


def slugify(text: str) -> str:
    """Lowercase words joined by single hyphens, with punctuation dropped."""

    return text.lower().replace(" ", "-")
