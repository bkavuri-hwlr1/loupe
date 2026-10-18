"""Account-wide ownership of a verified physical checkout (ADR 0010).

Kernel locks grant authority; records only explain it. No environment variable
selects this lock domain. The execution contract remains claim_backed_v1.
"""

from __future__ import annotations

import contextvars
import ctypes
import fcntl
import functools
import hashlib
import os
import pwd
import secrets
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.workspace.installation import (
    InstallationIdentity,
    encode,
    private_directory,
    private_read,
    private_write,
    recovery_error,
)

if TYPE_CHECKING:
    from llm_cli.storage.control import ControlStore

SUPPORTED_FILESYSTEMS = frozenset(
    {"apfs", "hfs", "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "tmpfs", "overlay"}
)
IDLE_SECONDS = 30.0
_ACTIVE: contextvars.ContextVar[tuple[PhysicalAuthority, Path] | None] = (
    contextvars.ContextVar("physical_checkout_authority", default=None)
)

_MUTATION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "physical_mutation", default=None
)

_STORE_HELD: contextvars.ContextVar[tuple[str, int, int] | None] = (
    contextvars.ContextVar("common_git_store", default=None)
)


def account_authority_root() -> Path:
    """Fixed account-home root, independent of HOME, profile and XDG settings."""
    return (
        Path(pwd.getpwuid(os.geteuid()).pw_dir)
        / ".llm-coord"
        / "physical-authority"
        / "v1"
    )


def process_start_identity(pid: int) -> str:
    if sys.platform.startswith("linux"):
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return (
            "linux:"
            + Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            + ":"
            + fields[19]
        )
    if sys.platform == "darwin":
        buffer = ctypes.create_string_buffer(136)
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        if library.proc_pidinfo(pid, 3, 0, buffer, len(buffer)) != len(buffer):
            raise recovery_error("process start identity cannot be verified")
        seconds = int.from_bytes(buffer.raw[120:128], sys.byteorder)
        micros = int.from_bytes(buffer.raw[128:136], sys.byteorder)
        return f"darwin:{seconds}:{micros}"
    raise recovery_error("this platform has no verified process-start identity")


def _filesystem(path: Path) -> str:
    if sys.platform == "darwin":
        buffer = ctypes.create_string_buffer(4096)
        library = ctypes.CDLL(None, use_errno=True)
        if library.statfs(os.fsencode(path), buffer) != 0:
            raise recovery_error("authority filesystem cannot be verified")
        return buffer.raw[72:88].split(b"\0", 1)[0].decode("ascii")
    if sys.platform.startswith("linux"):
        matches: list[tuple[int, str]] = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            before, after = line.split(" - ", 1)
            mount = Path(before.split()[4].replace("\\040", " "))
            if path == mount or path.is_relative_to(mount):
                matches.append((len(mount.parts), after.split()[0]))
        if matches:
            return max(matches)[1]
    raise recovery_error("authority filesystem semantics are unknown")


def _identity(status: os.stat_result) -> dict[str, int]:
    return {
        "dev": status.st_dev,
        "ino": status.st_ino,
        "birth_ns": int(getattr(status, "st_birthtime", 0) * 1_000_000_000),
        "generation": int(getattr(status, "st_gen", 0)),
    }


