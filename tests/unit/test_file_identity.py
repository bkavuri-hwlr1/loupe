"""Exact content identity for coordinated paths.

Every downstream guarantee -- divergence detection, optimistic publication,
the change ledger -- reduces to "is this path still what I thought it was".
These tests pin the distinctions that question depends on, especially the ones
a Git blob ID would erase.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from llm_cli.workspace.identity import (
    ABSENT,
    EXECUTABLE_MODE,
    REGULAR_MODE,
    FileIdentity,
    IdentityError,
    ObjectKind,
    content_identity,
    identify_path,
    read_identified_path,
)


def test_identical_content_has_one_identity(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_bytes(b"same bytes\n")
    second.write_bytes(b"same bytes\n")

    assert identify_path(first).digest == identify_path(second).digest


def test_different_content_differs(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"one\n")
    before = identify_path(path)
    path.write_bytes(b"two\n")

    assert identify_path(path).digest != before.digest


def test_the_executable_bit_changes_identity(tmp_path: Path) -> None:
    path = tmp_path / "script.sh"
    path.write_bytes(b"#!/bin/sh\n")
    plain = identify_path(path)
    path.chmod(0o755)
    executable = identify_path(path)

    # Same bytes, different mode: publishing one over the other is a real
    # change, so they must not share an identity.
    assert plain.mode == REGULAR_MODE
    assert executable.mode == EXECUTABLE_MODE
    assert plain.digest != executable.digest


def test_an_absent_path_has_a_stable_identity(tmp_path: Path) -> None:
    identity = identify_path(tmp_path / "never-existed")

    assert identity == ABSENT
    assert identity.absent
    assert identity.kind is ObjectKind.ABSENT


def test_a_symlink_is_identified_by_its_target_not_its_destination(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real.txt"
    target.write_bytes(b"contents\n")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    identity = identify_path(link)

    # Following the link would make a symlink and a copy indistinguishable,
    # and would make the identity change when something else was edited.
    assert identity.kind is ObjectKind.SYMLINK
    assert identity.digest != identify_path(target).digest


def test_a_symlink_that_is_repointed_changes_identity(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_bytes(b"one\n")
    (tmp_path / "two.txt").write_bytes(b"two\n")
    link = tmp_path / "link.txt"
    link.symlink_to(tmp_path / "one.txt")
    before = identify_path(link)
    link.unlink()
    link.symlink_to(tmp_path / "two.txt")

    assert identify_path(link).digest != before.digest


def test_a_directory_is_a_marker_not_its_contents(tmp_path: Path) -> None:
    directory = tmp_path / "pkg"
    directory.mkdir()
    empty = identify_path(directory)
    (directory / "file.txt").write_bytes(b"added\n")

    # A directory's identity does not summarize its children; children are
    # coordinated as their own paths.
    assert empty.kind is ObjectKind.DIRECTORY_MARKER
    assert identify_path(directory).digest == empty.digest


def test_a_nested_repository_is_a_gitlink(tmp_path: Path) -> None:
    nested = tmp_path / "vendor"
    (nested / ".git").mkdir(parents=True)

    identity = identify_path(nested)

    # Publishing into a nested repository would cross a repository boundary,
    # so it must not look like an ordinary directory.
    assert identity.kind is ObjectKind.GITLINK


def test_kind_and_mode_cannot_collide_with_content(tmp_path: Path) -> None:
    # The NUL separators and leading version exist so that no rearrangement of
    # the parts can produce another combination's digest.
    assert content_identity(ObjectKind.REGULAR, REGULAR_MODE, b"x") != (
        content_identity(ObjectKind.SYMLINK, REGULAR_MODE, b"x")
    )
    assert content_identity(ObjectKind.REGULAR, REGULAR_MODE, b"ab") != (
        content_identity(ObjectKind.REGULAR, REGULAR_MODE + "a", b"b")
    )


def test_an_absent_identity_cannot_carry_content() -> None:
    with pytest.raises(IdentityError):
        content_identity(ObjectKind.ABSENT, "", b"bytes")


def test_an_unsupported_file_type_is_refused(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    # A device or socket has no publishable content, and guessing one would be
    # worse than refusing to coordinate the path.
    with pytest.raises(IdentityError):
        identify_path(fifo)


def test_identity_records_the_observed_size(tmp_path: Path) -> None:
    path = tmp_path / "sized.txt"
    path.write_bytes(b"12345")

    identity = identify_path(path)

    assert isinstance(identity, FileIdentity)
    assert identity.size == 5


@pytest.mark.parametrize("body", [b"", b"at limit"])
def test_read_accepts_content_exactly_at_the_limit(tmp_path: Path, body: bytes) -> None:
    path = tmp_path / "bounded.txt"
    path.write_bytes(body)

    identity, content = read_identified_path(path, max_bytes=len(body))

    assert content == body
    assert identity.digest == content_identity(ObjectKind.REGULAR, REGULAR_MODE, body)
    assert identity.size == len(body)


def test_oversized_file_is_rejected_before_opening_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"12345")

    def unexpected_open(*args: object, **kwargs: object) -> int:
        pytest.fail("an oversized file must not be opened or read")

    monkeypatch.setattr(os, "open", unexpected_open)
    with pytest.raises(IdentityError, match="4-byte read limit"):
        read_identified_path(path, max_bytes=4)


def test_file_growth_during_read_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "growing.txt"
    path.write_bytes(b"tiny")
    original_read = os.read
    sizes: list[int] = []

    def grow_then_read(descriptor: int, size: int) -> bytes:
        path.write_bytes(b"x" * 100)
        sizes.append(size)
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", grow_then_read)
    with pytest.raises(IdentityError, match="8-byte read limit"):
        read_identified_path(path, max_bytes=8)
    # Even a file that grew after the initial size check can only supply one
    # overflow byte beyond the caller's budget, never an unbounded allocation.
    assert sum(sizes) == 9


def test_atomic_replacement_during_read_retries_with_the_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "replaced.txt"
    path.write_bytes(b"old body")
    original_read = os.read
    replaced = False

    def read_then_replace(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        content = original_read(descriptor, size)
        if not replaced:
            replacement = tmp_path / "editor-save.tmp"
            replacement.write_bytes(b"new body")
            replacement.chmod(0o755)
            replacement.replace(path)
            replaced = True
        return content

    monkeypatch.setattr(os, "read", read_then_replace)
    identity, content = read_identified_path(path)

    assert content == b"new body"
    assert identity.mode == EXECUTABLE_MODE
    assert identity.digest == content_identity(
        ObjectKind.REGULAR, EXECUTABLE_MODE, content
    )


def test_in_place_write_during_read_discards_the_old_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "edited.txt"
    path.write_bytes(b"old body")
    original_read = os.read
    edited = False

    def read_then_edit(descriptor: int, size: int) -> bytes:
        nonlocal edited
        content = original_read(descriptor, size)
        if not edited:
            before = path.stat()
            path.write_bytes(b"new body")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
            edited = True
        return content

    monkeypatch.setattr(os, "read", read_then_edit)
    identity, content = read_identified_path(path)

    assert content == b"new body"
    assert identity.digest == content_identity(
        ObjectKind.REGULAR, REGULAR_MODE, content
    )


def test_persistent_replacement_fails_closed_after_bounded_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "unstable.txt"
    path.write_bytes(b"old body")
    original_read = os.read
    replacements = 0

    def read_then_replace(descriptor: int, size: int) -> bytes:
        nonlocal replacements
        content = original_read(descriptor, size)
        if content:
            replacements += 1
            assert replacements <= 3, "retries must be bounded"
            replacement = tmp_path / "editor-save.tmp"
            replacement.write_bytes(b"new body")
            replacement.replace(path)
        return content

    monkeypatch.setattr(os, "read", read_then_replace)
    with pytest.raises(IdentityError, match="changed while being read"):
        read_identified_path(path)
    assert replacements == 3


def test_symlink_swapped_in_before_open_is_never_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "regular.txt"
    path.write_bytes(b"original body")
    destination = tmp_path / "private.txt"
    destination.write_bytes(b"must never read this")
    original_open = os.open

    def replace_then_open(target: Path, flags: int) -> int:
        assert target == path
        path.unlink()
        path.symlink_to(destination)
        return original_open(target, flags)

    def unexpected_read(descriptor: int, size: int) -> bytes:
        pytest.fail("a symlink destination must not be read")

    monkeypatch.setattr(os, "open", replace_then_open)
    monkeypatch.setattr(os, "read", unexpected_read)
    identity, content = read_identified_path(path)

    assert identity.kind is ObjectKind.SYMLINK
    assert content is None


def test_fifo_swapped_in_before_open_is_refused_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "regular.txt"
    path.write_bytes(b"original body")
    original_open = os.open

    def replace_then_open(target: Path, flags: int) -> int:
        assert target == path
        path.unlink()
        os.mkfifo(path)
        return original_open(target, flags)

    monkeypatch.setattr(os, "open", replace_then_open)
    with pytest.raises(IdentityError, match="neither a regular file"):
        read_identified_path(path)
