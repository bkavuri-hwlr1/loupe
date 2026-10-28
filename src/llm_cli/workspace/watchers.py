"""Native file-change wake-ups for checkout scans.

FSEvents (macOS) and inotify (Linux) are reached through ``ctypes``, with no
new dependency. A watcher never decides what changed. It records which paths
may have changed since a scanner's cursor; the scanner still stats, hashes and
compares exact identities, and publication still checks exact bases on disk.

When a watcher cannot vouch for a cursor, the next scan is a full one, exactly
as without watchers. That covers a first scan, daemon downtime, a kernel or
user-space queue overflow, a dropped FSEvents batch, a replaced root, a watcher
restart, an ignore-policy change and an unconfirmed delivery. Lost events also
call ``on_loss`` so the daemon can tell sessions to resynchronize. Unsupported
platforms and filesystems never start a watcher, and every cursor still gets a
full scan at least every ``FULL_RESCAN_SECONDS``.

One backend serves the whole process: a single inotify instance with a reader
thread, or one FSEvents dispatch queue with a stream per watched root. A
registry's watches end when it closes or is garbage collected.
"""

from __future__ import annotations

import bisect
import ctypes
import ctypes.util
import errno
import logging
import os
import select
import stat
import struct
import sys
import threading
import time
import unicodedata
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm_cli.git.environment import run_git

_LOG = logging.getLogger(__name__)

# Distinct possibly-changed paths kept per root before narrowing collapses into
# one full rescan. A large checkout or rebase is cheaper to rescan than to track.
MAX_CHANGED_PATHS = 8_192
# Even a healthy watcher is verified by a full scan at least this often.
FULL_RESCAN_SECONDS = 300.0
SETTLE_SECONDS = 2.0
UNSUPPORTED_RETRY_SECONDS = 300.0
MAX_WATCHED_DIRECTORIES = 50_000

FILE = 1
DIRECTORY = 2
# The entry was created, removed or renamed, not only modified.
STRUCTURAL = 4

_ADMIN_INDEX = ("index", "index.lock")
_ADMIN_POLICY = {("config",), ("config.worktree",), ("info", "exclude")}


def key(path: str) -> str:
    """Compare spellings conservatively: an extra rescan is cheap, a miss is not."""
    return unicodedata.normalize("NFD", path).casefold()


# Journal identity, last sequence covered, and when the last full scan began.
Cursor = tuple[str, int, float]


@dataclass(frozen=True, slots=True)
class Changes:
    """What may have changed since a cursor; ``full`` names a full-rescan cause."""

    cursor: Cursor
    full: str | None = None
    # Spelling keys of possibly changed entries, with FILE, DIRECTORY and
    # STRUCTURAL bits.
    paths: dict[str, int] = field(default_factory=dict)
    # Something was created, removed or renamed, so the source set may differ.
    structural: bool = False
    # The Git index changed, so the source set may differ.
    index: bool = False
    # Ignored directories whose churn is not recorded.
    unwatched: frozenset[str] = frozenset()
    # The latest reported spelling of each key in ``paths``.
    spellings: dict[str, str] = field(default_factory=dict)

    def touches(self, relative: str) -> bool:
        """Whether a scan must check this path (or one of its parents changed)."""
        if self.full is not None:
            return True
        candidate = key(relative)
        if candidate in self.paths:
            return True
        position = candidate.find("/")
        while position >= 0:
            prefix = candidate[:position]
            if self.paths.get(prefix, 0) & DIRECTORY or prefix in self.unwatched:
                return True
            position = candidate.find("/", position + 1)
        return False

    def affected(self, keys: list[str]) -> set[int]:
        """Positions in sorted ``keys`` that this change may touch."""
        if self.full is not None:
            return set(range(len(keys)))
        result: set[int] = set()
        for changed, kinds in self.paths.items():
            position = bisect.bisect_left(keys, changed)
            while position < len(keys) and keys[position] == changed:
                result.add(position)
                position += 1
            if kinds & DIRECTORY:
                result.update(_prefixed(keys, changed))
        for prefix in self.unwatched:
            result.update(_prefixed(keys, prefix))
        return result


def _prefixed(keys: list[str], prefix: str) -> range:
    start = bisect.bisect_left(keys, prefix + "/")
    # "0" sorts directly after "/", so this bounds every descendant.
    return range(start, bisect.bisect_left(keys, prefix + "0", start))


class _Unsupported(Exception):
    """This root keeps today's periodic scans."""


