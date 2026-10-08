"""One task's web policy: which domains need approval, and fetched pages."""

from __future__ import annotations

import ssl
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from llm_cli.web.fetch import Page, Resolver, fetch, public_addresses

_MAX_PAGE_BYTES = 2 * 1024 * 1024
_TIMEOUT_SECONDS = 20.0
_MAX_CACHED_PAGES = 32


def domain_matches(host: str, pattern: str) -> bool:
    """``docs.python.org`` matches only itself; ``*.example.com`` matches
    its subdomains but not ``example.com``."""

    if pattern.startswith("*."):
        return host.endswith(pattern[1:])
    return host == pattern


class WebAccess:
    """Policy, approvals, and a page cache for one task's web_fetch calls."""

    def __init__(
        self,
        mode: str,
        domains: Sequence[str] = (),
        *,
        resolver: Resolver = public_addresses,
        tls: ssl.SSLContext | None = None,
        timeout: float = _TIMEOUT_SECONDS,
        max_bytes: int = _MAX_PAGE_BYTES,
    ) -> None:
        self.mode = mode
        self.domains = tuple(domains)
        self._resolver = resolver
        self._tls = tls
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._approved: set[str] = set()
        self._pages: dict[str, Page] = {}

    def available(self, *, interactive: bool) -> bool:
        """Whether any fetch could be allowed: by policy, list, or approval."""

        return self.mode == "allow" or bool(self.domains) or interactive

    def allowed_without_asking(self, host: str) -> bool:
        return (
            self.mode == "allow"
            or host in self._approved
            or any(domain_matches(host, pattern) for pattern in self.domains)
        )

    def approve(self, host: str) -> None:
        self._approved.add(host)

    def page(
        self, url: str, *, allowed: Callable[[str, str], str | None]
    ) -> tuple[Page, bool]:
        """Fetch ``url``, or reuse this task's earlier fetch of it; the flag
        says whether it was downloaded now."""

        cached = self._pages.get(url)
        if cached is not None:
            return cached, False
        page = fetch(
            url,
            max_bytes=self._max_bytes,
            timeout=self._timeout,
            allowed=allowed,
            resolver=self._resolver,
            tls=self._tls,
        )
        if len(self._pages) >= _MAX_CACHED_PAGES:
            self._pages.pop(next(iter(self._pages)))
        self._pages[url] = page
        return page, True


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").rstrip(".").lower()


__all__ = ["WebAccess", "domain_matches", "host_of"]
