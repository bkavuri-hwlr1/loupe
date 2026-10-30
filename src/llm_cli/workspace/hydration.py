"""Bounded, metadata-only preview of ignored runtime hydration.

This module never opens runtime file contents, follows descendant symlinks,
copies bytes, creates a worktree, or persists consent. A plan is diagnostic
evidence for the operator, not authorization or an executable snapshot. A
private bounded catalogue holds path metadata only during the preview and is
removed before it returns.
"""

from __future__ import annotations

import hashlib
import math
import os
import posixpath
import re
import selectors
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from llm_cli.config.models import (
    DEFAULT_ISOLATED_HYDRATION_MAX_ENTRIES,
    MAX_HYDRATION_ENTRIES,
    RebuildConfig,
)
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import sanitized_git_environment
from llm_cli.workspace.hydration_catalogue import CatalogueLimit, PathCatalogue

_ADMIN_NAMES = frozenset({".git", ".loupe", ".llm-coord", ".codex", ".agents"})
_SECRET_DIRECTORIES = frozenset({".ssh", ".aws", ".azure", ".gnupg"})
_SECRET_NAMES = frozenset(
    {
        ".env",
        ".envrc",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "credentials",
        "credentials.json",
        "auth.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
_COPY_ACTIONS = frozenset({"clone_or_copy", "private_copy", "private_tree"})
_CHECK_INPUT_BYTES = 256 * 1024
_PIPE_BYTES = 8192


@dataclass(frozen=True, slots=True)
class HydrationBudgets:
    """Limits apply to the entire inventory, including ordinary source paths."""

    max_entries: int = DEFAULT_ISOLATED_HYDRATION_MAX_ENTRIES
    max_metadata_bytes: int = 4 * 1024 * 1024
    max_depth: int = 64
    max_file_bytes: int = 256 * 1024 * 1024
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_allocated_bytes: int = 2 * 1024 * 1024 * 1024
    max_seconds: float = 10.0

    def __post_init__(self) -> None:
        ceilings = {
            "max_entries": MAX_HYDRATION_ENTRIES,
            "max_metadata_bytes": 16 * 1024 * 1024,
            "max_depth": 128,
            "max_file_bytes": 1024**4,
            "max_total_bytes": 1024**4,
            "max_allocated_bytes": 1024**4,
        }
        for name, ceiling in ceilings.items():
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= ceiling:
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID, f"{name} must be between 1 and {ceiling}"
                )
        if (
            isinstance(self.max_seconds, bool)
            or not math.isfinite(self.max_seconds)
            or not 0 < self.max_seconds <= 60
        ):
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID, "max_seconds must be positive and at most 60"
            )


@dataclass(frozen=True, slots=True)
class HydrationEntry:
    path: str
    kind: str
    classification: str
    sensitivity: str
    action: str
    reason: str
    apparent_bytes: int
    allocated_bytes: int
    estimated_copy_bytes: int
    source_device: int
    requires_decision: bool
    descendants_inventoried: bool | None = None
    directory_entries: int | None = None
    destination_mode: str | None = None
    ignore_provenance: str | None = None
    observation: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HydrationPlan:
    checkout: str
    entries: tuple[HydrationEntry, ...]
    complete: bool
    issues: tuple[str, ...]
    budgets: HydrationBudgets
    scanned_entries: int
    inventory_metadata_bytes: int
    planned_apparent_bytes: int
    planned_estimated_copy_bytes: int
    requires_decision: bool
    preview_only: bool = True
    cow_capability: str = "unprobed"
    destination_device: int | None = None
    freshness: str = "metadata_preview_only_rescan_before_materialization"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _BudgetExceeded(Exception):
    pass