class _Journal:
    """Possibly-changed paths for one root, in sequence order."""

    def __init__(self) -> None:
        self.id = uuid.uuid4().hex
        self.sequence = 1
        self.floor = 1
        self.reason = "watcher started"
        self.entries: OrderedDict[str, tuple[int, int, str]] = OrderedDict()
        self.structural = 0
        self.index = 0

    def record(self, spelling: str, kind: int, structural: bool) -> None:
        self.sequence += 1
        path = key(spelling)
        previous = self.entries.pop(path, None)
        bits = kind | (STRUCTURAL if structural else 0)
        self.entries[path] = (
            self.sequence,
            bits | (previous[1] if previous else 0),
            spelling,
        )
        if structural:
            self.structural = self.sequence
        if len(self.entries) > MAX_CHANGED_PATHS:
            self.collapse("too many changed paths to track")

    def touch_index(self) -> None:
        self.sequence += 1
        self.index = self.sequence

    def collapse(self, reason: str) -> None:
        self.sequence += 1
        self.floor = self.sequence
        self.reason = reason
        self.entries.clear()

    def since(
        self, cursor: Cursor | None, unwatched: frozenset[str], now: float
    ) -> Changes:
        if cursor is None or cursor[0] != self.id or cursor[1] < self.floor:
            reason = self.reason
            if cursor is None:
                reason = "first scan"
            elif cursor[0] != self.id:
                reason = "watcher restarted"
            return Changes((self.id, self.sequence, now), reason)
        if now - cursor[2] >= FULL_RESCAN_SECONDS:
            return Changes((self.id, self.sequence, now), "periodic verification")
        paths: dict[str, int] = {}
        spellings: dict[str, str] = {}
        for path, (sequence, kind, spelling) in reversed(self.entries.items()):
            if sequence <= cursor[1]:
                break
            paths[path] = kind
            spellings[path] = spelling
        return Changes(
            (self.id, self.sequence, cursor[2]),
            None,
            paths,
            self.structural > cursor[1],
            self.index > cursor[1],
            unwatched,
            spellings,
        )


@dataclass(eq=False)
class _Root:
    owner: weakref.ref[WatchRegistry]
    path: Path
    real: str
    identity: tuple[int, int]
    admin: str | None
    common: str | None
    unwatched: frozenset[str]
    cookie_dir: str | None = None
    journal: _Journal = field(default_factory=_Journal)
    rebuild: str | None = None
    handle: Any = None
    cookies: set[str] = field(default_factory=set)
    cookie_count: int = 0


def _canonical(path: Path) -> str:
    """The on-disk spelling FSEvents reports, including case on macOS."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if sys.platform == "darwin":
            import fcntl

            raw = fcntl.fcntl(descriptor, fcntl.F_GETPATH, bytes(1024))
            return os.fsdecode(raw.split(b"\0", 1)[0])
        return str(path)
    finally:
        os.close(descriptor)


def _filesystem_supported(path: Path) -> bool:
    if sys.platform == "darwin":
        return _darwin_filesystem(path) in {"apfs", "hfs"}
    if sys.platform.startswith("linux"):
        return _linux_filesystem(path) in _LINUX_LOCAL
    return False


# ext2/3/4, XFS, Btrfs, tmpfs and overlayfs report every local write to inotify.
_LINUX_LOCAL = {0xEF53, 0x58465342, 0x9123683E, 0x01021994, 0x794C7630}


def _linux_filesystem(path: Path) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(256)
    if libc.statfs(os.fsencode(path), buffer) != 0:
        return -1
    # f_type is the first field, a native long on every supported Linux ABI.
    return int(struct.unpack_from("l", buffer.raw)[0]) & 0xFFFFFFFF


def _darwin_filesystem(path: Path) -> str:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        function = libc["statfs$INODE64"]
    except AttributeError:
        function = libc.statfs
    buffer = ctypes.create_string_buffer(4096)
    if function(os.fsencode(path), buffer) != 0:
        return ""
    # uint32 bsize, int32 iosize, 5 x uint64 counts, fsid (2 x int32), owner,
    # type, flags, subtype (4 x uint32), then the 16-byte type name.
    offset = 4 + 4 + 5 * 8 + 8 + 4 * 4
    return buffer.raw[offset : offset + 16].split(b"\0", 1)[0].decode()


def _git_paths(root: Path) -> tuple[str, str]:
    result = run_git(
        root,
        ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
        check=False,
        remaining=lambda: 10.0,
    )
    lines = str(result.stdout).splitlines() if result.returncode == 0 else []
    if len(lines) != 2:
        raise _Unsupported("Git administration could not be located")
    return _canonical(Path(lines[0])), _canonical(Path(lines[1]))


def _ignored_directories(root: Path) -> frozenset[str]:
    """Untracked ignored directories hold no source; their churn is not watched.

    Git lists a directory here only when nothing inside it is tracked. A scan
    treats every path below one of these as possibly changed.
    """
    result = run_git(
        root,
        [
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
        ],
        check=False,
        text=False,
        remaining=lambda: 10.0,
    )
    if result.returncode != 0:
        return frozenset()
    assert isinstance(result.stdout, bytes)
    return frozenset(
        key(os.fsdecode(entry[:-1]))
        for entry in result.stdout.split(b"\0")
        if entry.endswith(b"/") and len(entry) > 1
    )


def _identity(path: Path) -> tuple[int, int]:
    status = os.stat(path)
    return status.st_dev, status.st_ino


def _private_directory(path: Path) -> str:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    status = os.lstat(path)
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.getuid()
        or status.st_mode & 0o077
    ):
        raise _Unsupported("watch cookie directory must be private")
    return _canonical(path)


def _release(roots: dict[Path, _Root], lock: threading.Lock) -> None:
    """Stop a collected registry's watches without blocking its collector."""
    with lock:
        states = list(roots.values())
        roots.clear()
    backend = _BACKEND
    if not states or backend is None:
        return

    def remove() -> None:
        for state in states:
            backend.remove(state)

    # Interpreter shutdown refuses new threads; the process ends them anyway.
    with suppress(RuntimeError):
        threading.Thread(target=remove, name="loupe-watch-release", daemon=True).start()


