"""Native watchers only narrow scans; anything they cannot vouch for rescans."""

from __future__ import annotations

import ctypes
import gc
import os
import struct
import sys
import time
import unicodedata
import weakref
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from llm_cli.workspace import watchers
from llm_cli.workspace.watchers import (
    DIRECTORY,
    FILE,
    Changes,
    WatchRegistry,
    _Journal,
    key,
)

native = pytest.mark.skipif(
    sys.platform != "darwin" and not sys.platform.startswith("linux"),
    reason="FSEvents or inotify is required",
)


def test_journal_narrows_after_a_full_scan_and_collapses_past_its_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _Journal()
    first = journal.since(None, frozenset(), 0.0)
    assert first.full == "first scan"
    journal.record("src/a.py", FILE, False)
    narrowed = journal.since(first.cursor, frozenset(), 1.0)
    assert narrowed.full is None
    assert narrowed.paths == {"src/a.py": FILE}
    assert narrowed.spellings == {"src/a.py": "src/a.py"}
    assert not narrowed.structural and not narrowed.index
    assert journal.since(narrowed.cursor, frozenset(), 2.0).paths == {}
    monkeypatch.setattr(watchers, "MAX_CHANGED_PATHS", 3)
    for number in range(4):
        journal.record(f"f{number}", FILE, True)
    assert journal.since(narrowed.cursor, frozenset(), 3.0).full == (
        "too many changed paths to track"
    )


def test_every_cursor_gets_a_periodic_full_scan() -> None:
    journal = _Journal()
    cursor = journal.since(None, frozenset(), 0.0).cursor
    quiet = journal.since(cursor, frozenset(), watchers.FULL_RESCAN_SECONDS - 1)
    assert quiet.full is None
    # A narrowed answer keeps the time of the last full scan.
    assert quiet.cursor[2] == 0.0
    due = journal.since(quiet.cursor, frozenset(), watchers.FULL_RESCAN_SECONDS)
    assert due.full == "periodic verification"


def test_another_journal_or_a_collapse_never_narrows_an_old_cursor() -> None:
    journal = _Journal()
    cursor = journal.since(None, frozenset(), 0.0).cursor
    assert _Journal().since(cursor, frozenset(), 1.0).full == "watcher restarted"
    journal.collapse("the kernel file event queue overflowed")
    assert journal.since(cursor, frozenset(), 1.0).full == (
        "the kernel file event queue overflowed"
    )


def test_changes_cover_parents_spellings_and_unwatched_directories() -> None:
    nfd = unicodedata.normalize("NFD", "Café.md")
    changes = Changes(
        ("journal", 2, 0.0),
        None,
        {key("Src"): DIRECTORY, key("README.md"): FILE, key(nfd): FILE},
        unwatched=frozenset({"build"}),
    )
    assert changes.touches("src/deep/a.py")
    assert changes.touches("readme.md")
    assert changes.touches(unicodedata.normalize("NFC", "café.md"))
    assert changes.touches("build/out.txt")
    assert not changes.touches("docs/a.md")
    assert not changes.touches("src0.py")
    paths = ("README.md", "build/x", "docs/a.md", "src/a.py", "src/b/c.py", "src0.py")
    keys = sorted(key(path) for path in paths)
    assert {keys[index] for index in changes.affected(keys)} == {
        "readme.md",
        "build/x",
        "src/a.py",
        "src/b/c.py",
    }
    assert Changes(("journal", 2, 0.0), "first scan").affected(keys) == set(
        range(len(keys))
    )


