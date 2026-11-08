"""Private cleanup excludes only provably too-short stable regular leaves."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_cli.agent.source_policy import (
    block_source_private_values,
    forget_source_private_values,
    register_source_private_values,
)
from llm_cli.errors import LlmCoordError
from llm_cli.workspace import process_privacy

_LONG = "private violet maple fixture 483526"


@pytest.fixture
def root(tmp_path: Path):
    register_source_private_values(tmp_path, (_LONG,))
    try:
        yield tmp_path
    finally:
        forget_source_private_values(tmp_path)


def test_stable_short_source_avoids_body_reads_but_private_copy_is_removed(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "ordinary.py").write_bytes(b"pass\n")
    (root / "private.py").write_text(_LONG)
    original = process_privacy.read_candidate_path

    def read(path: Path, **kwargs):
        assert path.name != "ordinary.py", "safe short source needs no body read"
        return original(path, **kwargs)

    monkeypatch.setattr(process_privacy, "read_candidate_path", read)
    assert process_privacy.scrub_source_copies(root) == {"private.py"}
    assert (root / "ordinary.py").read_bytes() == b"pass\n"
    assert not (root / "private.py").exists()


@pytest.mark.parametrize(
    ("value", "body"),
    [("q", b"q"), ("qZ6!", b"qZ6!\n"), ("\ufffd", b"\xff"), ("é", "é".encode())],
)
def test_all_value_lengths_and_replacement_decoding_remain_screened(
    root: Path, value: str, body: bytes
) -> None:
    register_source_private_values(root, (_LONG, value))
    (root / "private.bin").write_bytes(body)
    assert process_privacy.scrub_source_copies(root) == {"private.bin"}
    assert not (root / "private.bin").exists()


def test_leaf_that_grows_after_short_metadata_is_read_and_scrubbed(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaf = root / "growing.py"
    leaf.write_text("x")
    original = os.fstat
    changed = False

    def fstat(descriptor: int):
        nonlocal changed
        if not changed:
            changed = True
            leaf.write_text(_LONG)
        return original(descriptor)

    monkeypatch.setattr(process_privacy.os, "fstat", fstat)
    assert process_privacy.scrub_source_copies(root) == {"growing.py"}
    assert changed and not leaf.exists()


def test_replacement_symlink_never_reads_its_referent(
    root: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaf = root / "replaced.py"
    leaf.write_text("x")
    outside = tmp_path_factory.mktemp("outside-private") / "source.py"
    outside.write_text(_LONG)
    original = os.open
    changed = False

    def open_path(path, flags, *args, **kwargs):
        nonlocal changed
        if Path(path) == leaf and not changed:
            changed = True
            leaf.unlink()
            leaf.symlink_to(outside)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(process_privacy.os, "open", open_path)
    assert process_privacy.scrub_source_copies(root) == set()
    assert changed and leaf.is_symlink()
    assert outside.read_text() == _LONG


def test_new_short_private_value_during_sweep_prevents_retirement(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "new-value.py").write_text("q")
    original = process_privacy._short_regular_size

    def short(path: Path, minimum: int):
        result = original(path, minimum)
        register_source_private_values(root, ("q",))
        return result

    monkeypatch.setattr(process_privacy, "_short_regular_size", short)
    with pytest.raises(LlmCoordError, match="private source was preserved"):
        process_privacy.scrub_source_copies(root)
    assert (root / "new-value.py").exists()


def test_blocked_guard_uses_the_existing_full_read_path(root: Path) -> None:
    (root / "short.py").write_text("x")
    block_source_private_values(root)
    assert process_privacy.scrub_source_copies(root) == {"short.py"}


def test_short_files_still_count_toward_the_configured_limit(root: Path) -> None:
    (root / "one.py").write_text("x")
    (root / "two.py").write_text("x")
    with pytest.raises(LlmCoordError, match=r"hydration_max_entries=1"):
        process_privacy.scrub_source_copies(root, max_paths=1)


def test_final_short_leaf_cannot_exceed_the_sweep_deadline(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "ordinary.py").write_text("x")
    ticks = iter((0.0, 1.0, 6.0))
    monkeypatch.setattr(
        process_privacy, "time", SimpleNamespace(monotonic=lambda: next(ticks))
    )
    with pytest.raises(LlmCoordError, match="private source was preserved"):
        process_privacy.scrub_source_copies(root)


def test_short_exclusions_charge_actual_sparse_file_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    size = 2 * 1024 * 1024
    register_source_private_values(tmp_path, ("z" * (size + 1),))
    try:
        for number in range(513):
            with (tmp_path / f"{number}.bin").open("wb") as stream:
                stream.truncate(size)

        def unexpected_read(*args, **kwargs):
            raise AssertionError("all sparse leaves are provably too short")

        monkeypatch.setattr(process_privacy, "read_candidate_path", unexpected_read)
        with pytest.raises(LlmCoordError, match="private source was preserved"):
            process_privacy.scrub_source_copies(tmp_path)
        assert (tmp_path / "0.bin").stat().st_size == size
    finally:
        forget_source_private_values(tmp_path)