class WatchRegistry:
    """Watch checkout roots on demand and answer narrowed-change queries.

    No lock here is held while calling into FSEvents or while another
    component's lock is held; callbacks run on watcher threads and only record.
    """

    def __init__(
        self,
        mode: str = "auto",
        *,
        cookie_dir: Path | None = None,
        on_loss: Callable[[Path, str], None] | None = None,
        on_change: Callable[[Path], None] | None = None,
    ) -> None:
        if mode not in {"auto", "off"}:
            raise ValueError("workspace watchers must be auto or off")
        self.mode = mode
        self.cookie_dir = cookie_dir
        self.on_loss = on_loss
        self.on_change = on_change
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._roots: dict[Path, _Root] = {}
        self._unsupported: dict[Path, tuple[float, str]] = {}
        self._closed = False
        weakref.finalize(self, _release, self._roots, self._lock)

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def changes(self, root: Path, cursor: Cursor | None) -> Changes | None:
        """Return narrowed changes, or None when this root keeps periodic scans."""
        if self.mode == "off" or self._closed:
            return None
        root = root.absolute()
        rebuilt: str | None = None
        for _attempt in range(3):
            with self._lock:
                state = self._roots.get(root)
                refused = self._unsupported.get(root)
            if refused is not None and time.monotonic() < refused[0]:
                return None
            if state is not None and state.rebuild is not None:
                rebuilt = rebuilt or state.rebuild
                self._stop(root, state)
                state = None
            if state is None:
                state = self._start(root)
                if state is None:
                    return None
            backend = _BACKEND
            try:
                settled = backend is not None and backend.settle(state)
                current = _identity(root)
            except OSError:
                settled, current = False, (-1, -1)
            if current != state.identity:
                self._lose(
                    state, "the watched checkout directory was replaced", rebuild=True
                )
                continue
            with self._lock:
                if state.rebuild is not None:
                    # Settling delivered a policy change or loss; rebuild now
                    # so one full scan, not two, follows it.
                    continue
                now = time.monotonic()
                result = state.journal.since(cursor, state.unwatched, now)
                if rebuilt is not None:
                    return Changes(result.cursor, rebuilt)
                if not settled and result.full is None:
                    # Undelivered events may exist; never narrow on them.
                    return Changes(
                        (state.journal.id, state.journal.sequence, now),
                        "file events were not confirmed in time",
                    )
                return result
        return Changes(("", 0, time.monotonic()), rebuilt or "file watching restarted")

    def watching(self) -> list[Path]:
        with self._lock:
            return list(self._roots)

    def retain(self, roots: Iterable[Path]) -> None:
        """Stop watching roots that no active session uses any more."""
        keep = {root.absolute() for root in roots}
        with self._lock:
            idle = [
                (path, state) for path, state in self._roots.items() if path not in keep
            ]
            for path in list(self._unsupported):
                if path not in keep:
                    self._unsupported.pop(path)
        for path, state in idle:
            self._stop(path, state)

    def close(self) -> None:
        self._closed = True
        with self._lock:
            roots = list(self._roots.items())
        for path, state in roots:
            self._stop(path, state)

    def inject_loss(self, root: Path, reason: str = "test overflow") -> None:
        """Simulate lost events, exactly as a backend reports an overflow."""
        with self._lock:
            state = self._roots.get(root.absolute())
        if state is not None:
            self._lose(state, reason)

    # Backend callbacks. They record only; scanners do every check.

    def _record(
        self, state: _Root, entries: Iterable[tuple[str, str, int, bool]]
    ) -> None:
        changed = False
        with self._lock:
            if self._roots.get(state.path) is not state:
                return
            for area, relative, kind, structural in entries:
                classified = _classify(state, area, relative, structural)
                if classified == "source":
                    state.journal.record(relative, kind, structural)
                    changed = True
                elif classified == "index":
                    state.journal.touch_index()
                    changed = True
                elif classified == "policy":
                    # Ignore rules decide both the source set and the unwatched
                    # directories; rebuild both before narrowing again.
                    state.journal.collapse("source ignore policy changed")
                    state.rebuild = "source ignore policy changed"
                    changed = True
        if changed and self.on_change is not None:
            self.on_change(state.path)

    def _lose(self, state: _Root, reason: str, *, rebuild: bool = False) -> None:
        with self._lock:
            if self._roots.get(state.path) is not state:
                return
            state.journal.collapse(reason)
            if rebuild:
                state.rebuild = reason
        _LOG.warning("file watching lost events under %s: %s", state.path, reason)
        if self.on_loss is not None:
            self.on_loss(state.path, reason)

    def _collapse(self, state: _Root, reason: str, *, rebuild: bool = False) -> None:
        with self._lock:
            if self._roots.get(state.path) is not state:
                return
            state.journal.collapse(reason)
            if rebuild:
                state.rebuild = reason

    def _start(self, root: Path) -> _Root | None:
        with self._start_lock:
            with self._lock:
                existing = self._roots.get(root)
            if existing is not None:
                return existing
            state: _Root | None = None
            try:
                backend = _shared_backend()
                if not _filesystem_supported(root):
                    raise _Unsupported("filesystem does not report local changes")
                admin, common = _git_paths(root)
                state = _Root(
                    weakref.ref(self),
                    root,
                    _canonical(root),
                    _identity(root),
                    admin,
                    common,
                    _ignored_directories(root),
                )
                if isinstance(backend, _FSEvents):
                    if self.cookie_dir is None:
                        raise _Unsupported("no private cookie directory")
                    state.cookie_dir = _private_directory(self.cookie_dir)
                with self._lock:
                    self._roots[root] = state
                backend.add(state)
                if not backend.settle(state):
                    raise _Unsupported("file events were not delivered")
            except (_Unsupported, OSError) as exc:
                if state is not None:
                    with self._lock:
                        if self._roots.get(root) is state:
                            self._roots.pop(root)
                    if _BACKEND is not None:
                        _BACKEND.remove(state)
                reason = str(exc) or type(exc).__name__
                _LOG.info("keeping periodic scans for %s: %s", root, reason)
                with self._lock:
                    self._unsupported[root] = (
                        time.monotonic() + UNSUPPORTED_RETRY_SECONDS,
                        reason,
                    )
                return None
            return state

    def _stop(self, root: Path, state: _Root) -> None:
        with self._lock:
            if self._roots.get(root) is state:
                self._roots.pop(root)
        if _BACKEND is not None:
            _BACKEND.remove(state)