@pytest.mark.parametrize(
    ("area", "relative", "structural", "expected"),
    [
        ("tree", "src/a.py", False, "source"),
        ("tree", ".git/index", False, "index"),
        ("tree", ".git/index.lock", True, "index"),
        ("tree", ".git/objects/ab/cdef", True, "git"),
        ("tree", ".git/llm-coord-stage/p.tmp", True, "git"),
        ("tree", ".git/info/exclude", False, "policy"),
        ("tree", ".git/config", False, "policy"),
        ("tree", "pkg/.gitignore", False, "policy"),
        ("tree", ".git", True, "policy"),
        ("tree", ".git", False, "git"),
        ("tree", "build/out.o", True, "ignore"),
        ("admin", "index", False, "index"),
        ("admin", "HEAD", False, "git"),
        ("admin", "config.worktree", False, "policy"),
        ("common", "index", False, "git"),
        ("common", "info/exclude", False, "policy"),
    ],
)
def test_git_administration_never_floods_source_paths(
    area: str, relative: str, structural: bool, expected: str
) -> None:
    registry = WatchRegistry("off")
    state = watchers._Root(
        weakref.ref(registry),
        Path("/r"),
        "/r",
        (0, 0),
        None,
        None,
        frozenset({"build"}),
    )
    assert watchers._classify(state, area, relative, structural) == expected


def test_off_mode_never_watches(tmp_path: Path) -> None:
    registry = WatchRegistry("off")
    assert not registry.enabled
    assert registry.changes(tmp_path, None) is None
    assert registry.watching() == []
    with pytest.raises(ValueError, match="auto or off"):
        WatchRegistry("on")


def test_unsupported_filesystem_keeps_periodic_scans(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "a\n"})
    monkeypatch.setattr(watchers, "_filesystem_supported", lambda path: False)
    registry = WatchRegistry(cookie_dir=tmp_path / "cookies")
    assert registry.changes(root, None) is None
    assert registry.watching() == []


class Watched:
    def __init__(self, root: Path, registry: WatchRegistry, losses: list[str]) -> None:
        self.root = root
        self.registry = registry
        self.losses = losses
        first = registry.changes(root, None)
        assert first is not None, "native file watching should start here"
        assert first.full == "first scan"
        self.cursor = first.cursor

    def next(self) -> Changes:
        changes = self.registry.changes(self.root, self.cursor)
        assert changes is not None
        self.cursor = changes.cursor
        return changes


@pytest.fixture
def watched(
    tmp_path: Path, repository_factory: Callable[[Path, dict[str, str]], Path]
) -> Iterator[Watched]:
    root = repository_factory(
        tmp_path,
        {
            "src/a.py": "a\n",
            "src/b.py": "b\n",
            ".gitignore": "build/\n",
            "build/cache.o": "cache\n",
        },
    )
    losses: list[str] = []
    registry = WatchRegistry(
        cookie_dir=tmp_path / "cookies",
        on_loss=lambda root, reason: losses.append(reason),
    )
    try:
        yield Watched(root.resolve(), registry, losses)
    finally:
        registry.close()


@native
def test_quiet_checkout_and_in_place_edit_are_narrowed(watched: Watched) -> None:
    quiet = watched.next()
    assert quiet.full is None and quiet.paths == {}
    (watched.root / "src/a.py").write_text("edited\n")
    changes = watched.next()
    assert changes.full is None
    assert changes.touches("src/a.py")
    assert not changes.touches("src/b.py")


@native
def test_editor_atomic_save_names_its_target(watched: Watched) -> None:
    watched.next()
    temporary = watched.root / "src" / ".a.py.swp"
    temporary.write_text("saved\n")
    os.replace(temporary, watched.root / "src/a.py")
    changes = watched.next()
    assert changes.full is None
    assert changes.touches("src/a.py") and changes.structural
    assert not changes.touches("src/b.py")


@native
def test_ignored_directory_churn_is_not_recorded(watched: Watched) -> None:
    watched.next()
    for number in range(50):
        (watched.root / "build" / f"out{number}.o").write_text("x")
    changes = watched.next()
    assert changes.full is None
    assert not any(path.startswith("build") for path in changes.paths)
    # Reads below an unwatched directory are still always checked.
    assert changes.touches("build/out0.o")