def _bounded_git(
    root: Path,
    arguments: Sequence[str],
    *,
    deadline: float,
    limit: int,
    input_data: bytes = b"",
    input_chunks: Iterable[bytes | bytearray] | None = None,
    output_chunk: Callable[[bytes], None] | None = None,
) -> tuple[int, bytes]:
    """Bound pipe storage as well as Git runtime; never run a hook or filter.

    Global configuration is enabled only for these passive metadata commands
    so actual global ignore rules participate. No arbitrary Git command can be
    supplied by callers of the public planner.
    """
    env = sanitized_git_environment()
    if "GIT_CONFIG_GLOBAL" in os.environ:
        env["GIT_CONFIG_GLOBAL"] = os.environ["GIT_CONFIG_GLOBAL"]
    else:
        env.pop("GIT_CONFIG_GLOBAL", None)
    command = [
        "git",
        "--no-pager",
        "--no-replace-objects",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.allow=never",
        "-c",
        "submodule.recurse=false",
        *arguments,
    ]
    output = bytearray()
    total = 0
    sent = 0
    chunks = iter(input_chunks if input_chunks is not None else (input_data,))
    pending: bytes | bytearray = b""

    def advance() -> bytes | bytearray:
        for chunk in chunks:
            if time.monotonic() >= deadline:
                raise _BudgetExceeded("time_budget_exceeded")
            if chunk:
                return chunk
        return b""

    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE, "Git ignore inspection could not run"
        ) from exc
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        pending = advance()
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            if pending:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _BudgetExceeded("time_budget_exceeded")
                for key, _ in selector.select(remaining):
                    selected_stream = key.fileobj
                    if selected_stream is process.stdin:
                        try:
                            sent += os.write(
                                process.stdin.fileno(),
                                pending[sent : sent + _PIPE_BYTES],
                            )
                        except (BlockingIOError, InterruptedError):
                            continue
                        except BrokenPipeError as exc:
                            raise LlmCoordError(
                                ErrorCode.REPOSITORY_UNSAFE,
                                "Git did not consume its complete metadata input",
                            ) from exc
                        if sent == len(pending):
                            pending = b""
                            pending = advance()
                            sent = 0
                            if not pending:
                                selector.unregister(process.stdin)
                                process.stdin.close()
                        continue
                    descriptor = key.fd
                    read_limit = _PIPE_BYTES if output_chunk is not None else 65_536
                    try:
                        chunk = os.read(descriptor, min(read_limit, limit - total + 1))
                    except (BlockingIOError, InterruptedError):
                        continue
                    if not chunk:
                        selector.unregister(selected_stream)
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise _BudgetExceeded("git_metadata_budget_exceeded")
                    if selected_stream is process.stdout:
                        if output_chunk is None:
                            output.extend(chunk)
                        else:
                            output_chunk(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _BudgetExceeded("time_budget_exceeded")
            return process.wait(timeout=remaining), bytes(output)
    except subprocess.TimeoutExpired as exc:
        raise _BudgetExceeded("time_budget_exceeded") from exc
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        close_input = getattr(chunks, "close", None)
        if close_input is not None:
            close_input()


def _ignored_paths(
    root: Path,
    paths: Iterable[str],
    *,
    deadline: float,
    limit: int,
    includes: Callable[[str], bool] | None = None,
    account: Callable[[int], None] | None = None,
    no_index: bool = False,
) -> set[str]:
    """Classify one bounded stream without repeatedly loading Git's index.

    Only the active stdin chunk, an incomplete stdout record and matched paths
    stay live. The inventory accounts those against its shared metadata budget.
    """
    if includes is None:
        expected = set(paths)
        paths = expected
        includes = expected.__contains__

    def charge(amount: int) -> None:
        if account is not None:
            account(amount)

    def inputs() -> Iterable[bytes | bytearray]:
        buffer = bytearray()
        held = 0
        try:
            for path in paths:
                if time.monotonic() >= deadline:
                    raise _BudgetExceeded("time_budget_exceeded")
                encoded = os.fsencode(path) + b"\x00"
                if len(encoded) > min(limit, _CHECK_INPUT_BYTES):
                    raise _BudgetExceeded("git_metadata_budget_exceeded")
                # A decoded catalogue row and its encoding can remain live
                # while a previous full pipe buffer is yielded to the child.
                row_cost = len(encoded) * 2 + 256
                try:
                    charge(row_cost)
                    if buffer and len(buffer) + len(encoded) > _PIPE_BYTES:
                        yield buffer
                        buffer = bytearray()
                        charge(-held)
                        held = 0
                    held += len(encoded)
                    charge(len(encoded))
                    buffer.extend(encoded)
                finally:
                    charge(-row_cost)
            if buffer:
                yield buffer
        finally:
            charge(-held)

    ignored: set[str] = set()
    tail = bytearray()
    tail_cost = result_cost = 0
    completed = False

    def receive(chunk: bytes) -> None:
        nonlocal tail_cost, result_cost
        tail_cost += len(chunk)
        charge(len(chunk))
        tail.extend(chunk)
        while (end := tail.find(b"\x00")) >= 0:
            decoding_cost = (end + 1) * 2 + 256
            try:
                charge(decoding_cost)
                path = tail[:end].decode(
                    sys.getfilesystemencoding(), errors="surrogateescape"
                )
                if not path or not includes(path):
                    raise LlmCoordError(
                        ErrorCode.REPOSITORY_UNSAFE,
                        "Git returned unexpected ignore metadata",
                    )
                if path not in ignored:
                    needed = len(os.fsencode(path)) + 256
                    result_cost += needed
                    charge(needed)
                    ignored.add(path)
                del tail[: end + 1]
                tail_cost -= end + 1
                charge(-(end + 1))
            finally:
                charge(-decoding_cost)

    # Reserve read/write slices and bytearray allocation slack in addition to
    # accounted stdin/stdout contents. No complete raw stdout stream is retained.
    pipe_cost = _PIPE_BYTES * 4
    try:
        charge(pipe_cost)
        code, _ = _bounded_git(
            root,
            ["check-ignore", *(["--no-index"] if no_index else []), "-z", "--stdin"],
            deadline=deadline,
            limit=limit,
            input_chunks=inputs(),
            output_chunk=receive,
        )
        if code not in {0, 1} or tail:
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                "Git could not classify ignored runtime paths",
            )
        completed = True
        return ignored
    finally:
        charge(-pipe_cost - tail_cost)
        if not completed:
            charge(-result_cost)