def _classify(state: _Root, area: str, relative: str, structural: bool) -> str:
    """Source paths narrow scans; Git administration only marks policy or index."""
    parts = tuple(part for part in relative.split("/") if part)
    if area == "tree":
        if not parts:
            return "ignore"
        if parts[0] != ".git":
            if parts[-1] == ".gitignore":
                return "policy"
            folded = key(relative)
            position = folded.find("/")
            while position >= 0:
                if folded[:position] in state.unwatched:
                    return "ignore"
                position = folded.find("/", position + 1)
            return "ignore" if folded in state.unwatched else "source"
        if len(parts) == 1:
            # The repository link itself was created, removed or replaced.
            return "policy" if structural else "git"
        parts = parts[1:]
    if not parts:
        return "git"
    if area in {"tree", "admin"} and (
        parts[0] in _ADMIN_INDEX or parts[0].startswith("sharedindex.")
    ):
        return "index"
    if parts in _ADMIN_POLICY:
        return "policy"
    return "git"


def _owner(state: _Root) -> WatchRegistry | None:
    return state.owner()


_BACKEND_LOCK = threading.Lock()
_BACKEND: _Backend | None = None
_BACKEND_FAILURE: tuple[float, str] | None = None


def _shared_backend() -> _Backend:
    global _BACKEND, _BACKEND_FAILURE
    with _BACKEND_LOCK:
        if _BACKEND is not None:
            return _BACKEND
        if _BACKEND_FAILURE is not None and time.monotonic() < _BACKEND_FAILURE[0]:
            raise _Unsupported(_BACKEND_FAILURE[1])
        try:
            if sys.platform == "darwin":
                _BACKEND = _FSEvents()
            elif sys.platform.startswith("linux"):
                _BACKEND = _Inotify()
            else:
                raise _Unsupported("no native watcher on this platform")
        except (_Unsupported, OSError, AttributeError) as exc:
            reason = str(exc) or type(exc).__name__
            _BACKEND_FAILURE = (time.monotonic() + UNSUPPORTED_RETRY_SECONDS, reason)
            raise _Unsupported(reason) from exc
        return _BACKEND


