"""One task's web policy: which domains need approval, and fetched pages."""

from __future__ import annotations

import ssl
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, wait
from urllib.parse import urlsplit

from llm_cli.web.fetch import Page, Resolver, WebFetchError, fetch, public_addresses

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
        proxy: str | None = None,
    ) -> None:
        self.mode = mode
        self.domains = tuple(domains)
        self._resolver = resolver
        self._tls = tls
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._proxy = proxy
        self._approved: set[str] = set()
        self._pages: dict[str, Page] = {}
        self._in_flight: dict[str, Future[Page]] = {}
        self._lock = threading.Lock()

    def available(self, *, interactive: bool) -> bool:
        """Whether any fetch could be allowed: by policy, list, or approval."""

        return self.mode == "allow" or bool(self.domains) or interactive

    def allowed_without_asking(self, host: str) -> bool:
        with self._lock:
            return (
                self.mode == "allow"
                or host in self._approved
                or any(domain_matches(host, pattern) for pattern in self.domains)
            )

    def approve(self, host: str) -> None:
        with self._lock:
            self._approved.add(host)

    def page(
        self,
        url: str,
        *,
        allowed: Callable[[str, str], str | None],
        check_cancelled: Callable[[], None] | None = None,
    ) -> tuple[Page, bool]:
        """Fetch once per URL, sharing a pending fetch and the bounded cache.

        Only the owner reports a download. Network, approvals, cancellation,
        and future waits never hold the cache lock.
        """

        if check_cancelled is not None:
            check_cancelled()
        with self._lock:
            cached = self._pages.get(url)
            if cached is not None:
                return cached, False
            pending = self._in_flight.get(url)
            owner = pending is None
            if pending is None:
                pending = self._in_flight[url] = Future()
        if not owner:
            return self._wait_for_page(pending, check_cancelled), False
        try:
            page = fetch(
                url,
                max_bytes=self._max_bytes,
                timeout=self._timeout,
                allowed=allowed,
                resolver=self._resolver,
                tls=self._tls,
                proxy=self._proxy,
            )
            if check_cancelled is not None:
                check_cancelled()
        except BaseException as exc:
            with self._lock:
                del self._in_flight[url]
            pending.set_exception(exc)
            raise
        with self._lock:
            if len(self._pages) >= _MAX_CACHED_PAGES:
                self._pages.pop(next(iter(self._pages)))
            self._pages[url] = page
            del self._in_flight[url]
        pending.set_result(page)
        return page, True

    def _wait_for_page(
        self, pending: Future[Page], check_cancelled: Callable[[], None] | None
    ) -> Page:
        # A stuck owner must not park a helper indefinitely or swallow task
        # cancellation. Waiting never extends the normal per-fetch timeout.
        deadline = time.monotonic() + self._timeout
        while True:
            if check_cancelled is not None:
                check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebFetchError("waiting for the page exceeded the fetch timeout")
            ready, _ = wait((pending,), timeout=min(0.1, remaining))
            if not ready:
                continue
            page = pending.result()
            if check_cancelled is not None:
                check_cancelled()
            return page


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").rstrip(".").lower()


__all__ = ["WebAccess", "domain_matches", "host_of"]
