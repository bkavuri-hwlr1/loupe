"""Bounded, metadata-only preview of ignored runtime hydration.

This module never opens runtime file contents, follows descendant symlinks,
copies bytes, creates a worktree, or persists consent. A plan is diagnostic
evidence for the operator, not authorization or an executable snapshot.
"""

from __future__ import annotations

import math
import os
import posixpath
import selectors
import stat
import subprocess
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from llm_cli.config.models import RebuildConfig
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import sanitized_git_environment

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
_CHECK_BATCH = 256


@dataclass(frozen=True, slots=True)
class HydrationBudgets:
    """Limits apply to the entire inventory, including ordinary source paths."""

    max_entries: int = 20_000
    max_metadata_bytes: int = 4 * 1024 * 1024
    max_depth: int = 64
    max_file_bytes: int = 256 * 1024 * 1024
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_allocated_bytes: int = 2 * 1024 * 1024 * 1024
    max_seconds: float = 10.0

    def __post_init__(self) -> None:
        ceilings = {
            "max_entries": 100_000,
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
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            if input_data:
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
                                process.stdin.fileno(), input_data[sent : sent + 8192]
                            )
                        except BrokenPipeError:
                            sent = len(input_data)
                        if sent == len(input_data):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                        continue
                    descriptor = key.fd
                    chunk = os.read(descriptor, min(65_536, limit - total + 1))
                    if not chunk:
                        selector.unregister(selected_stream)
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise _BudgetExceeded("git_metadata_budget_exceeded")
                    if selected_stream is process.stdout:
                        output.extend(chunk)
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


def _ignored_paths(
    root: Path, paths: Sequence[str], *, deadline: float, limit: int
) -> set[str]:
    ignored: set[str] = set()
    for start in range(0, len(paths), _CHECK_BATCH):
        batch = paths[start : start + _CHECK_BATCH]
        code, output = _bounded_git(
            root,
            ["check-ignore", "-z", "--stdin"],
            deadline=deadline,
            limit=limit,
            input_data=b"\x00".join(map(os.fsencode, batch)) + b"\x00",
        )
        if code not in {0, 1}:
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE,
                "Git could not classify ignored runtime paths",
            )
        matches = {os.fsdecode(path) for path in output.split(b"\x00") if path}
        if not matches.issubset(batch):
            raise LlmCoordError(
                ErrorCode.REPOSITORY_UNSAFE, "Git returned unexpected ignore metadata"
            )
        ignored.update(matches)
    return ignored


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
        self.aliases: dict[str, _Observed] = {}
        self.deferred_ignored_paths: set[str] = set()

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
                self.metadata_bytes += len(encoded) + 256
                if self.metadata_bytes > self.budgets.max_metadata_bytes:
                    raise _BudgetExceeded("metadata_budget_exceeded")
                names.append(entry.name)
        self.scanned += len(names)
        return sorted(names)

    def walk(self, descriptor: int, prefix: str = "", depth: int = 0) -> int | None:
        self.remaining()
        before = _evidence(os.fstat(descriptor))
        child_count: int | None = None
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
                    self.entries.append(entry)
                    alias = unicodedata.normalize("NFC", relative).casefold()
                    previous = self.aliases.get(alias)
                    if previous is not None:
                        for collision in (entry, previous):
                            collision.refusal = "refuse_path_alias"
                            collision.reason = "case or Unicode destination alias"
                        self.issues.append("path_alias_requires_decision")
                    else:
                        self.aliases[alias] = entry
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
                        entry.refusal = "needs_rescan"
                        entry.reason = "entry changed during metadata observation"
                        self.issues.append("entry_changed_during_inventory")
                except OSError:
                    if entry is not None:
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


def _safe_stat(root: int, relative: str) -> dict[str, int] | None:
    parts = PurePosixPath(relative).parts
    descriptor = os.dup(root)
    try:
        for name in parts[:-1]:
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        return _evidence(os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False))
    except OSError:
        return None
    finally:
        os.close(descriptor)


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
    """Preview current ignored entries; no bytes, consent, or state are written.

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
            paths = [
                entry.path
                for entry in inventory.entries
                if entry.refusal != "exclude_administration"
            ]
            ignored = _ignored_paths(
                root, paths, deadline=deadline, limit=limits.max_metadata_bytes
            )
            # A second passive classification plus rule metadata catches a
            # changing ignore policy. No rule text or content digest is exposed.
            repeated = _ignored_paths(
                root, paths, deadline=deadline, limit=limits.max_metadata_bytes
            )
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