def _evidence(status: os.stat_result) -> dict[str, int]:
    return {
        "device": status.st_dev,
        "inode": status.st_ino,
        "mode": status.st_mode,
        "size": status.st_size,
        "mtime_ns": status.st_mtime_ns,
        "ctime_ns": status.st_ctime_ns,
    }


def _kind(mode: int) -> str:
    for predicate, name in (
        (stat.S_ISREG, "regular"),
        (stat.S_ISDIR, "directory"),
        (stat.S_ISLNK, "symlink"),
        (stat.S_ISFIFO, "fifo"),
        (stat.S_ISSOCK, "socket"),
        (stat.S_ISCHR, "character_device"),
        (stat.S_ISBLK, "block_device"),
    ):
        if predicate(mode):
            return name
    return "special"


def _likely_secret(path: str) -> bool:
    for name in (part.casefold() for part in PurePosixPath(path).parts):
        if name.endswith((".example", ".sample", ".template")):
            continue
        if (
            name in _SECRET_NAMES
            or name in _SECRET_DIRECTORIES
            or name.startswith(".env.")
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
            or (name.startswith("service-account") and name.endswith(".json"))
        ):
            return True
    return False


@dataclass(slots=True)
class _Observed:
    path: str
    status: os.stat_result
    refusal: str | None = None
    reason: str = ""
    descendants_inventoried: bool | None = None
    directory_entries: int | None = None


