"""Streaming ignore inspection keeps deadlines, pipe bounds and child cleanup."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from llm_cli.errors import LlmCoordError
from llm_cli.workspace import hydration


@pytest.fixture
def checkout(tmp_path: Path, repository_factory: Callable[..., Path]) -> Path:
    return repository_factory(
        tmp_path, {".gitignore": "runtime/\n", "app.py": "pass\n"}
    )


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    original = hydration.subprocess.Popen
    started: list[Any] = []

    def recorded(*args: Any, **kwargs: Any) -> Any:
        child = original(*args, **kwargs)
        started.append(child)
        return child

    monkeypatch.setattr(hydration.subprocess, "Popen", recorded)
    return started


def test_streaming_partial_writes_empty_chunks_final_eof_and_exit_one(
    checkout: Path, children: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.write
    writes: list[int] = []

    def partial(fd: int, data: bytes) -> int:
        writes.append(len(data))
        return original(fd, data[:3])

    monkeypatch.setattr(os, "write", partial)
    received: list[bytes] = []
    code, output = hydration._bounded_git(
        checkout,
        ["check-ignore", "-z", "--stdin"],
        deadline=time.monotonic() + 3,
        limit=128 * 1024,
        input_chunks=[b"", b"app.py\x00", b"", b"missing.py\x00", b""],
        output_chunk=received.append,
    )
    assert code == 1
    assert output == b"" and received == []
    assert len(writes) >= 6
    assert len(children) == 1 and children[0].poll() == 1
    assert children[0].stdin.closed


def test_streaming_reads_output_while_writing_large_input(
    checkout: Path, children: list[Any]
) -> None:
    expected = {f"runtime/{number:06}.bin" for number in range(5000)}
    held = peak = 0

    def account(amount: int) -> None:
        nonlocal held, peak
        held += amount
        peak = max(peak, held)
        assert 0 <= held <= 4 * 1024 * 1024

    ignored = hydration._ignored_paths(
        checkout,
        iter(expected),
        includes=expected.__contains__,
        account=account,
        deadline=time.monotonic() + 3,
        limit=4 * 1024 * 1024,
    )
    assert ignored == expected
    assert held == sum(len(path.encode()) + 256 for path in expected)
    assert peak > held
    assert len(children) == 1 and children[0].poll() == 0


def test_streaming_deadline_reaps_child(checkout: Path, children: list[Any]) -> None:
    with pytest.raises(hydration._BudgetExceeded, match="time_budget_exceeded"):
        hydration._bounded_git(
            checkout,
            ["check-ignore", "-z", "--stdin"],
            deadline=time.monotonic() - 1,
            limit=4096,
            input_chunks=[b"app.py\x00"],
            output_chunk=lambda chunk: None,
        )
    assert len(children) == 1 and children[0].poll() is not None
    assert all(stream.closed for stream in (children[0].stdin, children[0].stdout))


def test_streaming_output_flood_reaps_child(
    checkout: Path, children: list[Any], git_run: Callable[..., str]
) -> None:
    git_run(checkout, "config", "test.large", "x" * 20_000)
    children.clear()
    received: list[bytes] = []
    with pytest.raises(hydration._BudgetExceeded, match="git_metadata_budget_exceeded"):
        hydration._bounded_git(
            checkout,
            ["config", "--get", "test.large"],
            deadline=time.monotonic() + 3,
            limit=128,
            input_chunks=[],
            output_chunk=received.append,
        )
    assert sum(map(len, received)) <= 128
    assert len(children) == 1 and children[0].poll() is not None


def test_streaming_matched_paths_share_the_inventory_metadata_cap(
    checkout: Path, children: list[Any]
) -> None:
    expected = {f"runtime/{number:06}.bin" for number in range(1000)}
    held = 0

    def account(amount: int) -> None:
        nonlocal held
        held += amount
        if amount > 0 and held > 128 * 1024:
            raise hydration._BudgetExceeded("metadata_budget_exceeded")

    with pytest.raises(hydration._BudgetExceeded, match="metadata_budget_exceeded"):
        hydration._ignored_paths(
            checkout,
            iter(expected),
            includes=expected.__contains__,
            account=account,
            deadline=time.monotonic() + 3,
            limit=4 * 1024 * 1024,
        )
    assert held == 0
    assert len(children) == 1 and children[0].poll() is not None


def test_streaming_oversized_input_reaps_child(
    checkout: Path, children: list[Any]
) -> None:
    with pytest.raises(hydration._BudgetExceeded, match="git_metadata_budget_exceeded"):
        hydration._ignored_paths(
            checkout,
            ["x" * (256 * 1024)],
            deadline=time.monotonic() + 3,
            limit=4 * 1024 * 1024,
        )
    assert len(children) == 1 and children[0].poll() is not None


@pytest.mark.parametrize("raw", [b"runtime/unterminated", b"unknown\x00"])
def test_streaming_rejects_truncated_or_unrequested_output(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    def invalid(*args: Any, **kwargs: Any) -> tuple[int, bytes]:
        kwargs["output_chunk"](raw)
        return 0, b""

    monkeypatch.setattr(hydration, "_bounded_git", invalid)
    with pytest.raises(LlmCoordError, match=r"ignore metadata|ignored runtime paths"):
        hydration._ignored_paths(
            checkout,
            ["runtime/expected"],
            deadline=time.monotonic() + 3,
            limit=4096,
        )


def test_streaming_broken_input_never_claims_complete_classification(
    checkout: Path, children: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def closed(fd: int, data: bytes) -> int:
        raise BrokenPipeError

    monkeypatch.setattr(os, "write", closed)
    with pytest.raises(LlmCoordError, match="complete metadata input"):
        hydration._ignored_paths(
            checkout,
            ["runtime/expected"],
            deadline=time.monotonic() + 3,
            limit=4096,
        )
    assert len(children) == 1 and children[0].poll() is not None