class _Backend:
    def add(self, state: _Root) -> None:
        raise NotImplementedError

    def remove(self, state: _Root) -> None:
        raise NotImplementedError

    def settle(self, state: _Root) -> bool:
        raise NotImplementedError


# FSEvents ------------------------------------------------------------------

_FS_MUST_SCAN = 0x01
_FS_USER_DROPPED = 0x02
_FS_KERNEL_DROPPED = 0x04
_FS_IDS_WRAPPED = 0x08
_FS_ROOT_CHANGED = 0x20
_FS_MOUNT = 0x40
_FS_UNMOUNT = 0x80
_FS_CREATED = 0x100
_FS_REMOVED = 0x200
_FS_RENAMED = 0x800
_FS_IS_FILE = 0x10000
_FS_IS_DIR = 0x20000
_FS_IS_SYMLINK = 0x40000
_FS_LOSS = _FS_MUST_SCAN | _FS_USER_DROPPED | _FS_KERNEL_DROPPED | _FS_IDS_WRAPPED
_FS_CREATE_NO_DEFER = 0x02
_FS_CREATE_WATCH_ROOT = 0x04
_FS_CREATE_FILE_EVENTS = 0x10
_FS_SINCE_NOW = 0xFFFFFFFFFFFFFFFF
_CF_UTF8 = 0x08000100


class _FSContext(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_long),
        ("info", ctypes.c_void_p),
        ("retain", ctypes.c_void_p),
        ("release", ctypes.c_void_p),
        ("description", ctypes.c_void_p),
    ]


_FS_CALLBACK = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_char_p),
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(ctypes.c_uint64),
)


