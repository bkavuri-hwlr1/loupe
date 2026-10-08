"""Fetch one public web page as text, with no access to private networks.

Only http and https URLs without credentials are fetched. Every address a host
resolves to must be public, so loopback, private, link-local (including cloud
metadata), and carrier-grade NAT addresses are refused, and the connection is
pinned to an address that was checked, so DNS rebinding cannot redirect it.
Redirects are checked the same way at every hop. Requests carry no cookies or
credentials and ignore proxy settings. HTML is reduced to readable text.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from llm_cli import __version__

_MAX_REDIRECTS = 5
_USER_AGENT = f"Loupe/{__version__} (documentation fetch)"
_TEXT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
)
_HTML_TYPES = ("text/html", "application/xhtml+xml")


class WebFetchError(Exception):
    """A URL could not be fetched; the message is safe to show the model."""


@dataclass(frozen=True)
class Page:
    url: str
    status: int
    content_type: str
    text: str
    truncated: bool


Resolver = Callable[[str, int], Sequence[str]]


def public_addresses(host: str, port: int) -> list[str]:
    """Resolve ``host``, refusing it unless every address is public."""

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise WebFetchError(f"{host} could not be resolved") from exc
    addresses = sorted({str(info[4][0]) for info in infos})
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global or ip.is_multicast:
            raise WebFetchError(f"{host} is not a public address")
    if not addresses:
        raise WebFetchError(f"{host} could not be resolved")
    return addresses


def check_url(url: str) -> tuple[str, str, int]:
    """Validate a URL; return its scheme, lowercase host, and port."""

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise WebFetchError("that is not a valid URL") from exc
    if parts.scheme not in {"http", "https"}:
        raise WebFetchError("only http and https URLs can be fetched")
    if parts.username is not None or parts.password is not None:
        raise WebFetchError("URLs with credentials cannot be fetched")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise WebFetchError("the URL has no host")
    return parts.scheme, host, port or (443 if parts.scheme == "https" else 80)


def fetch(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    allowed: Callable[[str, str], str | None],
    resolver: Resolver = public_addresses,
    tls: ssl.SSLContext | None = None,
) -> Page:
    """Fetch ``url``, following redirects.

    ``allowed(host, url)`` returns a refusal message for a host the policy
    does not allow, so redirects to other hosts are approved like the first.
    """

    context = tls or ssl.create_default_context()
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        scheme, host, port = check_url(current)
        refusal = allowed(host, current)
        if refusal is not None:
            raise WebFetchError(refusal)
        address = resolver(host, port)[0]
        parts = urlsplit(current)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        connection = _connection(scheme, host, address, port, timeout, context)
        try:
            connection.request(
                "GET",
                target,
                headers={
                    "Host": parts.netloc.rsplit("@", 1)[-1],
                    "User-Agent": _USER_AGENT,
                    "Accept": (
                        "text/html, text/markdown, text/plain, application/json;"
                        "q=0.9, */*;q=0.1"
                    ),
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            location = response.getheader("Location")
            if response.status in {301, 302, 303, 307, 308} and location:
                current = urljoin(current, location)
                continue
            if response.status >= 400:
                raise WebFetchError(f"the server answered HTTP {response.status}")
            content_type = response.getheader("Content-Type") or "text/plain"
            media = content_type.split(";", 1)[0].strip().lower()
            if not media.startswith(_TEXT_TYPES) and not media.endswith(
                ("+json", "+xml")
            ):
                raise WebFetchError(f"{media} content is not text and was not read")
            body = response.read(max_bytes + 1)
        except (OSError, http.client.HTTPException) as exc:
            if isinstance(exc, ssl.SSLCertVerificationError):
                raise WebFetchError(f"{host} has an invalid certificate") from exc
            raise WebFetchError(f"{host} could not be reached") from exc
        finally:
            connection.close()
        truncated = len(body) > max_bytes
        text = _decode(body[:max_bytes], content_type)
        if media in _HTML_TYPES:
            text = html_to_text(text, base=current)
        return Page(current, response.status, media, text, truncated)
    raise WebFetchError(f"more than {_MAX_REDIRECTS} redirects")


def _connection(
    scheme: str,
    host: str,
    address: str,
    port: int,
    timeout: float,
    context: ssl.SSLContext,
) -> http.client.HTTPConnection:
    if scheme == "https":
        return _PinnedHTTPSConnection(host, address, port, timeout, context)
    return _PinnedHTTPConnection(host, address, port, timeout)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connect to a checked address while naming the original host."""

    def __init__(self, host: str, address: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS to a checked address, verifying the certificate for the host."""

    def __init__(
        self,
        host: str,
        address: str,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._tls = context

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)


def _decode(body: bytes, content_type: str) -> str:
    message = Message()
    message["Content-Type"] = content_type
    charset = message.get_content_charset() or "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


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


__all__ = [
    "Page",
    "WebFetchError",
    "check_url",
    "fetch",
    "html_to_text",
    "public_addresses",
]