class _Inventory:
    def __init__(
        self,
        root: Path,
        budgets: HydrationBudgets,
        deadline: float,
        rebuild: frozenset[str] = frozenset(),
    ) -> None:
        self.root = root
        self.rebuild = rebuild
        self.budgets = budgets
        self.deadline = deadline
        self.entries: list[_Observed] = []
        self.issues: list[str] = []
        self.scanned = 0
        self.metadata_bytes = 0
        self.retained_metadata_bytes = 0
        self.temporary_metadata_bytes = 0
        self.retained: dict[str, _Observed] = {}
        self.catalogue: PathCatalogue | None = None
        self.deferred_ignored_paths: set[str] = set()
        self.index_fingerprint: str | None = None

    def metadata(self) -> None:
        allocated = self.catalogue.allocated_bytes if self.catalogue is not None else 0
        current = (
            self.retained_metadata_bytes + self.temporary_metadata_bytes + allocated
        )
        self.metadata_bytes = max(self.metadata_bytes, current)
        if current > self.budgets.max_metadata_bytes:
            raise _BudgetExceeded("metadata_budget_exceeded")

    def retain(self, entry: _Observed) -> None:
        if entry.path not in self.retained:
            self.retained_metadata_bytes += len(entry.path.encode("utf-8")) + 256
            self.metadata()
            self.entries.append(entry)
            self.retained[entry.path] = entry

    def observe(self, entry: _Observed, *, administration: bool) -> None:
        try:
            if self.catalogue is None:
                self.catalogue = PathCatalogue(self.budgets.max_metadata_bytes)
            previous_path = self.catalogue.add(
                entry.path,
                administration=administration,
                directory=stat.S_ISDIR(entry.status.st_mode),
            )
        except CatalogueLimit:
            raise _BudgetExceeded("metadata_budget_exceeded") from None
        self.metadata()
        if previous_path is not None:
            previous = self.retained.get(previous_path)
            for collision in (entry, previous):
                if collision is not None:
                    collision.refusal = "refuse_path_alias"
                    collision.reason = "case or Unicode destination alias"
            self.issues.append("path_alias_requires_decision")

    def ignored_paths(self) -> set[str]:
        """Stream classifications through one child with bounded live metadata."""
        if self.catalogue is None:
            return set()

        def account(amount: int) -> None:
            self.temporary_metadata_bytes += amount
            if amount > 0:
                self.metadata()

        try:
            self.catalogue.reset_tracked()
        except CatalogueLimit:
            raise _BudgetExceeded("metadata_budget_exceeded") from None
        tail = bytearray()
        tail_cost = 0
        fingerprint = hashlib.sha256()

        def receive(chunk: bytes) -> None:
            nonlocal tail_cost
            fingerprint.update(chunk)
            tail_cost += len(chunk)
            account(len(chunk))
            tail.extend(chunk)
            while (end := tail.find(b"\x00")) >= 0:
                row_cost = (end + 1) * 2 + 256
                try:
                    account(row_cost)
                    record = tail[:end].decode(
                        sys.getfilesystemencoding(), errors="surrogateescape"
                    )
                    prefix, separator, path = record.partition("\t")
                    if not separator or not path or not re.fullmatch(
                        r"[0-7]{6} (?:[0-9a-f]{40}|[0-9a-f]{64}) [0-3]", prefix
                    ):
                        raise LlmCoordError(
                            ErrorCode.REPOSITORY_UNSAFE,
                            "Git returned invalid tracked path metadata",
                        )
                    assert self.catalogue is not None
                    try:
                        self.catalogue.mark_tracked(path)
                    except CatalogueLimit:
                        raise _BudgetExceeded("metadata_budget_exceeded") from None
                    self.metadata()
                    del tail[: end + 1]
                    tail_cost -= end + 1
                    account(-(end + 1))
                finally:
                    account(-row_cost)

        pipe_cost = _PIPE_BYTES * 4
        try:
            account(pipe_cost)
            code, _ = _bounded_git(
                self.root,
                ["ls-files", "--cached", "--stage", "-z"],
                deadline=self.deadline,
                limit=self.budgets.max_metadata_bytes,
                output_chunk=receive,
            )
            if code or tail:
                raise LlmCoordError(
                    ErrorCode.REPOSITORY_UNSAFE,
                    "Git could not inspect tracked path metadata",
                )
        finally:
            account(-pipe_cost - tail_cost)
        observed_index = fingerprint.hexdigest()
        if (
            self.index_fingerprint is not None
            and observed_index != self.index_fingerprint
        ):
            self.issues.append("tracked_index_changed")
        self.index_fingerprint = observed_index
        ignored = _ignored_paths(
            self.root,
            self.catalogue.untracked(),
            deadline=self.deadline,
            limit=self.budgets.max_metadata_bytes,
            includes=self.catalogue.includes,
            account=account,
            no_index=True,
        )
        ignored.update(
            _ignored_paths(
                self.root,
                self.catalogue.ambiguous(),
                deadline=self.deadline,
                limit=self.budgets.max_metadata_bytes,
                includes=self.catalogue.includes,
                account=account,
            )
        )
        return ignored

    def promote(self, root: int, ignored: set[str]) -> None:
        """Retain stat receipts only after a bounded full-path classification.

        Every entry was observed without following links during the walk. A
        second no-follow observation pins an ignored regular file's current
        receipt; a changed kind or an unstable receipt makes the plan incomplete.
        """
        if self.catalogue is None:
            return
        for path in sorted(ignored | set(self.catalogue.collisions())):
            self.remaining()
            if path in self.retained:
                continue
            before = _safe_status(root, path)
            if before is None:
                self.issues.append(
                    "entry_changed_or_unreadable_during_inventory:" + path
                )
                continue
            entry = _Observed(path, before)
            if not stat.S_ISREG(before.st_mode):
                entry.refusal = "needs_rescan"
                entry.reason = "entry kind changed during metadata observation"
                entry.descendants_inventoried = (
                    False if stat.S_ISDIR(before.st_mode) else None
                )
                self.issues.append("entry_changed_during_inventory")
            elif self.catalogue.collided(path):
                entry.refusal = "refuse_path_alias"
                entry.reason = "case or Unicode destination alias"
            self.retain(entry)
            after = _safe_status(root, path)
            if after is None or _evidence(before) != _evidence(after):
                entry.refusal = "needs_rescan"
                entry.reason = "entry changed during metadata observation"
                self.issues.append("entry_changed_during_inventory")

    def close(self) -> None:
        if self.catalogue is not None:
            self.catalogue.close()
            self.catalogue = None

    def remaining(self) -> None:
        if time.monotonic() >= self.deadline:
            raise _BudgetExceeded("time_budget_exceeded")

    def names(self, descriptor: int, prefix: str) -> list[str]:
        names: list[str] = []
        with os.scandir(descriptor) as directory:
            for entry in directory:
                self.remaining()
                if self.scanned + len(names) >= self.budgets.max_entries:
                    raise _BudgetExceeded("entry_budget_exceeded")
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                try:
                    encoded = relative.encode("utf-8")
                except UnicodeError as exc:
                    raise _BudgetExceeded("unsupported_path_encoding_budget") from exc
                self.temporary_metadata_bytes += len(encoded) + 256
                self.metadata()
                names.append(entry.name)
        self.scanned += len(names)
        return sorted(names)

    def walk(self, descriptor: int, prefix: str = "", depth: int = 0) -> int | None:
        self.remaining()
        before = _evidence(os.fstat(descriptor))
        child_count: int | None = None
        previous_temporary = self.temporary_metadata_bytes
        try:
            names = self.names(descriptor, prefix)
            child_count = len(names)
            for name in names:
                self.remaining()
                relative = f"{prefix}/{name}" if prefix else name
                entry: _Observed | None = None
                try:
                    status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    entry = _Observed(relative, status)
                    self.observe(
                        entry,
                        administration=name.casefold() in _ADMIN_NAMES,
                    )
                    if (
                        not stat.S_ISREG(status.st_mode)
                        or name in {".gitignore", ".gitattributes"}
                        or entry.refusal is not None
                    ):
                        self.retain(entry)
                    if name.casefold() in _ADMIN_NAMES:
                        entry.refusal = "exclude_administration"
                        entry.reason = (
                            "Git or coordinator administration is never hydrated"
                        )
                        entry.descendants_inventoried = (
                            False if stat.S_ISDIR(status.st_mode) else None
                        )
                    elif relative in self.rebuild:
                        entry.refusal = "rebuild"
                        entry.reason = (
                            "explicit private installer; primary dependency "
                            "tree is not copied"
                        )
                        entry.descendants_inventoried = False
                    elif stat.S_ISLNK(status.st_mode):
                        entry.refusal = "refuse_symlink"
                        target = os.readlink(name, dir_fd=descriptor)
                        destination = posixpath.normpath(posixpath.join(prefix, target))
                        if target.startswith("/"):
                            entry.reason = (
                                "absolute symlink is refused without following "
                                "its target"
                            )
                        elif destination == ".." or destination.startswith("../"):
                            entry.reason = "symlink escapes the checkout and is refused"
                        else:
                            entry.reason = (
                                "contained symlink recreation is deferred; "
                                "target was not followed"
                            )
                    elif stat.S_ISDIR(status.st_mode) and entry.refusal is None:
                        self.directory(descriptor, name, entry, depth)
                    elif not stat.S_ISREG(status.st_mode) and entry.refusal is None:
                        entry.refusal = "refuse_special"
                        entry.reason = "special files cannot be safely hydrated"
                    after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if _evidence(status) != _evidence(after):
                        self.retain(entry)
                        entry.refusal = "needs_rescan"
                        entry.reason = "entry changed during metadata observation"
                        self.issues.append("entry_changed_during_inventory")
                except OSError:
                    if entry is not None:
                        self.retain(entry)
                        entry.refusal = "needs_rescan"
                        entry.reason = "entry changed or could not be safely observed"
                        if stat.S_ISDIR(entry.status.st_mode):
                            entry.descendants_inventoried = False
                    self.issues.append(
                        f"entry_changed_or_unreadable_during_inventory:{relative}"
                    )
        except _BudgetExceeded as exc:
            self.issues.append(str(exc))
        finally:
            # Parent name lists stay reserved across recursion. This directory's
            # names are released when its real list is no longer needed.
            self.temporary_metadata_bytes = previous_temporary
            if before != _evidence(os.fstat(descriptor)):
                self.issues.append("directory_changed_during_inventory")
        return child_count

    def directory(self, parent: int, name: str, entry: _Observed, depth: int) -> None:
        entry.descendants_inventoried = False
        if depth >= self.budgets.max_depth:
            entry.refusal = "needs_rescan"
            entry.reason = "depth budget exceeded"
            self.issues.append("depth_budget_exceeded")
            return
        child = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        try:
            if _evidence(os.fstat(child)) != _evidence(entry.status):
                raise OSError("directory changed")
            if entry.status.st_dev != os.fstat(parent).st_dev:
                entry.refusal = "refuse_mount"
                entry.reason = (
                    "cross-device runtime directories need an explicit adapter"
                )
            elif self.is_repository(child):
                entry.refusal = "refuse_nested_repository"
                entry.reason = (
                    "nested Git repository/worktree is not runtime; "
                    "no adapter is enabled"
                )
            elif name.casefold() in {
                ".venv",
                "venv",
                "node_modules",
            } and entry.path in _ignored_paths(
                self.root,
                [entry.path],
                deadline=self.deadline,
                limit=self.budgets.max_metadata_bytes,
            ):
                code, tracked = _bounded_git(
                    self.root,
                    ["ls-files", "-z", "--", entry.path],
                    deadline=self.deadline,
                    limit=self.budgets.max_metadata_bytes,
                )
                if code or tracked:
                    initial_issues = len(self.issues)
                    entry.directory_entries = self.walk(child, entry.path, depth + 1)
                    entry.descendants_inventoried = len(self.issues) == initial_issues
                    return
                self.deferred_ignored_paths.add(entry.path)
                entry.refusal = "private_tree"
                entry.reason = (
                    "private dependency-tree adapter; descendants checked while "
                    "copying, contained links recreated, no shared writable aliases"
                )
            else:
                initial_issues = len(self.issues)
                entry.directory_entries = self.walk(child, entry.path, depth + 1)
                entry.descendants_inventoried = len(self.issues) == initial_issues
                return
            entry.descendants_inventoried = False
        finally:
            os.close(child)

    @staticmethod
    def is_repository(descriptor: int) -> bool:
        def exists(name: str) -> bool:
            try:
                os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return False
            return True

        return exists(".git") or all(
            exists(name) for name in ("HEAD", "objects", "refs")
        )