def _open_directory(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    status = os.fstat(fd)
    if status.st_uid != os.geteuid() or not stat.S_ISDIR(status.st_mode):
        os.close(fd)
        raise recovery_error("checkout administration must be owned by this account")
    return fd


def _admin_text(directory: int, name: str) -> str:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 4096:
            raise ValueError("Git administration pointer is not bounded regular text")
        data = os.read(fd, 4097)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(data) != before.st_size or any(
            getattr(before, key) != getattr(observed, key)
            for observed in (after, named)
            for key in fields
        ):
            raise ValueError("Git administration pointer changed while read")
        return data.decode().strip()
    finally:
        os.close(fd)


@dataclass
class PhysicalCheckout:
    root: Path
    admin: Path
    common: Path
    binding: dict[str, Any]
    key: str
    store_key: str
    descriptors: tuple[int, int, int]

    @classmethod
    def inspect(cls, supplied: Path) -> PhysicalCheckout:
        descriptors: list[int] = []
        try:
            root = supplied.resolve(strict=True)
            descriptors.append(_open_directory(root))
            data = run_git(
                root,
                [
                    "rev-parse",
                    "--path-format=absolute",
                    "--show-toplevel",
                    "--git-dir",
                    "--git-common-dir",
                    "--show-object-format",
                ],
            )
            assert isinstance(data.stdout, str)
            actual, admin_name, common_name, object_format = (
                data.stdout.strip().splitlines()
            )
            if Path(actual).resolve(strict=True) != root:
                raise ValueError("checkout root differs from Git root")
            admin = Path(admin_name).resolve(strict=True)
            common = Path(common_name).resolve(strict=True)
            if (root / ".git").is_symlink():
                raise ValueError("Git administration is a symlink")
            descriptors.extend((_open_directory(admin), _open_directory(common)))
            for directory in (root, admin, common):
                if _filesystem(directory) not in SUPPORTED_FILESYSTEMS:
                    raise recovery_error(
                        "checkout and Git administration require a supported local "
                        "filesystem"
                    )
            if admin != root / ".git" and (
                not (root / ".git").is_file()
                or Path((admin / "gitdir").read_text().strip()).resolve(strict=True)
                != root / ".git"
            ):
                raise ValueError("linked worktree relationship differs")
            if object_format not in {"sha1", "sha256"}:
                raise ValueError("unknown Git object format")
            binding: dict[str, Any] = {
                "version": 1,
                "root": _identity(os.fstat(descriptors[0])),
                "admin": _identity(os.fstat(descriptors[1])),
                "common": _identity(os.fstat(descriptors[2])),
                "object_format": object_format,
            }
            key = hashlib.sha256(
                encode(
                    {
                        k: binding[k]
                        for k in ("version", "root", "admin", "object_format")
                    }
                )
            ).hexdigest()
            store_key = hashlib.sha256(
                encode({"version": 1, "common": binding["common"]})
            ).hexdigest()
            result = cls(
                root,
                admin,
                common,
                binding,
                key,
                store_key,
                (descriptors[0], descriptors[1], descriptors[2]),
            )
            result.validate()
            return result
        except Exception as exc:
            for fd in descriptors:
                os.close(fd)
            if isinstance(exc, LlmCoordError):
                raise
            raise LlmCoordError(
                ErrorCode.CHECKOUT_IDENTITY_CHANGED,
                "physical checkout binding could not be verified",
            ) from exc

    def validate(self) -> None:
        try:
            for path, descriptor, name in zip(
                (self.root, self.admin, self.common),
                self.descriptors,
                ("root", "admin", "common"),
                strict=True,
            ):
                if (
                    path.is_symlink()
                    or _identity(path.lstat()) != self.binding[name]
                    or _identity(os.fstat(descriptor)) != self.binding[name]
                ):
                    raise ValueError("physical identity changed")
            if self.admin != self.root / ".git":
                pointer = _admin_text(self.descriptors[0], ".git")
                if not pointer.startswith("gitdir: "):
                    raise ValueError("linked administration pointer changed")
                destination = Path(pointer.removeprefix("gitdir: "))
                if not destination.is_absolute():
                    destination = self.root / destination
                back_pointer = Path(_admin_text(self.descriptors[1], "gitdir"))
                if not back_pointer.is_absolute():
                    back_pointer = self.admin / back_pointer
                if (
                    destination.resolve(strict=True) != self.admin
                    or back_pointer.resolve(strict=True) != self.root / ".git"
                ):
                    raise ValueError("linked administration relationship changed")
            elif not (self.root / ".git").is_dir():
                raise ValueError("Git administration changed")
            try:
                common_pointer = Path(_admin_text(self.descriptors[1], "commondir"))
            except FileNotFoundError:
                if self.admin != self.common:
                    raise ValueError(
                        "Git common administration pointer is missing"
                    ) from None
            else:
                destination = (
                    common_pointer
                    if common_pointer.is_absolute()
                    else self.admin / common_pointer
                )
                if destination.resolve(strict=True) != self.common:
                    raise ValueError("Git common administration relationship changed")

        except (OSError, ValueError) as exc:
            raise LlmCoordError(
                ErrorCode.CHECKOUT_IDENTITY_CHANGED,
                "physical checkout identity changed; explicit re-registration is "
                "required",
            ) from exc

    def close(self) -> None:
        for fd in self.descriptors:
            os.close(fd)


@dataclass
class _Owned:
    physical: PhysicalCheckout
    descriptor: int
    marker: dict[str, Any]
    pins: int = 0
    idle_since: float = field(default_factory=time.monotonic)
    mutation_lock: threading.RLock = field(default_factory=threading.RLock)
    history_gap: bool = False


class OwnershipPin:
    def __init__(
        self, authority: PhysicalAuthority, owner: _Owned, *, activity: bool = True
    ) -> None:
        self.authority, self.owner = authority, owner
        self.closed = False
        self.activity = activity

    def validate(self) -> None:
        self.authority._validate(self.owner)

    def close(self) -> None:
        with self.authority._lock:
            if not self.closed:
                self.closed = True
                self.owner.pins -= 1
                if self.activity:
                    self.owner.idle_since = time.monotonic()
                if self.authority._closing and not self.owner.pins:
                    self.authority._release(self.owner)


class Mutation:
    def __init__(
        self, authority: PhysicalAuthority, owner: _Owned, kind: str, reference_id: str
    ) -> None:
        self.authority, self.owner = authority, owner
        self.kind, self.reference_id = kind, reference_id
        self.confirmed = False
        self.delegated = _MUTATION.get() == owner.physical.key

    def confirm(self) -> None:
        """Called only after the domain journal and final paths are verified."""
        self.authority._validate(self.owner)
        if self.delegated:
            self.confirmed = True
            return
        if self.owner.marker["state"] == "clean":
            self.confirmed = True
            return
        journal = self.owner.marker["journal_id"]
        with self.authority.store.connection() as connection:
            connection.execute(
                "UPDATE authority_journals SET state='confirmed' WHERE journal_id=? "
                "AND physical_key=?",
                (journal, self.owner.physical.key),
            )
        self.authority._write_marker(self.owner, "clean")
        self.confirmed = True


class PhysicalAuthority:
    def __init__(
        self,
        store: ControlStore,
        profile: str,
        boot_id: str,
        socket: Path,
        *,
        root: Path | None = None,
    ) -> None:
        self.store, self.profile, self.boot_id, self.socket = (
            store,
            profile,
            boot_id,
            socket,
        )
        self._production_root = root is None
        self.root = account_authority_root() if root is None else root
        self.installation = InstallationIdentity(store)
        self._lock = threading.RLock()
        self._owned: dict[str, _Owned] = {}
        self._store_locks: dict[str, threading.RLock] = {}
        self._closing = False
        self._start = process_start_identity(os.getpid())

    def _directory(self, key: str) -> Path:
        return self.root / "checkouts" / key

    def _prepare_root(self) -> None:
        private_directory(self.root)
        if _filesystem(self.root) not in SUPPORTED_FILESYSTEMS:
            raise recovery_error(
                "authority requires a verified supported local filesystem"
            )
        for ancestor in (
            *(
                (self.root.parent, self.root.parent.parent)
                if self._production_root
                else ()
            ),
            self.root / "checkouts",
            self.root / "git-stores",
        ):
            private_directory(ancestor)

    def _metadata(self, instance: str) -> dict[str, Any]:
        return {
            "version": 1,
            "profile": self.profile,
            "database_instance_id": instance,
            "boot_id": self.boot_id,
            "pid": os.getpid(),
            "process_start": self._start,
            "socket": str(self.socket),
            "acquired_at": time.time_ns() // 1_000_000,
        }

    def _validate(self, owner: _Owned) -> None:
        owner.physical.validate()
        instance = self.installation.validate()
        held = os.fstat(owner.descriptor)
        named = (self._directory(owner.physical.key) / "ownership.lock").lstat()
        if (
            owner.marker["database_instance_id"] != instance
            or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)
            or named.st_uid != os.geteuid()
            or named.st_mode & 0o077
            or named.st_nlink != 1
            or not stat.S_ISREG(named.st_mode)
        ):
            raise recovery_error("physical ownership or database binding changed")

        observed = private_read(self._directory(owner.physical.key) / "marker.json")
        if observed != owner.marker:
            raise recovery_error("authority marker changed while owned")

    def validate_current(self) -> None:
        active = _ACTIVE.get()
        if active is None or active[0] is not self:
            raise recovery_error("managed mutation has no live ownership pin")
        root = active[1]
        with self._lock:
            owner = next(
                (item for item in self._owned.values() if item.physical.root == root),
                None,
            )
            if owner is None:
                raise recovery_error("managed mutation lost its kernel ownership")
            self._validate(owner)

    def _write_marker(
        self,
        owner: _Owned,
        state: str,
        *,
        journal_id: str | None = None,
        journal_kind: str | None = None,
    ) -> None:
        marker = {
            "version": 1,
            "physical_key": owner.physical.key,
            "binding": owner.physical.binding,
            "generation": int(owner.marker.get("generation", 0)) + 1,
            "state": state,
            "execution_contract": "claim_backed_v1",
            "owner_profile": self.profile,
            "database_instance_id": self.installation.instance_id,
            "daemon_boot": self.boot_id,
            "checkout_id": owner.marker.get("checkout_id"),
            "journal_id": journal_id,
            "journal_kind": journal_kind,
        }
        private_write(self._directory(owner.physical.key) / "marker.json", marker)
        owner.marker = marker
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE authority_checkouts SET marker_generation=? WHERE "
                "physical_key=?",
                (marker["generation"], owner.physical.key),
            )

    def acquire(
        self, root: Path, *, recovery: bool = False, activity: bool = True
    ) -> OwnershipPin:
        with self._lock:
            physical = PhysicalCheckout.inspect(root)
            try:
                self._prepare_root()
                instance = self.installation.validate()
                prior = self._owned.get(physical.key)
                if prior is not None:
                    physical.close()
                    self._validate(prior)
                    if (
                        prior.marker["state"] != "clean"
                        and not recovery
                        and _MUTATION.get() != prior.physical.key
                    ):
                        raise recovery_error(
                            "checkout requires its owning journal recovery",
                            owner_profile=self.profile,
                            journal_locator=prior.marker.get("journal_id"),
                        )
                    prior.pins += 1
                    return OwnershipPin(self, prior, activity=activity)
                directory = self._directory(physical.key)
                private_directory(directory)
                path = directory / "ownership.lock"
                descriptor = os.open(
                    path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
                )
                status = os.fstat(descriptor)
                if (
                    status.st_uid != os.geteuid()
                    or status.st_mode & 0o077
                    or status.st_nlink != 1
                    or not stat.S_ISREG(status.st_mode)
                ):
                    os.close(descriptor)
                    raise recovery_error("authority lock has unsafe storage")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    os.close(descriptor)
                    owner_record = private_read(directory / "owner.json") or {}
                    marker = private_read(directory / "marker.json")
                    code = (
                        ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
                        if marker and marker.get("state") != "clean"
                        else ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE
                    )
                    raise LlmCoordError(
                        code,
                        "another profile owns this physical checkout",
                        {
                            "retryable": code
                            is ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE,
                            "owner_profile": owner_record.get("profile"),
                            "boot_id": owner_record.get("boot_id"),
                            "pid": owner_record.get("pid"),
                            "process_start": owner_record.get("process_start"),
                            "journal_locator": marker.get("journal_id")
                            if marker
                            else None,
                        },
                    ) from exc
                try:
                    marker = private_read(directory / "marker.json")
                    with self.store.connection() as connection:
                        checkout = connection.execute(
                            "SELECT checkout_id FROM checkouts WHERE canonical_path=?",
                            (str(physical.root),),
                        ).fetchone()
                        checkout_id = str(checkout[0]) if checkout else None
                        bindings = connection.execute(
                            "SELECT a.* FROM authority_checkouts a "
                            "LEFT JOIN checkouts c ON c.checkout_id=a.checkout_id "
                            "WHERE a.physical_key=? OR c.canonical_path=?",
                            (physical.key, str(physical.root)),
                        ).fetchall()
                    if any(
                        row["physical_key"] != physical.key
                        or row["binding_json"] != encode(physical.binding).decode()
                        for row in bindings
                    ):
                        raise LlmCoordError(
                            ErrorCode.CHECKOUT_IDENTITY_CHANGED,
                            "registered physical checkout identity changed",
                        )
                    if marker is None and bindings:
                        raise recovery_error(
                            "authority marker is missing for a previously bound "
                            "checkout"
                        )
                    if marker is not None and (
                        marker.get("version") != 1
                        or marker.get("execution_contract") != "claim_backed_v1"
                        or marker.get("physical_key") != physical.key
                        or marker.get("binding") != physical.binding
                        or marker.get("state") not in {"clean", "dirty", "attention"}
                        or type(marker.get("generation")) is not int
                    ):
                        raise recovery_error(
                            "authority marker is unknown or has changed identity"
                        )
                    if (
                        marker is not None
                        and bindings
                        and int(marker["generation"])
                        < int(bindings[0]["marker_generation"])
                    ):
                        raise recovery_error(
                            "authority marker generation moved backwards"
                        )
                    if marker is not None and marker["state"] != "clean":
                        if (
                            marker.get("owner_profile") != self.profile
                            or marker.get("database_instance_id") != instance
                        ):
                            raise recovery_error(
                                "recover the unfinished journal through its owning "
                                "profile",
                                owner_profile=marker.get("owner_profile"),
                                journal_locator=marker.get("journal_id"),
                            )
                        with self.store.connection() as connection:
                            journal = connection.execute(
                                "SELECT * FROM authority_journals WHERE journal_id=? "
                                "AND physical_key=?",
                                (marker.get("journal_id"), physical.key),
                            ).fetchone()
                        if journal is None or journal["kind"] != marker.get(
                            "journal_kind"
                        ):
                            raise recovery_error(
                                "the exact owning journal cannot be verified",
                                owner_profile=self.profile,
                                journal_locator=marker.get("journal_id"),
                            )
                        if not recovery:
                            raise recovery_error(
                                "the owning profile must recover its unfinished "
                                "journal",
                                owner_profile=self.profile,
                                journal_locator=marker.get("journal_id"),
                            )
                    owner = _Owned(
                        physical,
                        descriptor,
                        marker or {"generation": 0, "checkout_id": checkout_id},
                        history_gap=marker is not None,
                    )
                    with self.store.connection() as connection:
                        connection.execute(
                            "INSERT INTO "
                            "authority_checkouts(physical_key,checkout_id,binding_json,marker_generation)"
                            " VALUES (?,?,?,?) ON CONFLICT(physical_key) DO UPDATE SET"
                            " marker_generation=excluded.marker_generation",
                            (
                                physical.key,
                                checkout_id,
                                encode(physical.binding).decode(),
                                int(owner.marker["generation"]),
                            ),
                        )
                    if marker is None or marker["state"] == "clean":
                        if marker is not None:
                            self._handoff(physical, checkout_id, marker)
                        self._write_marker(owner, "clean")
                    private_write(directory / "owner.json", self._metadata(instance))
                    self._owned[physical.key] = owner
                    owner.pins = 1
                    return OwnershipPin(self, owner, activity=activity)
                except Exception:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
                    raise
            except Exception:
                if physical.key not in self._owned:
                    physical.close()
                raise

    def _handoff(
        self,
        physical: PhysicalCheckout,
        checkout_id: str | None,
        marker: dict[str, Any],
    ) -> None:
        if checkout_id is None:
            return
        # C4's stable scanner installs observations before any edit capability.
        from llm_cli.workspace.external import ExternalWorkspaceScanner

        scanner = ExternalWorkspaceScanner(self.store)
        from llm_cli.workspace.isolated import _source_paths

        paths = _source_paths(physical.root)

        def observations() -> dict[str, tuple[int, ...] | None]:
            result: dict[str, tuple[int, ...] | None] = {}
            for path in paths:
                target = physical.root / path
                try:
                    info = target.lstat()
                    result[path] = (
                        info.st_dev,
                        info.st_ino,
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                        info.st_mode,
                    )
                except FileNotFoundError:
                    result[path] = None
            return result

        before = observations()
        for session in self.store.list_sessions(checkout_id=checkout_id):
            scanner._scan_session_owned(session.session_id)
        if paths != _source_paths(physical.root) or observations() != before:
            raise recovery_error(
                "source changed during clean handoff; retry after edits settle"
            )
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE workspaces SET workspace_epoch=workspace_epoch+1 WHERE "
                "checkout_id=?",
                (checkout_id,),
            )
            self.store.append_checkout_event(
                connection,
                checkout_id=checkout_id,
                event_type="checkout.authority_handoff",
                caused_by_session_id=None,
                payload={
                    "paths": [],
                    "history_gap": True,
                    "previous_profile": marker.get("owner_profile"),
                    "execution_contract": "claim_backed_v1",
                },
                now=self.store._now(None),
            )

    @contextmanager
    def pin(
        self,
        root: Path,
        *,
        recovery: bool = False,
        git_store: bool = False,
        activity: bool = True,
    ) -> Iterator[OwnershipPin]:
        pin = self.acquire(root, recovery=recovery, activity=activity)
        token = _ACTIVE.set((self, pin.owner.physical.root))
        try:
            if git_store:
                with self.common_store(pin.owner):
                    yield pin
            else:
                yield pin
        finally:
            _ACTIVE.reset(token)
            pin.close()

    @contextmanager
    def common_store(self, owner: _Owned) -> Iterator[None]:
        identity = (owner.physical.store_key, os.getpid(), threading.get_ident())
        if _STORE_HELD.get() == identity:
            self._validate(owner)
            yield
            return
        with self._lock:
            local_lock = self._store_locks.setdefault(
                owner.physical.store_key, threading.RLock()
            )
        with local_lock:
            path = self.root / "git-stores" / (owner.physical.store_key + ".lock")
            private_directory(path.parent)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                status = os.fstat(fd)
                if (
                    status.st_uid != os.geteuid()
                    or status.st_mode & 0o077
                    or status.st_nlink != 1
                    or not stat.S_ISREG(status.st_mode)
                ):
                    raise recovery_error("common Git store lock is unsafe")
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._validate(owner)
                token = _STORE_HELD.set(identity)
                try:
                    yield
                finally:
                    _STORE_HELD.reset(token)
            finally:
                os.close(fd)

    @contextmanager
    def mutation(
        self,
        root: Path,
        journal_kind: str,
        reference_id: str,
        *,
        recovery: bool = False,
    ) -> Iterator[Mutation]:
        with self.pin(root, recovery=recovery) as pin, pin.owner.mutation_lock:
            owner = pin.owner
            self._validate(owner)
            if _MUTATION.get() == owner.physical.key:
                yield Mutation(self, owner, journal_kind, reference_id)
                return
            if owner.marker["state"] == "clean":
                journal_id = "authority_" + secrets.token_hex(16)
                # Crash here leaves a durable dirty locator without its journal:
                # acquisition must retain attention, never infer no effect.
                self._write_marker(
                    owner, "dirty", journal_id=journal_id, journal_kind=journal_kind
                )
                with self.store.connection() as connection:
                    connection.execute(
                        "INSERT INTO authority_journals VALUES (?,?,?,?, 'applying',?)",
                        (
                            journal_id,
                            owner.physical.key,
                            journal_kind,
                            reference_id,
                            self.store._now(None),
                        ),
                    )
            else:
                with self.store.connection() as connection:
                    journal = connection.execute(
                        "SELECT * FROM authority_journals WHERE journal_id=?",
                        (owner.marker["journal_id"],),
                    ).fetchone()
                if (
                    journal is None
                    or journal["kind"] != journal_kind
                    or journal["reference_id"] != reference_id
                ):
                    raise recovery_error(
                        "another unfinished operation requires exact recovery",
                        owner_profile=self.profile,
                        journal_locator=owner.marker.get("journal_id"),
                    )
            operation = Mutation(self, owner, journal_kind, reference_id)
            token = _MUTATION.set(owner.physical.key)
            try:
                yield operation
            finally:
                _MUTATION.reset(token)

    def finish_recovery(self, root: Path) -> bool:
        with self.pin(root, recovery=True) as pin, pin.owner.mutation_lock:
            owner = pin.owner
            if owner.marker["state"] == "clean":
                return True
            with self.store.connection() as connection:
                journal = connection.execute(
                    "SELECT * FROM authority_journals WHERE journal_id=?",
                    (owner.marker["journal_id"],),
                ).fetchone()
                if journal is None:
                    return False
                proved = False
                if journal["kind"] == "batch":
                    row = connection.execute(
                        "SELECT * FROM workspace_batches WHERE batch_id=?",
                        (journal["reference_id"],),
                    ).fetchone()
                    if row is not None and row["state"] == "diverged":
                        proved = True  # Domain journal records no filesystem effect.
                    elif row is not None and row["state"] == "published":
                        from llm_cli.workspace.batches import (
                            _journal_files,
                            _matches_snapshot,
                            _observe,
                            _target,
                        )

                        proved = all(
                            _matches_snapshot(
                                _observe(
                                    _target(owner.physical.root, item.relative_path)
                                ),
                                item.result,
                                result=True,
                            )
                            for item in _journal_files(row["request_json"])
                        )
                elif journal["kind"] == "manual":
                    row = connection.execute(
                        "SELECT p.operation_state,c.* FROM workspace_publications p "
                        "JOIN workspace_candidates c ON c.candidate_id=p.candidate_id "
                        "WHERE p.candidate_id=?",
                        (journal["reference_id"],),
                    ).fetchone()
                    if row is not None and row["operation_state"] in {
                        "confirmed",
                        "rolled_back",
                    }:
                        from llm_cli.workspace.broker import workspace_target
                        from llm_cli.workspace.identity import identify_path

                        prefix = (
                            "result"
                            if row["operation_state"] == "confirmed"
                            else "base"
                        )
                        observed = identify_path(
                            workspace_target(owner.physical.root, row["relative_path"])
                        )
                        proved = (
                            str(observed.kind),
                            observed.mode,
                            observed.digest,
                        ) == (
                            row[prefix + "_kind"],
                            row[prefix + "_mode"],
                            row[prefix + "_digest"],
                        )

                elif journal["kind"] == "legacy_execution":
                    row = connection.execute(
                        "SELECT state FROM task_executions WHERE execution_id=?",
                        (journal["reference_id"],),
                    ).fetchone()
                    proved = (
                        row is not None
                        and row["state"] in {"failed", "cleaned"}
                        and journal["state"] == "confirmed"
                    )

            if proved:
                Mutation(
                    self, owner, str(journal["kind"]), str(journal["reference_id"])
                ).confirm()
            return bool(proved)

    def release_idle(self, *, now: float | None = None, force: bool = False) -> None:
        with self._lock:
            instant = time.monotonic() if now is None else now
            for owner in tuple(self._owned.values()):
                if (
                    owner.pins
                    or owner.marker["state"] != "clean"
                    or (not force and instant - owner.idle_since < IDLE_SECONDS)
                ):
                    continue
                with self.store.connection() as connection:
                    busy = connection.execute(
                        "SELECT 1 FROM claims c JOIN tasks t ON t.task_id=c.task_id "
                        "JOIN repositories r ON r.repo_key=t.repo_key WHERE "
                        "r.main_worktree_path=? AND c.state IN "
                        "('active_work','publishing','active_integration') LIMIT 1",
                        (str(owner.physical.root),),
                    ).fetchone()
                if busy is None:
                    self._release(owner)

    def _release(self, owner: _Owned) -> None:
        fcntl.flock(owner.descriptor, fcntl.LOCK_UN)
        os.close(owner.descriptor)
        owner.physical.close()
        self._owned.pop(owner.physical.key, None)

    def close(self) -> None:
        with self._lock:
            self._closing = True
            # Shutdown is not a clean handoff. Retain dirty markers, but once
            # real workers drain the process may release its kernel descriptor.
            for owner in tuple(self._owned.values()):
                if not owner.pins:
                    self._release(owner)

    def is_owned(self, root: Path) -> bool:
        with self._lock:
            return any(
                owner.physical.root == root.resolve() for owner in self._owned.values()
            )

    def status(self, root: Path) -> dict[str, Any]:
        physical: PhysicalCheckout | None = None
        try:
            physical = PhysicalCheckout.inspect(root)
            instance = self.installation.validate()
            directory = self._directory(physical.key)
            marker = private_read(directory / "marker.json")
            with self.store.connection() as connection:
                binding = connection.execute(
                    "SELECT * FROM authority_checkouts WHERE physical_key=?",
                    (physical.key,),
                ).fetchone()
                historical = connection.execute(
                    "SELECT a.physical_key FROM authority_checkouts a JOIN checkouts c"
                    " ON c.checkout_id=a.checkout_id WHERE c.canonical_path=?",
                    (str(physical.root),),
                ).fetchall()
            if any(row[0] != physical.key for row in historical):
                raise LlmCoordError(
                    ErrorCode.CHECKOUT_IDENTITY_CHANGED,
                    "registered physical checkout identity changed",
                )
            if (marker is None and binding is not None) or (
                marker is not None and marker.get("version") != 1
            ):
                raise recovery_error("authority marker is missing or unsupported")
            owner = (
                private_read(directory / "owner.json") if directory.exists() else None
            )
            if marker is not None and marker.get("state") != "clean":
                state = (
                    "foreign_recovery"
                    if marker.get("database_instance_id") != instance
                    else "recovery_required"
                )
            elif physical.key in self._owned:
                state = "owned"
            else:
                state = "observed_only"
            return {
                "state": state,
                "physical_key": physical.key,
                "execution_contract": "claim_backed_v1",
                "owner_profile": (owner or {}).get("profile"),
                "pid": (owner or {}).get("pid"),
                "process_start": (owner or {}).get("process_start"),
                "journal_locator": marker.get("journal_id") if marker else None,
                "cooperative_read_barrier": state == "owned",
                "history_gap": self._owned[physical.key].history_gap
                if physical.key in self._owned
                else False,
            }
        except (LlmCoordError, OSError) as exc:
            code = (
                exc.code
                if isinstance(exc, LlmCoordError)
                else ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
            )
            return {
                "state": "identity_changed"
                if code is ErrorCode.CHECKOUT_IDENTITY_CHANGED
                else "foreign_recovery",
                "error": str(code),
                "execution_contract": "claim_backed_v1",
                "cooperative_read_barrier": False,
            }
        finally:
            if physical is not None:
                physical.close()