class _FSEvents(_Backend):
    """One dispatch queue; a stream per watched root, with a delivery cookie."""

    def __init__(self) -> None:
        services = ctypes.CDLL(ctypes.util.find_library("CoreServices"))
        foundation = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        system = ctypes.CDLL(None)
        foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
        foundation.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        foundation.CFArrayCreate.restype = ctypes.c_void_p
        foundation.CFArrayCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long,
            ctypes.c_void_p,
        ]
        foundation.CFRelease.argtypes = [ctypes.c_void_p]
        services.FSEventStreamCreate.restype = ctypes.c_void_p
        services.FSEventStreamCreate.argtypes = [
            ctypes.c_void_p,
            _FS_CALLBACK,
            ctypes.POINTER(_FSContext),
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_double,
            ctypes.c_uint32,
        ]
        for name in (
            "FSEventStreamFlushSync",
            "FSEventStreamStop",
            "FSEventStreamInvalidate",
            "FSEventStreamRelease",
        ):
            getattr(services, name).argtypes = [ctypes.c_void_p]
        services.FSEventStreamSetDispatchQueue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        services.FSEventStreamStart.restype = ctypes.c_bool
        services.FSEventStreamStart.argtypes = [ctypes.c_void_p]
        system.dispatch_queue_create.restype = ctypes.c_void_p
        system.dispatch_queue_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        self.services = services
        self.foundation = foundation
        self.array_callbacks = ctypes.c_void_p.in_dll(
            foundation, "kCFTypeArrayCallBacks"
        )
        # Lives as long as the process, like the callback CoreServices calls.
        self.queue = system.dispatch_queue_create(b"dev.loupe.watchers", None)
        if not self.queue:
            raise _Unsupported("could not create a watcher queue")
        self.condition = threading.Condition()
        # Serializes flushing with releasing a stream. Callbacks never take it,
        # so a release that waits for a running callback cannot deadlock.
        self.stream_lock = threading.Lock()
        self.streams: dict[int, _Root] = {}
        self.next_id = 1
        self.callback = _FS_CALLBACK(self._callback)

    def add(self, state: _Root) -> None:
        assert state.cookie_dir is not None
        paths = [state.real]
        for extra in (state.common, state.admin):
            if extra is not None and not any(
                extra == path or extra.startswith(path + "/") for path in paths
            ):
                paths.append(extra)
        paths.append(state.cookie_dir)
        strings = [
            self.foundation.CFStringCreateWithCString(None, os.fsencode(path), _CF_UTF8)
            for path in paths
        ]
        array = self.foundation.CFArrayCreate(
            None,
            (ctypes.c_void_p * len(strings))(*strings),
            len(strings),
            ctypes.addressof(self.array_callbacks),
        )
        for value in strings:
            self.foundation.CFRelease(value)
        with self.condition:
            stream_id = self.next_id
            self.next_id += 1
            self.streams[stream_id] = state
        context = _FSContext(0, stream_id, None, None, None)
        stream = self.services.FSEventStreamCreate(
            None,
            self.callback,
            ctypes.byref(context),
            array,
            _FS_SINCE_NOW,
            0.01,
            _FS_CREATE_FILE_EVENTS | _FS_CREATE_NO_DEFER | _FS_CREATE_WATCH_ROOT,
        )
        self.foundation.CFRelease(array)
        if not stream:
            with self.condition:
                self.streams.pop(stream_id, None)
            raise _Unsupported("could not create an FSEvents stream")
        self.services.FSEventStreamSetDispatchQueue(stream, self.queue)
        if not self.services.FSEventStreamStart(stream):
            self.services.FSEventStreamInvalidate(stream)
            self.services.FSEventStreamRelease(stream)
            with self.condition:
                self.streams.pop(stream_id, None)
            raise _Unsupported("could not start an FSEvents stream")
        with self.condition:
            state.handle = (stream_id, stream)

    def remove(self, state: _Root) -> None:
        with self.stream_lock:
            with self.condition:
                handle, state.handle = state.handle, None
            if handle is None:
                return
            stream_id, stream = handle
            self.services.FSEventStreamStop(stream)
            self.services.FSEventStreamInvalidate(stream)
            self.services.FSEventStreamRelease(stream)
        with self.condition:
            self.streams.pop(stream_id, None)

    def settle(self, state: _Root) -> bool:
        """Every event before a fresh cookie is delivered once the cookie is."""
        with self.condition:
            handle = state.handle
            if handle is None or state.cookie_dir is None:
                return False
            state.cookie_count += 1
            name = f"{handle[0]}-{os.getpid()}-{state.cookie_count}"
        path = os.path.join(state.cookie_dir, name)
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        os.close(descriptor)
        os.unlink(path)
        deadline = time.monotonic() + SETTLE_SECONDS
        while True:
            with self.stream_lock:
                if state.handle is not handle:
                    return False
                self.services.FSEventStreamFlushSync(handle[1])
            with self.condition:
                if name in state.cookies:
                    state.cookies.discard(name)
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(min(0.005, remaining))

    def _callback(
        self,
        stream: int,
        info: int | None,
        count: int,
        paths: Any,
        flags: Any,
        ids: Any,
    ) -> None:
        with self.condition:
            state = self.streams.get(int(info or 0))
        registry = _owner(state) if state is not None else None
        if state is None or registry is None:
            return
        try:
            self._events(registry, state, int(info or 0), count, paths, flags)
        except Exception:
            _LOG.exception("file watcher callback failed")
            with suppress(Exception):
                registry._lose(state, "file watcher callback failed")

    def _events(
        self,
        registry: WatchRegistry,
        state: _Root,
        stream_id: int,
        count: int,
        paths: Any,
        flags: Any,
    ) -> None:
        entries: list[tuple[str, str, int, bool]] = []
        loss: str | None = None
        unmapped = False
        replaced = False
        cookie_dir = state.cookie_dir or ""
        cookie_prefix = f"{cookie_dir}/{stream_id}-"
        for index in range(count):
            raw = paths[index]
            flag = int(flags[index])
            path = os.fsdecode(raw) if raw is not None else ""
            if path == cookie_dir or path.startswith(cookie_dir + "/"):
                # Streams can share a cookie directory; keep only this one's.
                if path.startswith(cookie_prefix):
                    with self.condition:
                        state.cookies.add(path[len(cookie_dir) + 1 :])
                        self.condition.notify_all()
                continue
            if flag & _FS_LOSS:
                loss = "file events were dropped"
                continue
            if flag & _FS_ROOT_CHANGED or flag & (_FS_MOUNT | _FS_UNMOUNT):
                replaced = True
                continue
            mapped = self._map(state, path)
            if mapped is None:
                unmapped = True
                continue
            area, relative = mapped
            if area == "tree" and not relative:
                if flag & (_FS_REMOVED | _FS_RENAMED):
                    replaced = True
                continue
            kind = (FILE if flag & (_FS_IS_FILE | _FS_IS_SYMLINK) else 0) | (
                DIRECTORY if flag & _FS_IS_DIR else 0
            )
            entries.append(
                (
                    area,
                    relative,
                    kind or FILE | DIRECTORY,
                    bool(flag & (_FS_CREATED | _FS_REMOVED | _FS_RENAMED)),
                )
            )
        if entries:
            registry._record(state, entries)
        if replaced:
            registry._lose(
                state, "the watched checkout directory changed", rebuild=True
            )
        elif loss is not None:
            registry._lose(state, loss)
        elif unmapped:
            # Not lost, just unattributable: rescan rather than guess a path.
            _LOG.warning("unmapped file event under %s", state.path)
            registry._collapse(state, "a file event could not be mapped")

    @staticmethod
    def _map(state: _Root, path: str) -> tuple[str, str] | None:
        for area, base in (
            ("tree", state.real),
            ("admin", state.admin),
            ("common", state.common),
        ):
            if base is None:
                continue
            for candidate, prefix in ((path, base), (key(path), key(base))):
                if candidate == prefix:
                    return area, ""
                if candidate.startswith(prefix + "/"):
                    return area, path[len(base) + 1 :]
        return None