def _safe_status(root: int, relative: str) -> os.stat_result | None:
    parts = PurePosixPath(relative).parts
    descriptor = os.dup(root)
    try:
        for name in parts[:-1]:
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        return os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _safe_stat(root: int, relative: str) -> dict[str, int] | None:
    status = _safe_status(root, relative)
    return _evidence(status) if status is not None else None


def _root_is_symlink(root: Path, relative: str) -> bool:
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        observation = _safe_stat(descriptor, relative)
        return observation is not None and stat.S_ISLNK(observation["mode"])
    finally:
        os.close(descriptor)


def _consent_paths(paths: Sequence[str]) -> frozenset[str]:
    for path in paths:
        parts = PurePosixPath(path).parts
        if (
            not path
            or path.startswith("/")
            or "\x00" in path
            or "\\" in path
            or not parts
            or any(part in {".", ".."} for part in path.split("/"))
            or PurePosixPath(path).as_posix() != path
        ):
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                "secret approval requires an exact normalized "
                "checkout-relative file path",
            )
    return frozenset(paths)


def _entry(
    observed: _Observed, ignored: bool, consent: frozenset[str]
) -> HydrationEntry:
    status = observed.status
    kind = _kind(status.st_mode)
    sensitivity = "likely_secret" if _likely_secret(observed.path) else "unclassified"
    size = status.st_size if kind == "regular" else 0
    allocated = getattr(status, "st_blocks", 0) * 512 if kind == "regular" else 0
    action, reason = observed.refusal, observed.reason
    mode = None
    if action is None:
        if kind == "directory":
            action, reason, mode = (
                "create_directory",
                "container only; each descendant has its own decision",
                "0700",
            )
        elif sensitivity == "likely_secret" and observed.path not in consent:
            action, reason = (
                "needs_consent",
                "likely secret; exact-path consent is required "
                "without reading its contents",
            )
        elif sensitivity == "likely_secret":
            action, reason, mode = (
                "private_copy",
                "explicitly approved file; private copy only, no link or sync-back",
                "0600",
            )
        else:
            action, reason = (
                "clone_or_copy",
                "prefer verified COW; otherwise a budgeted private copy, "
                "never a hardlink",
            )
    requires_decision = action not in {
        *_COPY_ACTIONS,
        "create_directory",
        "exclude_administration",
        "rebuild",
    }
    return HydrationEntry(
        path=observed.path,
        kind=kind,
        classification="ignored_runtime" if ignored else "unsupported_or_excluded",
        sensitivity=sensitivity,
        action=action,
        reason=reason,
        apparent_bytes=size,
        allocated_bytes=allocated,
        estimated_copy_bytes=max(size, allocated),
        source_device=status.st_dev,
        requires_decision=requires_decision,
        descendants_inventoried=observed.descendants_inventoried,
        directory_entries=observed.directory_entries,
        destination_mode=mode,
        ignore_provenance="git_check_ignore_current_rules" if ignored else None,
        observation=_evidence(status),
    )


