"""Unexpected optional-index errors remain visible without killing its worker."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from llm_cli.daemon.service import DaemonService


def _service(
    tmp_path: Path, service_factory: Callable[[Path], DaemonService]
) -> DaemonService:
    service = service_factory(tmp_path)
    service.paths.ensure()
    service.store.initialize()
    service.knowledge.enabled = True
    service.knowledge.initialize()
    return service


def test_unexpected_failure_is_sanitized_and_worker_recovers_on_next_pass(
    tmp_path: Path,
    service_factory: Callable[[Path], DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        service = _service(tmp_path, service_factory)
        index = service.knowledge
        process_pending = index.process_pending
        loop = asyncio.get_running_loop()
        recovered = asyncio.Event()
        starts: list[float] = []
        failures: list[dict[str, Any]] = []
        private_message = "private source text and credential should stay private"

        def flaky_process() -> int:
            starts.append(time.monotonic())
            if len(starts) == 1:
                raise TypeError(private_message)
            failures.append(index.status())
            result = process_pending()
            loop.call_soon_threadsafe(recovered.set)
            return result

        monkeypatch.setattr(index, "process_pending", flaky_process)
        worker = asyncio.create_task(service._knowledge_loop())
        try:
            await asyncio.wait_for(recovered.wait(), timeout=5)
            assert not worker.done()
            assert len(starts) == 2
            # A failed pass keeps the worker's usual one-second pacing.
            assert starts[1] - starts[0] >= 0.9
            assert failures[0]["state"] == "unavailable"
            assert failures[0]["error"] == (
                "optional lexical index unavailable (TypeError)"
            )
            assert private_message not in str(failures[0])
            assert index.status()["state"] == "ready"
            service.shutdown.set()
            await asyncio.wait_for(worker, timeout=1)
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["waiting", "processing"])
def test_worker_cancellation_remains_cancellation(
    tmp_path: Path,
    service_factory: Callable[[Path], DaemonService],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    async def scenario() -> None:
        service = _service(tmp_path, service_factory)
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()

        def blocking_process() -> int:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=5), "worker fixture was not released"
            return 0

        monkeypatch.setattr(service.knowledge, "process_pending", blocking_process)
        worker = asyncio.create_task(service._knowledge_loop())
        try:
            if phase == "processing":
                await asyncio.wait_for(started.wait(), timeout=3)
            else:
                await asyncio.sleep(0)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
            assert service.knowledge.status()["state"] == "ready"
            assert service.knowledge.error is None
        finally:
            release.set()
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            service.close()

    asyncio.run(scenario())