@native
def test_git_index_change_is_flagged_without_flooding_paths(
    watched: Watched, git_run: Callable[..., str]
) -> None:
    watched.next()
    git_run(watched.root, "rm", "--cached", "--quiet", "src/b.py")
    changes = watched.next()
    assert changes.full is None
    assert changes.index
    assert not any(path.startswith(".git") for path in changes.paths)


@native
def test_renamed_directory_is_never_narrowed_to_its_old_names(
    watched: Watched,
) -> None:
    watched.next()
    os.rename(watched.root / "src", watched.root / "lib")
    changes = watched.next()
    # FSEvents names both directories; inotify rebuilds its moved watches.
    assert changes.full is not None or (
        changes.touches("lib/a.py") and changes.touches("src/a.py")
    )
    (watched.root / "lib/a.py").write_text("after rename\n")
    later = watched.next()
    assert later.full is not None or later.touches("lib/a.py")


@native
def test_ignore_policy_change_forces_a_full_scan_then_narrows_again(
    watched: Watched,
) -> None:
    watched.next()
    (watched.root / ".gitignore").write_text("build/\n*.log\n")
    assert watched.next().full is not None
    assert watched.next().full is None
    assert watched.losses == []


@native
def test_lost_events_force_a_full_scan_and_report_the_loss(watched: Watched) -> None:
    watched.next()
    watched.registry.inject_loss(watched.root, "forced overflow")
    assert watched.next().full == "forced overflow"
    assert watched.losses == ["forced overflow"]
    assert watched.next().full is None


@native
def test_backend_overflow_signal_is_a_loss(watched: Watched) -> None:
    watched.next()
    backend = watchers._BACKEND
    state = watched.registry._roots[watched.root]
    if sys.platform == "darwin":
        assert isinstance(backend, watchers._FSEvents)
        paths = (ctypes.c_char_p * 1)(os.fsencode(watched.root / "src"))
        flags = (ctypes.c_uint32 * 1)(watchers._FS_KERNEL_DROPPED)
        backend._events(watched.registry, state, state.handle[0], 1, paths, flags)
        expected = "file events were dropped"
    else:
        assert isinstance(backend, watchers._Inotify)
        backend._handle(struct.pack("iIII", -1, watchers._IN_Q_OVERFLOW, 0, 0))
        expected = "the kernel file event queue overflowed"
    assert watched.losses == [expected]
    assert watched.next().full is not None


@native
def test_replaced_root_forces_a_full_scan(
    watched: Watched, git_run: Callable[..., str]
) -> None:
    watched.next()
    os.rename(watched.root, watched.root.with_name("moved-away"))
    watched.root.mkdir()
    git_run(watched.root, "init", "--quiet")
    changes = watched.next()
    assert changes.full is not None
    assert watched.losses


@native
def test_forked_child_keeps_periodic_scans_and_leaves_parent_watches(
    watched: Watched,
) -> None:
    watched.next()
    read, write = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - the child reports through the pipe
        try:
            result = watched.registry.changes(watched.root, watched.cursor)
            watched.registry.close()
            os.write(write, b"none" if result is None else b"watched")
        finally:
            os._exit(0)
    os.close(write)
    try:
        assert os.read(read, 16) == b"none"
    finally:
        os.close(read)
        os.waitpid(child, 0)
    (watched.root / "src/b.py").write_text("after fork\n")
    changes = watched.next()
    assert changes.full is None and changes.touches("src/b.py")


@native
def test_collected_registry_releases_its_watches(
    tmp_path: Path, repository_factory: Callable[[Path, dict[str, str]], Path]
) -> None:
    root = repository_factory(tmp_path, {"a.py": "a\n"}).resolve()
    registry = WatchRegistry(cookie_dir=tmp_path / "cookies")
    assert registry.changes(root, None) is not None
    state = weakref.ref(registry._roots[root])
    del registry
    gc.collect()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = state()
        if current is None or current.handle is None:
            break
        time.sleep(0.05)
    current = state()
    assert current is None or current.handle is None