def plan_hydration(
    checkout: Path,
    *,
    budgets: HydrationBudgets | None = None,
    approved_secret_paths: Sequence[str] = (),
    rebuild: Sequence[RebuildConfig] = (),
    frozen_rebuild_roots: Sequence[str] = (),
) -> HydrationPlan:
    """Preview ignored entries without reading their contents or saving consent.

    Refused directories explicitly mark descendants as unexamined. A truncated
    or changing inventory disables all candidate copy actions. Even a complete
    preview requires source/rule pinning and a fresh scan before C2 can execute.
    """
    limits = budgets or HydrationBudgets()
    consent = _consent_paths(approved_secret_paths)
    deadline = time.monotonic() + limits.max_seconds
    try:
        requested = checkout.expanduser().resolve(strict=True)
    except OSError as exc:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_NOT_FOUND,
            "hydration preview needs an existing Git checkout",
        ) from exc
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "this platform cannot inventory directories without following links",
        )
    inventory = _Inventory(requested, limits, deadline)
    root = requested
    entries: list[HydrationEntry] = []
    planned_bytes = estimated_bytes = 0
    try:
        code, output = _bounded_git(
            requested,
            ["rev-parse", "--show-toplevel"],
            deadline=deadline,
            limit=limits.max_metadata_bytes,
        )
        if code != 0:
            raise LlmCoordError(
                ErrorCode.REPOSITORY_NOT_FOUND,
                "hydration preview needs a non-bare Git checkout",
            )
        root = Path(os.fsdecode(output.removesuffix(b"\n"))).resolve(strict=True)
        configured_roots = frozenset(spec.root for spec in rebuild)
        rebuild_roots = configured_roots | frozenset(frozen_rebuild_roots)
        if configured_roots:
            ignored_roots = _ignored_paths(
                root,
                [
                    path if _root_is_symlink(root, path) else path + "/"
                    for path in sorted(configured_roots)
                ],
                deadline=deadline,
                limit=limits.max_metadata_bytes,
            )
            code, tracked = _bounded_git(
                root,
                ["ls-files", "-z", "--", *sorted(configured_roots)],
                deadline=deadline,
                limit=limits.max_metadata_bytes,
            )
            if (
                code
                or tracked
                or configured_roots != {path.rstrip("/") for path in ignored_roots}
            ):
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID,
                    "rebuild roots must be ignored and contain no tracked source",
                )
        inventory = _Inventory(root, limits, deadline, rebuild_roots)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            root_evidence = _evidence(os.fstat(descriptor))
            inventory.walk(descriptor)
            # Every path remains in the bounded catalogue, including ordinary
            # source whose full stat record is no longer retained for hydration.
            ignored = inventory.ignored_paths()
            inventory.promote(descriptor, ignored)
            repeated = inventory.ignored_paths()
            rules_changed = any(
                _safe_stat(descriptor, entry.path) != _evidence(entry.status)
                for entry in inventory.entries
                if PurePosixPath(entry.path).name == ".gitignore"
            )
            if (
                ignored != repeated
                or rules_changed
                or not inventory.deferred_ignored_paths.issubset(ignored)
            ):
                inventory.issues.append("ignore_policy_changed")
            if root_evidence != _evidence(
                os.fstat(descriptor)
            ) or root_evidence != _evidence(root.lstat()):
                inventory.issues.append("checkout_changed_during_inventory")
            for observed in sorted(inventory.entries, key=lambda item: item.path):
                if observed.path not in ignored and observed.refusal is None:
                    continue
                entry = _entry(observed, observed.path in ignored, consent)
                if entry.action in _COPY_ACTIONS:
                    if (
                        entry.apparent_bytes > limits.max_file_bytes
                        or planned_bytes + entry.apparent_bytes > limits.max_total_bytes
                        or estimated_bytes + entry.estimated_copy_bytes
                        > limits.max_allocated_bytes
                    ):
                        entry = replace(
                            entry,
                            action="needs_budget",
                            requires_decision=True,
                            reason="per-file, total logical, or estimated copy "
                            "allocation budget exceeded",
                        )
                    else:
                        planned_bytes += entry.apparent_bytes
                        estimated_bytes += entry.estimated_copy_bytes
                entries.append(entry)
            for relative in sorted(rebuild_roots - {entry.path for entry in entries}):
                if _safe_stat(descriptor, relative) is not None:
                    raise LlmCoordError(
                        ErrorCode.REPOSITORY_UNSAFE,
                        "rebuild root has an unsafe or excluded ancestor: " + relative,
                    )
                # Missing dependency roots are deliberately rebuildable, including
                # external Poetry environments that have no checkout-local copy.
                entries.append(
                    HydrationEntry(
                        path=relative,
                        kind="missing",
                        classification="ignored_runtime",
                        sensitivity="unclassified",
                        action="rebuild",
                        reason="explicit private installer; no primary dependency copy",
                        apparent_bytes=0,
                        allocated_bytes=0,
                        estimated_copy_bytes=0,
                        source_device=root_evidence["device"],
                        requires_decision=False,
                        descendants_inventoried=False,
                        ignore_provenance="git_check_ignore_current_rules",
                    )
                )
            entries.sort(key=lambda entry: entry.path)
        finally:
            os.close(descriptor)
    except _BudgetExceeded as exc:
        inventory.issues.append(str(exc))
    except OSError as exc:
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "checkout could not be safely inventoried; retry the preview",
        ) from exc
    finally:
        inventory.close()
    issues = tuple(sorted(set(inventory.issues)))
    complete = not issues
    if not complete:
        entries = [
            replace(
                entry,
                action="needs_rescan",
                requires_decision=True,
                reason="inventory is incomplete or changed; repeat a bounded preview",
            )
            if entry.action in _COPY_ACTIONS
            else entry
            for entry in entries
        ]
        planned_bytes = estimated_bytes = 0
    return HydrationPlan(
        checkout=str(root),
        entries=tuple(entries),
        complete=complete,
        issues=issues,
        budgets=limits,
        scanned_entries=inventory.scanned,
        inventory_metadata_bytes=inventory.metadata_bytes,
        planned_apparent_bytes=planned_bytes,
        planned_estimated_copy_bytes=estimated_bytes,
        requires_decision=not complete
        or any(entry.requires_decision for entry in entries),
    )


__all__ = ["HydrationBudgets", "HydrationEntry", "HydrationPlan", "plan_hydration"]