@contextmanager
def managed_pin(
    store: ControlStore,
    root: Path,
    *,
    recovery: bool = False,
    activity: bool = True,
) -> Iterator[None]:
    if store.authority is None:
        yield
    else:
        with store.authority.pin(root, recovery=recovery, activity=activity):
            yield


@contextmanager
def git_side_effect(kind: str, reference_id: str) -> Iterator[None]:
    """Short common-store barrier for trusted Git side effects in a managed pin."""
    active = _ACTIVE.get()
    if active is None:
        yield
        return
    authority, root = active
    with authority.mutation(root, "git:" + kind, reference_id) as mutation:
        with authority.common_store(mutation.owner):
            yield
        mutation.confirm()


def managed_git_operation[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(function)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        # Never derive a shared locator from arbitrary arguments: validation
        # objects can include source patches and runtime equality evidence.
        reference = "git_" + secrets.token_hex(16)
        with git_side_effect(function.__name__, reference):
            return function(*args, **kwargs)

    return guarded


@contextmanager
def managed_mutation(
    store: ControlStore,
    root: Path,
    kind: str,
    reference_id: str,
    *,
    recovery: bool = False,
) -> Iterator[Mutation | None]:
    if store.authority is None:
        yield None
    else:
        with store.authority.mutation(
            root, kind, reference_id, recovery=recovery
        ) as operation:
            yield operation