# inotify --------------------------------------------------------------------

_IN_MODIFY = 0x2
_IN_ATTRIB = 0x4
_IN_CLOSE_WRITE = 0x8
_IN_MOVED_FROM = 0x40
_IN_MOVED_TO = 0x80
_IN_CREATE = 0x100
_IN_DELETE = 0x200
_IN_DELETE_SELF = 0x400
_IN_MOVE_SELF = 0x800
_IN_UNMOUNT = 0x2000
_IN_Q_OVERFLOW = 0x4000
_IN_IGNORED = 0x8000
_IN_ONLYDIR = 0x01000000
_IN_DONT_FOLLOW = 0x02000000
_IN_EXCL_UNLINK = 0x04000000
_IN_ISDIR = 0x40000000
_IN_NONBLOCK = os.O_NONBLOCK
_IN_CLOEXEC = 0o2000000
_IN_MASK = (
    _IN_MODIFY
    | _IN_ATTRIB
    | _IN_CLOSE_WRITE
    | _IN_MOVED_FROM
    | _IN_MOVED_TO
    | _IN_CREATE
    | _IN_DELETE
    | _IN_DELETE_SELF
    | _IN_MOVE_SELF
    | _IN_ONLYDIR
    | _IN_DONT_FOLLOW
    | _IN_EXCL_UNLINK
)
_IN_HEADER = struct.Struct("iIII")


