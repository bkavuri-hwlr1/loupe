"""Parallel helper fetches share a bounded task cache without holding network locks."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from llm_cli.agent.tools import TaskCancelled
from llm_cli.web.access import WebAccess
from llm_cli.web.fetch import Page, WebFetchError


def _page(url: str) -> Page:
    return Page(url, 200, "text/plain", url, False)


def test_parallel_insertions_at_full_cache_do_not_evict_the_same_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    returning = threading.Barrier(2)
    second_pop = threading.Event()
    count_lock = threading.Lock()
    pops = 0

    class PausingCache(dict[str, Page]):
        def pop(self, key: str, *args: Any) -> Any:
            nonlocal pops
            with count_lock:
                pops += 1
                first = pops == 1
                if pops == 2:
                    second_pop.set()
            # Force an unprotected compound eviction to interleave. A correctly
            # locked second insertion waits until the first releases the lock.
            if first:
                second_pop.wait(0.2)
            return super().pop(key, *args)

    def fetch(url: str, **kwargs: Any) -> Page:
        returning.wait(timeout=5)
        return _page(url)

    monkeypatch.setattr("llm_cli.web.access.fetch", fetch)
    web = WebAccess("allow")
    web._pages = PausingCache({str(index): _page(str(index)) for index in range(32)})
    urls = ["https://one.example/", "https://two.example/"]
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = [
            workers.submit(web.page, url, allowed=lambda *args: None) for url in urls
        ]
        assert [result.result(timeout=5)[0].url for result in pending] == urls
    assert len(web._pages) == 32
    assert all(url in web._pages for url in urls)


def test_same_url_fetch_is_coalesced_and_only_owner_reports_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = threading.Barrier(2)
    downloading = threading.Event()
    duplicate = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    downloads: list[str] = []

    def fetch(url: str, **kwargs: Any) -> Page:
        with lock:
            downloads.append(url)
            if len(downloads) > 1:
                duplicate.set()
        downloading.set()
        assert release.wait(5)
        return _page(url)

    monkeypatch.setattr("llm_cli.web.access.fetch", fetch)
    web = WebAccess("allow")
    url = "https://same.example/"

    def read() -> tuple[Page, bool]:
        start.wait(timeout=5)
        return web.page(url, allowed=lambda *args: None)

    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = [workers.submit(read) for _ in range(2)]
        assert downloading.wait(5)
        duplicated = duplicate.wait(0.05)
        release.set()
        pages = [result.result(timeout=5) for result in pending]
    assert not duplicated and downloads == [url]
    assert sorted(downloaded for _, downloaded in pages) == [False, True]
    assert web.page(url, allowed=lambda *args: None) == (_page(url), False)


def test_coalesced_waiter_can_cancel_without_canceling_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloading = threading.Event()
    release = threading.Event()
    checking = threading.Event()
    cancelled = threading.Event()

    def fetch(url: str, **kwargs: Any) -> Page:
        downloading.set()
        assert release.wait(5)
        return _page(url)

    def check() -> None:
        checking.set()
        if cancelled.is_set():
            raise TaskCancelled("task stopped")

    monkeypatch.setattr("llm_cli.web.access.fetch", fetch)
    web = WebAccess("allow")
    url = "https://same.example/"
    with ThreadPoolExecutor(max_workers=2) as workers:
        owner = workers.submit(web.page, url, allowed=lambda *args: None)
        assert downloading.wait(5)
        waiter = workers.submit(
            web.page, url, allowed=lambda *args: None, check_cancelled=check
        )
        try:
            assert checking.wait(5)
            cancelled.set()
            with pytest.raises(TaskCancelled):
                waiter.result(timeout=1)
            assert not owner.done()
        finally:
            release.set()
        assert owner.result(timeout=5) == (_page(url), True)


def test_coalesced_waiter_has_a_bounded_timeout_and_owner_can_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloading = threading.Event()
    release = threading.Event()

    def fetch(url: str, **kwargs: Any) -> Page:
        downloading.set()
        assert release.wait(5)
        return _page(url)

    monkeypatch.setattr("llm_cli.web.access.fetch", fetch)
    web = WebAccess("allow", timeout=0.02)
    url = "https://same.example/"
    with ThreadPoolExecutor(max_workers=1) as workers:
        owner = workers.submit(web.page, url, allowed=lambda *args: None)
        assert downloading.wait(5)
        try:
            with pytest.raises(WebFetchError, match="waiting for the page"):
                web.page(url, allowed=lambda *args: None)
            assert not owner.done()
        finally:
            release.set()
        assert owner.result(timeout=5) == (_page(url), True)
    assert web.page(url, allowed=lambda *args: None) == (_page(url), False)


def test_failed_fetch_releases_waiters_and_can_be_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloading = threading.Event()
    release = threading.Event()
    checking = threading.Event()
    downloads: list[str] = []

    def fetch(url: str, **kwargs: Any) -> Page:
        downloads.append(url)
        if len(downloads) == 1:
            downloading.set()
            assert release.wait(5)
            raise WebFetchError("refused")
        return _page(url)

    monkeypatch.setattr("llm_cli.web.access.fetch", fetch)
    web = WebAccess("allow")
    url = "https://same.example/"
    with ThreadPoolExecutor(max_workers=2) as workers:
        owner = workers.submit(web.page, url, allowed=lambda *args: None)
        assert downloading.wait(5)
        with web._lock:
            future = web._in_flight[url]
        original_result = future.result

        def joined(*args: Any, **kwargs: Any) -> Page:
            checking.set()
            return original_result(*args, **kwargs)

        monkeypatch.setattr(future, "result", joined)
        waiter = workers.submit(web.page, url, allowed=lambda *args: None)
        assert checking.wait(1)
        release.set()
        for pending in (owner, waiter):
            with pytest.raises(WebFetchError, match="refused"):
                pending.result(timeout=5)
    assert web.page(url, allowed=lambda *args: None) == (_page(url), True)
    assert downloads == [url, url]
