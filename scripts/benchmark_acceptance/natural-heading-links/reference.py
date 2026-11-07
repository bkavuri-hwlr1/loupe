from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

_SKIPPED = {"script", "style", "noscript", "svg", "template", "head", "iframe"}


_BLOCKS = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "header",
    "footer",
    "nav",
    "aside",
    "ul",
    "ol",
    "li",
    "table",
    "tr",
    "pre",
    "blockquote",
    "br",
    "hr",
    "dl",
    "dt",
    "dd",
    "figure",
    "figcaption",
    "form",
}


_HEADINGS = {
    "h1": "#",
    "h2": "##",
    "h3": "###",
    "h4": "####",
    "h5": "#####",
    "h6": "######",
}


class _TextExtractor(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts: list[str] = []
        self.skipping = 0
        self.preformatted = 0
        self.link: str | None = None
        self.anchor: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self.skipping += 1
        elif self.skipping:
            return
        elif tag in _HEADINGS:
            self.parts.append(f"\n\n{_HEADINGS[tag]} ")
        elif tag in _BLOCKS:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")
            elif tag == "pre":
                self.preformatted += 1
        elif tag == "a":
            href = dict(attrs).get("href")
            if href and href.startswith("#"):
                self.anchor = len(self.parts)
            elif href and not href.startswith("javascript:"):
                self.link = urljoin(self.base, href)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self.skipping = max(0, self.skipping - 1)
        elif self.skipping:
            return
        elif tag in _HEADINGS or tag in _BLOCKS:
            if tag == "pre":
                self.preformatted = max(0, self.preformatted - 1)
            if tag != "li":  # The next item starts its own line.
                self.parts.append("\n")
        elif tag == "a" and self.link:
            self.parts.append(f" ({self.link})")
            self.link = None
        elif tag == "a" and self.anchor is not None:
            # A heading's permalink marker, such as ¶ or #, is not text.
            if not any(c.isalnum() for c in "".join(self.parts[self.anchor :])):
                del self.parts[self.anchor :]
            self.anchor = None

    def handle_data(self, data: str) -> None:
        if self.skipping:
            return
        self.parts.append(data if self.preformatted else re.sub(r"\s+", " ", data))


def html_to_text(html: str, *, base: str = "") -> str:
    """Readable text from HTML: headings marked, links kept, scripts dropped."""

    extractor = _TextExtractor(base)
    extractor.feed(html)
    extractor.close()
    lines = (line.rstrip() for line in "".join(extractor.parts).splitlines())
    text = "\n".join(line if line.strip() else "" for line in lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
