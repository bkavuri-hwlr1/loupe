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
import platform
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

_REGIME_RANKS = {"legacy": 0, "transition": 1, "workspace": 2}


def _marker_regime(marker: dict[str, Any] | None) -> dict[str, Any]:
    if marker is None or marker.get("version") == 1:
        return {
            "regime": "legacy",
            "generation": 0,
            "intent_id": None,
            "checkpoint": None,
        }
    return {
        "regime": marker["regime"],
        "generation": marker["regime_generation"],
        "intent_id": marker["regime_intent_id"],
        "checkpoint": marker["regime_checkpoint"],
    }


def _supported_marker(marker: dict[str, Any]) -> bool:
    if marker.get("version") == 1:
        return marker.get("execution_contract") == "claim_backed_v1"
    return (
        marker.get("version") == 2
        and marker.get("regime") in {"transition", "workspace"}
        and type(marker.get("regime_generation")) is int
        and marker["regime_generation"] > 0
        and isinstance(marker.get("regime_checkpoint"), str)
        and len(marker["regime_checkpoint"]) == 64
        and isinstance(marker.get("regime_intent_id"), str)
        and bool(marker["regime_intent_id"])
        and marker.get("execution_contract")
        == ("workspace_v1" if marker["regime"] == "workspace" else "transition_v1")
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
        # Darwin's x86_64 dlsym("statfs") exposes the legacy ABI, whose
        # f_fstypename starts at 104. Match the modern SDK layout (offset 72)
        # explicitly there; arm64 exposes that layout through plain statfs.
        symbol = "statfs$INODE64" if platform.machine() == "x86_64" else "statfs"
        try:
            statfs = getattr(library, symbol)
        except AttributeError as exc:
            raise recovery_error("authority filesystem cannot be verified") from exc
        statfs.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        statfs.restype = ctypes.c_int
        if statfs(os.fsencode(path), buffer) != 0:
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
                filesystem = _filesystem(directory)
                if filesystem not in SUPPORTED_FILESYSTEMS:
                    raise recovery_error(
                        "checkout and Git administration require a supported local "
                        f"filesystem; detected {filesystem!r}; supported: "
                        + ", ".join(sorted(SUPPORTED_FILESYSTEMS)),
                        filesystem=filesystem,
                        supported_filesystems=sorted(SUPPORTED_FILESYSTEMS),
                    )
            if admin != root / ".git" and not (root / ".git").is_file():
                raise ValueError("Git administration pointer is not a regular file")
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
                if destination.resolve(strict=True) != self.admin:
                    raise ValueError("Git administration pointer changed")
                if self.admin != self.common:
                    back_pointer = Path(_admin_text(self.descriptors[1], "gitdir"))
                    if not back_pointer.is_absolute():
                        back_pointer = self.admin / back_pointer
                    if back_pointer.resolve(strict=True) != self.root / ".git":
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
                if self.admin == self.common and self.admin != self.root / ".git":
                    raise ValueError("standalone administration has a common pointer")
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
    live_mutation: threading.Event | None = None


class _LiveMutationPending(Exception):
    def __init__(self, completed: threading.Event) -> None:
        self.completed = completed


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
            proof = connection.execute(
                "SELECT 1 FROM authority_journals WHERE journal_id=? "
                "AND physical_key=? AND kind=? AND reference_id=?",
                (journal, self.owner.physical.key, self.kind, self.reference_id),
            ).fetchone()
            if proof is None or self.owner.marker.get("journal_kind") != self.kind:
                raise recovery_error("mutation confirmation does not match its journal")
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
        # Observers run inside SQLite writer transactions. Never access SQLite
        # while holding this lock, or acquire the authority lock from it.
        self._observation_lock = threading.RLock()
        self._owned: dict[str, _Owned] = {}
        self._store_locks: dict[str, threading.RLock] = {}
        self._closing = False
        self._start = process_start_identity(os.getpid())

    def _directory(self, key: str) -> Path:
        return self.root / "checkouts" / key

    def _prepare_root(self) -> None:
        private_directory(self.root)
        filesystem = _filesystem(self.root)
        if filesystem not in SUPPORTED_FILESYSTEMS:
            raise recovery_error(
                "authority requires a verified supported local filesystem; "
                f"detected {filesystem!r}; supported: "
                + ", ".join(sorted(SUPPORTED_FILESYSTEMS)),
                filesystem=filesystem,
                supported_filesystems=sorted(SUPPORTED_FILESYSTEMS),
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

    @staticmethod
    def _record_lock(path: Path, *, create: bool = True) -> int:
        descriptor = os.open(
            path, os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600
        )
        try:
            held = os.fstat(descriptor)
            named = path.lstat()
            if (
                held.st_uid != os.geteuid()
                or held.st_mode & 0o077
                or held.st_nlink != 1
                or not stat.S_ISREG(held.st_mode)
                or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise recovery_error("authority lock has unsafe storage")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @contextmanager
    def _maintenance(self) -> Iterator[None]:
        descriptor = self._record_lock(self.root / "maintenance.lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def records(
        self,
        *,
        prune: bool = False,
        apply: bool = False,
        include_unrecorded: bool = False,
    ) -> dict[str, Any]:
        """Inspect or explicitly retire dead, clean, unbound physical records.

        The path is diagnostic evidence of staleness, never an authority key.
        Unsafe/ambiguous records are reported and preserved. Maintenance never
        validates an installation, creates a checkout binding or adopts history.
        """
        if apply and not prune:
            raise ValueError("only authority prune accepts --apply")
        with self._lock:
            if not self.root.exists():
                return {"root": str(self.root), "applied": apply, "records": []}
            self._prepare_root()
            with self.store.connection() as connection:
                bound = {
                    row[0]
                    for row in connection.execute(
                        "SELECT physical_key FROM authority_checkouts "
                        "UNION SELECT physical_key FROM checkout_regimes "
                        "UNION SELECT physical_key FROM legacy_claim_bindings "
                        "UNION SELECT physical_key FROM authority_journals"
                    )
                }
            records = []
            for directory in sorted((self.root / "checkouts").iterdir()):
                item: dict[str, Any] = {
                    "physical_key": directory.name,
                    "eligible": False,
                    "removed": False,
                    "bound_by_profile": directory.name in bound,
                }
                records.append(item)
                try:
                    with self._maintenance():
                        private_directory(directory)
                        descriptor = self._record_lock(
                            directory / "ownership.lock", create=False
                        )
                        try:
                            try:
                                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                item["reason"] = "owned"
                                continue
                            marker = private_read(directory / "marker.json")
                            owner = private_read(directory / "owner.json") or {}
                            item["checkout_path"] = owner.get("checkout_path")
                            item["owner_profile"] = owner.get("profile")
                            if item["bound_by_profile"]:
                                item["reason"] = "bound_by_profile"
                                continue
                            if (
                                marker is None
                                or not _supported_marker(marker)
                                or marker.get("physical_key") != directory.name
                                or marker.get("state") != "clean"
                                or type(marker.get("generation")) is not int
                                or marker["generation"] < 1
                                or marker.get("journal_id") is not None
                                or marker.get("journal_kind") is not None
                                or not isinstance(marker.get("binding"), dict)
                            ):
                                item["reason"] = "recovery_required"
                                continue
                            binding = marker["binding"]
                            if (
                                binding.get("version") != 1
                                or binding.get("object_format")
                                not in {"sha1", "sha256"}
                                or any(
                                    not isinstance(binding.get(name), dict)
                                    or any(
                                        type(binding[name].get(field)) is not int
                                        or binding[name][field] < 0
                                        for field in (
                                            "dev",
                                            "ino",
                                            "birth_ns",
                                            "generation",
                                        )
                                    )
                                    for name in ("root", "admin", "common")
                                )
                            ):
                                item["reason"] = "unverified_binding"
                                continue
                            try:
                                binding_key = hashlib.sha256(
                                    encode(
                                        {
                                            key: marker["binding"][key]
                                            for key in (
                                                "version",
                                                "root",
                                                "admin",
                                                "object_format",
                                            )
                                        }
                                    )
                                ).hexdigest()
                            except (KeyError, TypeError):
                                item["reason"] = "unverified_binding"
                                continue
                            if binding_key != directory.name:
                                item["reason"] = "unverified_binding"
                                continue
                            recorded = owner.get("checkout_path")
                            if recorded is None:
                                item["reason"] = "unrecorded_path"
                                if not include_unrecorded:
                                    continue
                            elif (
                                not isinstance(recorded, str)
                                or not Path(recorded).is_absolute()
                            ):
                                item["reason"] = "invalid_path"
                                continue
                            else:
                                try:
                                    physical = PhysicalCheckout.inspect(Path(recorded))
                                except LlmCoordError as exc:
                                    # Only absence or a provable changed identity
                                    # is stale. Unsupported filesystems, unsafe
                                    # metadata and permission errors aren't proof.
                                    if not isinstance(exc.__cause__, FileNotFoundError):
                                        item["reason"] = "identity_unverified"
                                        continue
                                else:
                                    try:
                                        if physical.key == directory.name:
                                            item["reason"] = "checkout_present"
                                            continue
                                    finally:
                                        physical.close()
                            if not set(p.name for p in directory.iterdir()) <= {
                                "ownership.lock",
                                "marker.json",
                                "owner.json",
                            }:
                                item["reason"] = "unexpected_contents"
                                continue
                            item["eligible"] = True
                            item["reason"] = (
                                "unrecorded_path"
                                if recorded is None
                                else "stale_checkout"
                            )
                            if prune and apply:
                                # The maintenance barrier prevents a contender
                                # opening this inode while it is retired. Never
                                # unlink a live/dirty lock to regain ownership.
                                for name in (
                                    "owner.json",
                                    "marker.json",
                                    "ownership.lock",
                                ):
                                    (directory / name).unlink(missing_ok=True)
                                directory.rmdir()
                                parent = os.open(
                                    directory.parent, os.O_RDONLY | os.O_DIRECTORY
                                )
                                try:
                                    os.fsync(parent)
                                finally:
                                    os.close(parent)
                                item["removed"] = True
                        finally:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                            os.close(descriptor)
                except (OSError, LlmCoordError) as exc:
                    item["reason"] = "unsafe_record"
                    item["error"] = str(exc)
            return {"root": str(self.root), "applied": apply, "records": records}

    def _metadata(self, instance: str, physical: PhysicalCheckout) -> dict[str, Any]:
        return {
            "version": 1,
            "profile": self.profile,
            "database_instance_id": instance,
            "boot_id": self.boot_id,
            "pid": os.getpid(),
            "process_start": self._start,
            "socket": str(self.socket),
            "acquired_at": time.time_ns() // 1_000_000,
            "checkout_path": str(physical.root),
        }

    def _validate(self, owner: _Owned) -> None:
        with self._lock:
            self._validate_owned(owner)

    def _validate_owned(self, owner: _Owned) -> None:
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

    def regime(self, root: Path) -> dict[str, Any]:
        """Observe the physical regime without creating ownership or a marker."""
        physical = PhysicalCheckout.inspect(root)
        try:
            # Marker writes precede their SQL generation. Read SQL first so a
            # concurrent publication cannot make a valid marker look stale.
            with self.store.connection() as connection:
                bindings = connection.execute(
                    "SELECT a.* FROM authority_checkouts a "
                    "LEFT JOIN checkouts c ON c.checkout_id=a.checkout_id "
                    "WHERE a.physical_key=? OR c.canonical_path=?",
                    (physical.key, str(physical.root)),
                ).fetchall()
                recorded = connection.execute(
                    "SELECT regime FROM checkout_regimes WHERE physical_key=?",
                    (physical.key,),
                ).fetchone()
            with self._observation_lock:
                physical.validate()
                owned = self._owned.get(physical.key)
                if owned is not None:
                    owned.physical.validate()
                    held = os.fstat(owned.descriptor)
                    named = (self._directory(physical.key) / "ownership.lock").lstat()
                    marker = private_read(self._directory(physical.key) / "marker.json")
                    if (
                        marker != owned.marker
                        or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)
                        or named.st_uid != os.geteuid()
                        or named.st_mode & 0o077
                        or named.st_nlink != 1
                        or not stat.S_ISREG(named.st_mode)
                    ):
                        raise recovery_error("authority regime ownership changed")
                else:
                    marker = private_read(self._directory(physical.key) / "marker.json")
                if any(
                    row["physical_key"] != physical.key
                    or row["binding_json"] != encode(physical.binding).decode()
                    for row in bindings
                ):
                    raise LlmCoordError(
                        ErrorCode.CHECKOUT_IDENTITY_CHANGED,
                        "registered physical checkout identity changed",
                    )
                binding = bindings[0] if bindings else None
                if marker is None and binding is not None:
                    raise recovery_error("authority marker is missing")
                if marker is not None and (
                    not _supported_marker(marker)
                    or marker.get("physical_key") != physical.key
                    or marker.get("binding") != physical.binding
                    or type(marker.get("generation")) is not int
                    or marker.get("state") not in {"clean", "dirty", "attention"}
                    or (
                        binding is not None
                        and marker["generation"] < binding["marker_generation"]
                    )
                ):
                    raise recovery_error("authority regime marker cannot be verified")
                observed = {"physical_key": physical.key, **_marker_regime(marker)}
                if (
                    recorded is not None
                    and _REGIME_RANKS[recorded["regime"]]
                    > _REGIME_RANKS[observed["regime"]]
                ):
                    # A durable cutover intent closes legacy admission before
                    # its marker advances. Until the marker catches up, every
                    # gate treats that pending conversion as transition.
                    observed["regime"] = "transition"
                return observed
        finally:
            physical.close()

    def assert_regime(
        self,
        root: Path,
        *,
        allowed: tuple[str, ...] = ("legacy",),
        generation: int | None = None,
    ) -> dict[str, Any]:
        """Final-boundary compatibility check; the caller retains its live pin."""
        result = self.regime(root)
        if result["regime"] not in allowed or (
            generation is not None and generation != result["generation"]
        ):
            raise LlmCoordError(
                ErrorCode.CUTOVER_BLOCKED
                if result["regime"] == "transition"
                else ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED,
                "execution is incompatible with the physical checkout regime",
                {"regime": result["regime"], "generation": result["generation"]},
            )
        return result

    def advance_regime(
        self,
        pin: OwnershipPin,
        *,
        regime: str,
        generation: int,
        checkpoint: str,
        intent_id: str,
    ) -> None:
        """Advance one already-journaled, exclusively pinned cutover."""
        owner = pin.owner
        with self._lock:
            self._validate(owner)
            current = _marker_regime(owner.marker)
            if (
                regime not in {"transition", "workspace"}
                or _REGIME_RANKS[regime] < _REGIME_RANKS[current["regime"]]
                or generation < current["generation"]
                or (regime != current["regime"] and generation <= current["generation"])
                or len(checkpoint) != 64
                or not intent_id
            ):
                raise recovery_error("checkout regime cannot move backwards")
            if (
                owner.marker["state"] == "dirty"
                and owner.marker.get("journal_id") != intent_id
            ):
                raise recovery_error(
                    "another unfinished journal blocks regime conversion"
                )
            if current["intent_id"] not in {None, intent_id}:
                raise recovery_error("another cutover owns the regime marker")
            with self.store.connection() as connection:
                journal = connection.execute(
                    "SELECT 1 FROM authority_journals WHERE journal_id=? "
                    "AND physical_key=? AND kind='regime_cutover' AND reference_id=?",
                    (intent_id, owner.physical.key, intent_id),
                ).fetchone()
            if journal is None:
                raise recovery_error("cutover journal proof is missing")
            self._write_marker(
                owner,
                "dirty",
                journal_id=intent_id,
                journal_kind="regime_cutover",
                regime_update={
                    "regime": regime,
                    "regime_generation": generation,
                    "regime_checkpoint": checkpoint,
                    "regime_intent_id": intent_id,
                },
            )

    def _write_marker(
        self,
        owner: _Owned,
        state: str,
        *,
        journal_id: str | None = None,
        journal_kind: str | None = None,
        regime_update: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            regime = (
                {
                    key: owner.marker[key]
                    for key in (
                        "regime",
                        "regime_generation",
                        "regime_checkpoint",
                        "regime_intent_id",
                    )
                }
                if owner.marker.get("version") == 2
                else {}
            )
            if regime_update is not None:
                regime.update(regime_update)
            marker = {
                "version": 2 if regime else 1,
                "physical_key": owner.physical.key,
                "binding": owner.physical.binding,
                "generation": int(owner.marker.get("generation", 0)) + 1,
                "state": state,
                "execution_contract": (
                    "workspace_v1"
                    if regime.get("regime") == "workspace"
                    else "transition_v1"
                    if regime
                    else "claim_backed_v1"
                ),
                **regime,
                "owner_profile": self.profile,
                "database_instance_id": self.installation.instance_id,
                "daemon_boot": self.boot_id,
                "checkout_id": owner.marker.get("checkout_id"),
                "journal_id": journal_id,
                "journal_kind": journal_kind,
            }
            with self._observation_lock:
                private_write(
                    self._directory(owner.physical.key) / "marker.json", marker
                )
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
        while True:
            try:
                return self._acquire(root, recovery=recovery, activity=activity)
            except _LiveMutationPending as live:
                # A live operation needs this lock for nested pins/validation.
                # Reinspect identity, binding and its final marker after it exits.
                live.completed.wait()

    def _acquire(self, root: Path, *, recovery: bool, activity: bool) -> OwnershipPin:
        with self._lock:
            physical = PhysicalCheckout.inspect(root)
            try:
                self._prepare_root()
                instance = self.installation.validate()
                prior = self._owned.get(physical.key)
                if prior is not None:
                    physical.close()
                    if (
                        prior.live_mutation is not None
                        and _MUTATION.get() != prior.physical.key
                    ):
                        raise _LiveMutationPending(prior.live_mutation)
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
                try:
                    # Serialize opening the inode with retirement. Once flock is
                    # held, pruning cannot retire this record even after this
                    # short directory-maintenance barrier has been released.
                    with self._maintenance():
                        private_directory(directory)
                        descriptor = self._record_lock(directory / "ownership.lock")
                        try:
                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BaseException:
                            os.close(descriptor)
                            raise
                except BlockingIOError as exc:
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
                        not _supported_marker(marker)
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
                    private_write(
                        directory / "owner.json", self._metadata(instance, physical)
                    )
                    with self._observation_lock:
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
                    "previous_database_instance_id": marker.get("database_instance_id"),
                    "execution_contract": marker.get("execution_contract"),
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
    def _live_mutation(self, owner: _Owned) -> Iterator[None]:
        completed = threading.Event()
        with self._lock:
            # Register before any dirty marker can become visible. This state
            # is process-local evidence of a live operation, never crash proof.
            prior = owner.live_mutation
            if prior is None:
                owner.live_mutation = completed
        if prior is not None:
            # Only a reentrant holder of mutation_lock can reach this branch.
            yield
            return
        try:
            yield
        finally:
            with self._lock:
                owner.live_mutation = None
                completed.set()

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
            if journal_kind.startswith("legacy_"):
                self.assert_regime(root, allowed=("legacy", "transition"))
            if _MUTATION.get() == owner.physical.key:
                yield Mutation(self, owner, journal_kind, reference_id)
                return
            with self._live_mutation(owner):
                if owner.marker["state"] == "clean":
                    journal_id = "authority_" + secrets.token_hex(16)
                    # Crash here leaves a durable dirty locator without its journal:
                    # acquisition must retain attention, never infer no effect.
                    self._write_marker(
                        owner, "dirty", journal_id=journal_id, journal_kind=journal_kind
                    )
                    with self.store.connection() as connection:
                        connection.execute(
                            "INSERT INTO authority_journals "
                            "VALUES (?,?,?,?, 'applying',?)",
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
        with (
            self.pin(root, recovery=True) as pin,
            pin.owner.mutation_lock,
            self._live_mutation(pin.owner),
        ):
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

                elif journal["kind"] == "regime_cutover":
                    intent = connection.execute(
                        (
                            "SELECT * FROM cutover_intents WHERE intent_id=? AND "
                            "physical_key=?"
                        ),
                        (journal["reference_id"], owner.physical.key),
                    ).fetchone()
                    proved = bool(
                        intent is not None
                        and intent["stage"] == "complete"
                        and owner.marker.get("regime") == "workspace"
                        and owner.marker.get("regime_intent_id") == intent["intent_id"]
                        and owner.marker.get("regime_generation")
                        == intent["workspace_generation"]
                        and owner.marker.get("regime_checkpoint")
                        == intent["settled_checkpoint"]
                        and journal["state"] == "confirmed"
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
                    or (not force and instant < owner.idle_since + IDLE_SECONDS)
                ):
                    continue
                with self.store.connection() as connection:
                    busy = connection.execute(
                        "SELECT 1 FROM claims c JOIN tasks t ON t.task_id=c.task_id "
                        "LEFT JOIN sessions s ON s.session_id=t.session_id "
                        "LEFT JOIN checkouts k ON k.checkout_id=s.checkout_id WHERE "
                        "c.state IN ('active_work','publishing','active_integration') "
                        "AND ((t.session_id IS NOT NULL AND k.canonical_path=?) OR "
                        "(t.session_id IS NULL AND (EXISTS("
                        "SELECT 1 FROM authority_journals j WHERE "
                        "j.kind='legacy_claim' "
                        "AND j.reference_id=c.claim_id AND j.physical_key=?) OR "
                        "EXISTS(SELECT 1 FROM authority_journals j JOIN "
                        "task_executions e ON e.execution_id=j.reference_id WHERE "
                        "j.kind='legacy_execution' AND e.claim_id=c.claim_id AND "
                        "j.physical_key=?) OR "
                        "(NOT EXISTS(SELECT 1 FROM authority_journals j WHERE "
                        "(j.kind='legacy_claim' AND j.reference_id=c.claim_id) OR "
                        "(j.kind='legacy_execution' AND j.reference_id IN "
                        "(SELECT execution_id FROM task_executions e WHERE "
                        "e.claim_id=c.claim_id))) AND "
                        "EXISTS(SELECT 1 FROM authority_checkouts a JOIN checkouts b "
                        "ON b.checkout_id=a.checkout_id WHERE a.physical_key=? AND "
                        "b.repository_id=t.repository_id))))) LIMIT 1",
                        (
                            str(owner.physical.root),
                            owner.physical.key,
                            owner.physical.key,
                            owner.physical.key,
                        ),
                    ).fetchone()
                if busy is None:
                    self._release(owner)

    def _release(self, owner: _Owned) -> None:
        with self._observation_lock:
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
                marker is not None and not _supported_marker(marker)
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
                **_marker_regime(marker),
                "execution_contract": marker.get("execution_contract")
                if marker
                else "claim_backed_v1",
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
                "detail": str(exc),
                **(exc.details or {} if isinstance(exc, LlmCoordError) else {}),
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