class _Inotify(_Backend):
    """One inotify instance and reader thread for every watched root.

    The kernel keeps one watch per directory inode, so roots that share a
    directory share its descriptor; it is removed with its last user.
    """

    def __init__(self) -> None:
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_add_watch.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self.libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        descriptor = self.libc.inotify_init1(_IN_NONBLOCK | _IN_CLOEXEC)
        if descriptor < 0:
            raise _Unsupported(os.strerror(ctypes.get_errno()))
        self.fd = descriptor
        self.lock = threading.Lock()
        self.read_lock = threading.Lock()
        self.watches: dict[int, list[tuple[_Root, str, str]]] = {}
        self.thread = threading.Thread(
            target=self._run, name="loupe-inotify", daemon=True
        )
        self.thread.start()

    def add(self, state: _Root) -> None:
        with self.lock:
            state.handle = set()
        self._watch(state, "tree", "", Path(state.real), recursive=True)
        # Git administration: the index, config and info/exclude decide the
        # source set. Objects, refs and logs are deliberately not watched.
        targets: list[tuple[str, str]] = []
        if state.admin is not None:
            targets.append(("admin", state.admin))
        if state.common is not None:
            targets += [("common", state.common), ("common", state.common + "/info")]
        seen: set[str] = set()
        for area, path in targets:
            if path in seen:
                continue
            seen.add(path)
            if path == state.real or path.startswith(state.real + "/"):
                area, relative = "tree", os.path.relpath(path, state.real)
            else:
                base = state.admin if area == "admin" else state.common
                assert base is not None
                relative = os.path.relpath(path, base).removeprefix(".")
            self._watch(state, area, relative, Path(path), recursive=False)

    def _watch(
        self, state: _Root, area: str, relative: str, path: Path, *, recursive: bool
    ) -> None:
        pending = [(relative, path)]
        while pending:
            current, directory = pending.pop()
            descriptor = self.libc.inotify_add_watch(
                self.fd, os.fsencode(directory), _IN_MASK
            )
            if descriptor < 0:
                error = ctypes.get_errno()
                if error in {errno.ENOENT, errno.ENOTDIR, errno.EACCES}:
                    continue
                raise _Unsupported("inotify watch failed: " + os.strerror(error))
            with self.lock:
                handles: set[int] | None = state.handle
                users = self.watches.setdefault(descriptor, [])
                if handles is None:
                    # Removed concurrently; drop the watch just added.
                    if not users:
                        self.watches.pop(descriptor)
                        self.libc.inotify_rm_watch(self.fd, descriptor)
                    return
                if descriptor not in handles:
                    users.append((state, area, current))
                    handles.add(descriptor)
                if len(handles) > MAX_WATCHED_DIRECTORIES:
                    raise _Unsupported("too many directories to watch")
            if not recursive:
                continue
            try:
                children = list(os.scandir(directory))
            except OSError:
                continue
            for child in children:
                if not child.is_dir(follow_symlinks=False):
                    continue
                name = child.name
                nested = f"{current}/{name}" if current else name
                if (not current and name == ".git") or key(nested) in state.unwatched:
                    continue
                pending.append((nested, Path(child.path)))

    def remove(self, state: _Root) -> None:
        unused: list[int] = []
        with self.lock:
            handles, state.handle = state.handle, None
            for descriptor in handles or ():
                users = [
                    user
                    for user in self.watches.get(descriptor, [])
                    if user[0] is not state
                ]
                if users:
                    self.watches[descriptor] = users
                else:
                    self.watches.pop(descriptor, None)
                    unused.append(descriptor)
        for descriptor in unused:
            self.libc.inotify_rm_watch(self.fd, descriptor)

    def settle(self, state: _Root) -> bool:
        # inotify queues an event before the changing system call returns.
        with self.read_lock:
            self._drain()
        return state.handle is not None

    def _run(self) -> None:
        poller = select.poll()
        poller.register(self.fd, select.POLLIN)
        while True:
            try:
                poller.poll(1_000)
                with self.read_lock:
                    self._drain()
            except Exception:
                _LOG.exception("inotify reader failed")
                self._lose_all("file watcher reader failed")
                time.sleep(1)

    def _drain(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 1 << 16)
            except BlockingIOError:
                return
            except InterruptedError:
                continue
            if not data:
                return
            self._handle(data)

    def _lose_all(self, reason: str) -> None:
        with self.lock:
            states = {
                id(user[0]): user[0]
                for users in self.watches.values()
                for user in users
            }
        for state in states.values():
            registry = _owner(state)
            if registry is not None:
                registry._lose(state, reason, rebuild=True)

    def _handle(self, data: bytes) -> None:
        offset = 0
        batches: dict[int, tuple[_Root, list[tuple[str, str, int, bool]]]] = {}
        lost: list[tuple[_Root, str, bool]] = []
        overflow = False
        while offset + _IN_HEADER.size <= len(data):
            descriptor, mask, _cookie, length = _IN_HEADER.unpack_from(data, offset)
            raw = data[offset + _IN_HEADER.size : offset + _IN_HEADER.size + length]
            offset += _IN_HEADER.size + length
            if mask & _IN_Q_OVERFLOW:
                overflow = True
                continue
            with self.lock:
                users = list(self.watches.get(descriptor, ()))
                if mask & _IN_IGNORED:
                    self.watches.pop(descriptor, None)
                    for user in users:
                        if isinstance(user[0].handle, set):
                            user[0].handle.discard(descriptor)
            name = os.fsdecode(raw.split(b"\0", 1)[0])
            for state, area, directory in users:
                self._event(state, area, directory, name, mask, batches, lost)
        for state, entries in batches.values():
            registry = _owner(state)
            if registry is not None:
                registry._record(state, entries)
        for state, reason, notify in lost:
            registry = _owner(state)
            if registry is None:
                continue
            if notify:
                registry._lose(state, reason, rebuild=True)
            else:
                registry._collapse(state, reason, rebuild=True)
        if overflow:
            self._lose_all("the kernel file event queue overflowed")

    def _event(
        self,
        state: _Root,
        area: str,
        directory: str,
        name: str,
        mask: int,
        batches: dict[int, tuple[_Root, list[tuple[str, str, int, bool]]]],
        lost: list[tuple[_Root, str, bool]],
    ) -> None:
        root_watch = area == "tree" and not directory
        if mask & _IN_IGNORED:
            if root_watch:
                lost.append((state, "the watched checkout directory was removed", True))
            return
        if mask & (_IN_DELETE_SELF | _IN_MOVE_SELF | _IN_UNMOUNT):
            if root_watch:
                lost.append((state, "the watched checkout directory changed", True))
            return
        relative = f"{directory}/{name}" if directory and name else directory or name
        is_dir = bool(mask & _IN_ISDIR)
        source = area == "tree" and relative.split("/", 1)[0] != ".git"
        if is_dir and mask & (_IN_MOVED_FROM | _IN_MOVED_TO) and source:
            # Moved watches keep their old names. Rebuild them, and narrow
            # nothing until then.
            lost.append((state, "a watched directory was renamed", False))
            return
        if (
            is_dir
            and mask & _IN_CREATE
            and source
            and key(relative) not in state.unwatched
        ):
            # Files created before this watch exists are covered by marking
            # the whole new directory changed below.
            try:
                self._watch(
                    state, area, relative, Path(state.real) / relative, recursive=True
                )
            except _Unsupported as exc:
                lost.append((state, str(exc), True))
        structural = bool(
            mask & (_IN_CREATE | _IN_DELETE | _IN_MOVED_FROM | _IN_MOVED_TO)
        )
        batches.setdefault(id(state), (state, []))[1].append(
            (area, relative, DIRECTORY if is_dir else FILE, structural)
        )
