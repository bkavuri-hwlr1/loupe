"""Explicit portable source exports with durable identity-based crash replay.

Only manifests, hashes and filesystem receipts enter SQLite. Bodies remain in
owner-private content-addressed files. No daemon/publication lock covers I/O.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from llm_cli.agent.source_policy import (
    ensure_safe_source,
    excluded_paths,
    runtime_path_matches,
)
from llm_cli.coordination.scopes import normalize_changed_path
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.ids import new_id
from llm_cli.paths import AppPaths
from llm_cli.storage.connection import immediate_transaction
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.authority import PhysicalCheckout, managed_pin
from llm_cli.workspace.batches import (
    BatchFile,
    _identity_from_json,
    candidate_target,
    result_identity,
)
from llm_cli.workspace.broker import (
    MAX_SHARED_TEXT_BYTES,
    load_candidate_content,
)
from llm_cli.workspace.hydration import _bounded_git
from llm_cli.workspace.identity import (
    ABSENT,
    FileIdentity,
    ObjectKind,
    content_identity,
    read_identified_path,
)
from llm_cli.workspace.patches import (
    export_manifest,
    portable_bundle,
    portable_patch,
    write_export_artifact,
)
from llm_cli.workspace.runtime_export import (
    _checkout_roots,
    _destination_parent,
    _relative_within,
)
from llm_cli.workspace.workflow import decode_files

if TYPE_CHECKING:
    from llm_cli.workspace.isolated import IsolatedWorkspaceManager

_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_KINDS = ("proposal", "candidate", "batch")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _unsafe(message: str, path: str | None = None) -> LlmCoordError:
    return LlmCoordError(
        ErrorCode.REPOSITORY_UNSAFE,
        message,
        {"path": path} if path is not None else None,
    )


def _private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        created = False
    current = path.lstat()
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_uid != os.geteuid()
        or current.st_mode & 0o077
    ):
        raise _unsafe("source export storage is not an owner-private directory")
    if created:
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


def _file_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _read_private(path: Path, digest: str, size: int | None = None) -> bytes:
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise _unsafe("source export body identity is invalid")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o077
            or before.st_nlink != 1
            or before.st_size > _MAX_ARTIFACT_BYTES
            or (size is not None and before.st_size != size)
        ):
            raise _unsafe("source export body identity changed")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            body = handle.read(before.st_size + 1)
        after = os.fstat(descriptor)
        named = path.lstat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (
            len(body) != before.st_size
            or _hash(body) != digest
            or any(
                getattr(before, field) != getattr(observed, field)
                for observed in (after, named)
                for field in fields
            )
        ):
            raise _unsafe("source export body identity changed")
        return body
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class _Source:
    kind: str
    identifier: str
    checkout_id: str
    session_id: str
    workspace_id: str
    root: Path
    runtime_paths: tuple[str, ...]
    payload: dict[str, Any]
    attempt: int | None = None


class SourceExporter:
    def __init__(
        self,
        store: ControlStore,
        candidate_dir: Path,
        storage: Path,
        *,
        isolated: IsolatedWorkspaceManager | None = None,
        protected_roots: tuple[Path, ...] = (),
        fault: Callable[[str], None] = lambda phase: None,
    ) -> None:
        self.store, self.candidate_dir, self.storage = store, candidate_dir, storage
        self.isolated, self.protected_roots, self.fault = (
            isolated,
            protected_roots,
            fault,
        )
        self.bodies, self.artifacts = storage / "bodies", storage / "artifacts"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        # This dedicated file lock serializes only explicit exports, across
        # threads/restarts. It never gates task dispatch or source publication.
        _private_directory(self.storage)
        descriptor = os.open(
            self.storage / "exports.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            value = os.fstat(descriptor)
            if (
                not stat.S_ISREG(value.st_mode)
                or value.st_uid != os.geteuid()
                or value.st_mode & 0o077
                or value.st_nlink != 1
            ):
                raise _unsafe("source export lock identity changed")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            for directory in (self.bodies, self.artifacts):
                _private_directory(directory)
                # A killed artifact writer can leave only private scratch files.
                # The export lock proves no live writer uses this namespace.
                for entry in directory.iterdir():
                    if entry.name.startswith(".switch_export_") and entry.name.endswith(
                        ".tmp"
                    ):
                        scratch = entry.lstat()
                        if (
                            stat.S_ISREG(scratch.st_mode)
                            and scratch.st_uid == os.geteuid()
                            and not scratch.st_mode & 0o077
                            and scratch.st_nlink == 1
                        ):
                            entry.unlink()
            yield
        finally:
            os.close(descriptor)

    def get(self, intent_id: str) -> dict[str, Any] | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM export_intents_v2 WHERE export_intent_id=?", (intent_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def _update(self, row: dict[str, Any], **fields: Any) -> dict[str, Any]:
        allowed = {
            "state",
            "artifact_hash",
            "artifact_size",
            "stage_parent",
            "stage_name",
            "stage_dev",
            "stage_ino",
            "parent_dev",
            "parent_ino",
            "destination_dev",
            "destination_ino",
            "destination_parent_dev",
            "destination_parent_ino",
            "terminal_at",
        }
        if not fields or not set(fields) <= allowed:
            raise ValueError("invalid source export transition")
        with self.store.connection() as connection, immediate_transaction(connection):
            changed = connection.execute(
                "UPDATE export_intents_v2 SET "
                + ",".join(field + "=?" for field in fields)
                + ",state_version=state_version+1,updated_at=? "
                "WHERE export_intent_id=? AND state_version=?",
                (
                    *fields.values(),
                    self.store._now(None),
                    row["export_intent_id"],
                    row["state_version"],
                ),
            ).rowcount
            if changed != 1:
                raise _unsafe("source export intent changed; inspect before retrying")
        current = self.get(str(row["export_intent_id"]))
        assert current is not None
        return current

    def _source(self, identifier: str, kind: str | None) -> _Source:
        with self.store.connection() as connection:
            proposal = connection.execute(
                "SELECT f.*,t.session_id FROM task_workflows f JOIN tasks t "
                "ON t.task_id=f.task_id AND t.attempt=f.attempt WHERE f.task_id=? "
                "AND f.status IN ('awaiting_review','cancelled') "
                "AND f.proposal_json IS NOT NULL",
                (identifier,),
            ).fetchone()
            candidate = connection.execute(
                "SELECT * FROM workspace_candidates WHERE candidate_id=? "
                "AND state IN ('staged','diverged')",
                (identifier,),
            ).fetchone()
            batch = connection.execute(
                "SELECT * FROM workspace_batches WHERE batch_id=? "
                "AND state='published'",
                (identifier,),
            ).fetchone()
            choices = {"proposal": proposal, "candidate": candidate, "batch": batch}
            found = [
                name
                for name, value in choices.items()
                if value is not None and (kind is None or kind == name)
            ]
            if len(found) != 1:
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID,
                    "choose --kind for an ambiguous ID"
                    if found
                    else "no held proposal, staged candidate, or published batch "
                    "has that ID",
                )
            selected = found[0]
            row = dict(choices[selected])
            owner = self.store.get_session(str(row["session_id"]))
            if owner is None:
                raise _unsafe("source export owner is unavailable")
            workspace_id = row.get("workspace_id")
            runtime: tuple[str, ...] = ()
            if selected == "proposal":
                binding = connection.execute(
                    "SELECT workspace_id FROM switch_proposal_bindings "
                    "WHERE task_id=? AND attempt=?",
                    (identifier, row["attempt"]),
                ).fetchone()
                execution = (
                    connection.execute(
                        "SELECT workspace_id FROM all_task_executions "
                        "WHERE execution_id=?",
                        (row["execution_id"],),
                    ).fetchone()
                    if row["execution_id"]
                    else None
                )
                workspace_id = (
                    binding[0]
                    if binding
                    else execution[0]
                    if execution
                    else owner.workspace_id
                )
                configuration = json.loads(row["config_json"])
                runtime = tuple(configuration.get("runtime_paths", ()))
            workspace = self.store.get_workspace(str(workspace_id))
            if workspace is None or workspace.checkout_id != owner.checkout_id:
                raise _unsafe("source export workspace binding is unavailable")
            return _Source(
                selected,
                identifier,
                owner.checkout_id,
                owner.session_id,
                workspace.workspace_id,
                Path(workspace.canonical_path),
                runtime,
                row,
                int(row["attempt"]) if selected == "proposal" else None,
            )

    def _before(
        self,
        checkout_id: str,
        root: Path,
        path: str,
        kind: ObjectKind,
        mode: str,
        digest: str,
        size: int | None,
    ) -> bytes | None:
        def matches(body: bytes | None) -> bool:
            return (
                body is not None
                and (size is None or len(body) == size)
                and content_identity(kind, mode, body) == digest
            )

        hashes: list[str] = []
        with self.store.connection() as connection:
            hashes.extend(
                str(row[0])
                for row in connection.execute(
                    "SELECT content_hash FROM knowledge_external_versions WHERE "
                    "checkout_id=? AND relative_path=? AND "
                    "json_extract(identity_json,'$.digest')=? "
                    "AND content_hash IS NOT NULL LIMIT 1",
                    (checkout_id, path, digest),
                )
            )
            hashes.extend(
                str(row[0])
                for row in connection.execute(
                    "SELECT json_extract(f.value,'$.content_hash') "
                    "FROM workspace_batches b,"
                    "json_each(b.request_json,'$.files') f WHERE b.checkout_id=? "
                    "AND b.state='published' "
                    "AND json_extract(f.value,'$.relative_path')=? "
                    "AND json_extract(f.value,'$.result.digest')=? "
                    "AND json_extract(f.value,'$.content_hash') IS NOT NULL LIMIT 1",
                    (checkout_id, path, digest),
                )
            )
        for candidate_hash in hashes:
            with suppress(OSError, ValueError):
                body = load_candidate_content(self.candidate_dir, candidate_hash)
                if matches(body):
                    return body
        with suppress(OSError, ValueError):
            identity, observed_body = read_identified_path(candidate_target(root, path))
            if identity.kind == kind and matches(observed_body):
                return observed_body
        with suppress(OSError, ValueError):
            code, body = _bounded_git(
                root,
                ["cat-file", "blob", "HEAD:" + path],
                deadline=time.monotonic() + 30,
                limit=size if size is not None else MAX_SHARED_TEXT_BYTES,
            )
            if code == 0 and matches(body):
                return body
        return None

    def _load(self, source: _Source) -> tuple[BatchFile, ...]:
        if source.kind == "proposal":
            files = decode_files(str(source.payload["proposal_json"]))
        elif source.kind == "batch":
            entries = json.loads(source.payload["request_json"])["files"]
            loaded = []
            for entry in entries:
                expected = _identity_from_json(entry["result"])
                item = BatchFile(
                    entry["relative_path"],
                    _identity_from_json(entry["base"]),
                    load_candidate_content(self.candidate_dir, entry["content_hash"])
                    if entry["content_hash"] is not None
                    else None,
                    expected.mode,
                    load_candidate_content(self.candidate_dir, entry["original_hash"])
                    if entry.get("original_hash") is not None
                    else None,
                    expected.permissions,
                    entry.get("binary", False),
                )
                if result_identity(item) != expected:
                    raise _unsafe(
                        "published source identity changed", item.relative_path
                    )
                loaded.append(item)
            files = tuple(loaded)
        else:
            row = source.payload
            kind = ObjectKind(row["base_kind"])
            before = (
                None
                if kind is ObjectKind.ABSENT
                else self._before(
                    source.checkout_id,
                    source.root,
                    row["relative_path"],
                    kind,
                    row["base_mode"],
                    row["base_digest"],
                    None,
                )
            )
            if kind is not ObjectKind.ABSENT and before is None:
                raise _unsafe(
                    "exact candidate before bytes are unavailable", row["relative_path"]
                )
            base = (
                ABSENT
                if kind is ObjectKind.ABSENT
                else FileIdentity(
                    kind,
                    row["base_mode"],
                    row["base_digest"],
                    len(before or b""),
                )
            )
            content = load_candidate_content(self.candidate_dir, row["content_hash"])
            if (
                len(content) != row["byte_count"]
                or content_identity(ObjectKind.REGULAR, row["result_mode"], content)
                != row["result_digest"]
            ):
                raise _unsafe("candidate source identity changed", row["relative_path"])
            files = (
                BatchFile(
                    row["relative_path"],
                    base,
                    content,
                    row["result_mode"],
                    before,
                ),
            )
        completed = []
        for item in files:
            if item.original is None and item.base.kind not in {
                ObjectKind.ABSENT,
                ObjectKind.DIRECTORY_MARKER,
            }:
                before = self._before(
                    source.checkout_id,
                    source.root,
                    item.relative_path,
                    item.base.kind,
                    item.base.mode,
                    item.base.digest,
                    item.base.size,
                )
                item = BatchFile(
                    item.relative_path,
                    item.base,
                    item.content,
                    item.mode,
                    before,
                    item.permissions,
                    item.binary,
                )
            completed.append(item)
        return tuple(completed)

    def _privacy(
        self, root: Path, files: tuple[BatchFile, ...], runtime: tuple[str, ...]
    ) -> None:
        for path in runtime:
            if not isinstance(path, str) or normalize_changed_path(path) != path:
                raise _unsafe("source export runtime policy is invalid")
        paths = [item.relative_path for item in files]
        excluded = excluded_paths(root, paths) if root.is_dir() else set()
        for item in files:
            if item.relative_path in excluded or runtime_path_matches(
                item.relative_path, runtime
            ):
                raise _unsafe(
                    "runtime or excluded source cannot be exported", item.relative_path
                )
            for body in (item.original, item.content):
                if body is not None:
                    try:
                        ensure_safe_source(root, body)
                    except LlmCoordError:
                        raise _unsafe(
                            "screened source cannot be exported", item.relative_path
                        ) from None

    def _prime_privacy(self, workspace_id: str) -> None:
        if self.isolated is None:
            return
        workspace = self.store.get_workspace(workspace_id)
        if (
            workspace is None
            or workspace.mode != "isolated"
            or workspace.state != "active"
        ):
            return
        state = self.isolated.validate(workspace_id, source_manifest=False)
        if state.runtime_files:
            from llm_cli.workspace.runtime import private_output_values

            values = private_output_values(
                Path(workspace.canonical_path), state.runtime_files, strict_private=True
            )
            if values:
                # Reuse C14's public-opening-source exceptions and historical
                # receipts; actual private values never enter SQL or logs.
                self.isolated.remember_process_values(workspace, values)

    def _freeze(
        self, source: _Source
    ) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        runtime = source.runtime_paths
        workspace = self.store.get_workspace(source.workspace_id)
        if (
            self.isolated is not None
            and workspace is not None
            and workspace.mode == "isolated"
        ):
            state = self.isolated.validate(
                workspace.workspace_id, source_manifest=False
            )
            runtime = tuple(sorted(set(runtime) | set(state.runtime_paths)))
        files = self._load(source)
        self._privacy(source.root, files, runtime)
        manifest = export_manifest(files)
        for item in files:
            for body in (item.original, item.content):
                if body is not None:
                    write_export_artifact(
                        self.bodies, _hash(body), body, lambda phase: None
                    )
        return manifest, runtime

    def _cached_files(self, row: dict[str, Any]) -> tuple[BatchFile, ...]:
        files = []
        for entry in json.loads(row["source_manifest_json"]):
            before, after = (
                _identity_from_json(entry["before"]),
                _identity_from_json(entry["after"]),
            )
            old = (
                _read_private(
                    self.bodies / entry["before_body"],
                    entry["before_body"],
                    before.size,
                )
                if entry["before_body"]
                else None
            )
            body = (
                _read_private(
                    self.bodies / entry["after_body"], entry["after_body"], after.size
                )
                if entry["after_body"]
                else b""
                if after.kind is ObjectKind.DIRECTORY_MARKER
                else None
            )
            item = BatchFile(
                entry["path"],
                before,
                body,
                after.mode,
                old,
                after.permissions,
                entry["binary"],
            )
            if result_identity(item) != after:
                raise _unsafe(
                    "frozen export source identity changed", item.relative_path
                )
            files.append(item)
        result = tuple(files)
        export_manifest(result)
        return result

    @contextmanager
    def _activity(self, workspace_id: str) -> Iterator[None]:
        with ExitStack() as activity:
            if self.isolated is not None:
                with self.isolated.lock:
                    workspace = self.store.get_workspace(workspace_id)
                    if (
                        workspace is not None
                        and workspace.mode == "isolated"
                        and workspace.state == "active"
                    ):
                        activity.enter_context(
                            self.isolated.runtime.activity(workspace_id)
                        )
            yield

    def export(
        self,
        identifier: str,
        destination: Path,
        *,
        intent_id: str,
        strategy: str = "patch",
        kind: str | None = None,
        into_checkout: bool = False,
    ) -> dict[str, Any]:
        try:
            return self._export(
                identifier,
                destination,
                intent_id=intent_id,
                strategy=strategy,
                kind=kind,
                into_checkout=into_checkout,
            )
        except LlmCoordError:
            raise
        except FileExistsError:
            raise _unsafe(
                "source export destination or staging already exists; "
                "inspect before retrying"
            ) from None
        except (OSError, ValueError, KeyError, TypeError):
            raise _unsafe(
                "source export record or artifact could not be verified; "
                "inspect before retrying"
            ) from None

    def _export(
        self,
        identifier: str,
        destination: Path,
        *,
        intent_id: str,
        strategy: str = "patch",
        kind: str | None = None,
        into_checkout: bool = False,
    ) -> dict[str, Any]:
        if (
            not identifier
            or len(identifier) > 256
            or not intent_id
            or len(intent_id) > 128
            or strategy not in ("patch", "bundle")
            or kind not in (None, *_KINDS)
            or type(into_checkout) is not bool
            or not destination.is_absolute()
        ):
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID, "invalid source export request"
            )
        request_hash = _hash(
            _json(
                {
                    "id": identifier,
                    "kind": kind,
                    "format": strategy,
                    "destination": str(destination),
                    "into_checkout": into_checkout,
                }
            ).encode()
        )
        with self._lock():
            row = self.get(intent_id)
            if row is not None and row["canonical_request_hash"] != request_hash:
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID,
                    "export intent is already bound to different arguments",
                )
            source = None if row else self._source(identifier, kind)
            checkout = self.store.get_checkout(
                str(row["checkout_id"]) if row else source.checkout_id if source else ""
            )
            if checkout is None:
                raise _unsafe("source export checkout is unavailable")
            root = Path(checkout.canonical_path)
            workspace_id = (
                str(row["source_workspace_id"])
                if row
                else source.workspace_id
                if source
                else ""
            )
            with managed_pin(self.store, root), self._activity(workspace_id):
                self._prime_privacy(workspace_id)
                if row is None:
                    assert source is not None
                    manifest, runtime = self._freeze(source)
                    now = self.store._now(None)
                    with (
                        self.store.connection() as connection,
                        immediate_transaction(connection),
                    ):
                        connection.execute(
                            "INSERT INTO export_intents_v2("
                            "export_intent_id,idempotency_key,"
                            "canonical_request_hash,checkout_id,session_id,source_workspace_id,"
                            "source_kind,source_id,source_attempt,source_root,runtime_paths_json,"
                            "source_manifest_json,strategy,destination,into_checkout,"
                            "state,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'requested',?,?)",
                            (
                                intent_id,
                                intent_id,
                                request_hash,
                                source.checkout_id,
                                source.session_id,
                                source.workspace_id,
                                source.kind,
                                source.identifier,
                                source.attempt,
                                str(source.root),
                                _json(runtime),
                                _json(manifest),
                                strategy,
                                str(destination),
                                int(into_checkout),
                                now,
                                now,
                            ),
                        )
                    self.fault("intent_recorded")
                    row = self.get(intent_id)
                    assert row is not None
                files = self._cached_files(row)
                self._privacy(
                    Path(row["source_root"]),
                    files,
                    tuple(json.loads(row["runtime_paths_json"])),
                )
                if row["state"] == "requested":
                    if strategy == "patch":
                        missing = next(
                            (
                                item.relative_path
                                for item in files
                                if item.base.kind
                                not in {ObjectKind.ABSENT, ObjectKind.DIRECTORY_MARKER}
                                and item.original is None
                            ),
                            None,
                        )
                        if missing is not None:
                            raise _unsafe(
                                "exact before bytes are unavailable; "
                                "use a bundle with a new intent",
                                missing,
                            )
                    body = (
                        portable_patch(files)
                        if strategy == "patch"
                        else portable_bundle(files)
                    )
                    if strategy == "patch" and not body:
                        raise _unsafe(
                            "Git patches cannot represent directory-only changes; "
                            "use a bundle with a new intent"
                        )
                    if len(body) > _MAX_ARTIFACT_BYTES:
                        raise _unsafe("source export artifact exceeds 64 MiB")
                    digest = _hash(body)
                    write_export_artifact(self.artifacts, digest, body, self.fault)
                    row = self._update(
                        row,
                        state="artifact_ready",
                        artifact_hash=digest,
                        artifact_size=len(body),
                    )
                    self.fault("artifact_recorded")
                body = _read_private(
                    self.artifacts / row["artifact_hash"],
                    row["artifact_hash"],
                    row["artifact_size"],
                )
                return self._publish(row, body, root, destination, into_checkout)

    def _publish(
        self,
        row: dict[str, Any],
        body: bytes,
        root: Path,
        destination: Path,
        into_checkout: bool,
    ) -> dict[str, Any]:
        with ExitStack() as stack:
            parent, leaf, verify, ancestry = stack.enter_context(
                _destination_parent(destination, administrative=True)
            )
            parent_identity = _file_identity(os.fstat(parent))
            if row["destination_parent_dev"] is None:
                row = self._update(
                    row,
                    destination_parent_dev=parent_identity[0],
                    destination_parent_ino=parent_identity[1],
                )
            elif parent_identity != (
                row["destination_parent_dev"],
                row["destination_parent_ino"],
            ):
                raise _unsafe("source export destination parent changed")
            with self.store.connection() as connection:
                protected = [
                    Path(value[0])
                    for value in connection.execute(
                        "SELECT canonical_path FROM workspaces WHERE mode='isolated' "
                        "UNION SELECT git_common_dir FROM checkouts "
                        "UNION SELECT git_dir FROM workspaces WHERE git_dir IS NOT NULL"
                    )
                ]
            for reserved in (*self.protected_roots, self.storage, *protected):
                if _relative_within(destination, reserved, ancestry) is not None:
                    raise _unsafe(
                        "source export destination is coordinator "
                        "or private workspace storage"
                    )
            checkouts = _checkout_roots(
                root,
                tuple(
                    Path(value.canonical_path) for value in self.store.list_checkouts()
                ),
                time.monotonic() + 30,
                4 * 1024 * 1024,
            )
            target_root = next(
                (
                    checkout
                    for checkout in checkouts
                    if _relative_within(destination, checkout, ancestry) is not None
                ),
                None,
            )
            stage_parent, stage_path = parent, destination.parent

            def no_stage_check() -> None:
                return

            verify_stage: Callable[[], None] = no_stage_check
            if target_root is not None:
                if not into_checkout:
                    raise _unsafe(
                        "source export into a checkout requires --into-checkout"
                    )
                stack.enter_context(managed_pin(self.store, target_root))
                physical = PhysicalCheckout.inspect(target_root)
                stack.callback(physical.close)
                private_stage = physical.admin / "loupe-source-exports"
                _private_directory(private_stage)
                stage_parent, _, verify_stage, _ = stack.enter_context(
                    _destination_parent(private_stage / "stage", administrative=True)
                )
                stage_path = private_stage
                if os.fstat(stage_parent).st_dev != os.fstat(parent).st_dev:
                    raise _unsafe(
                        "checkout source export needs same-filesystem private staging; "
                        "export outside the checkout"
                    )
            if row["state"] == "exported":
                self._verify_destination(row, parent, leaf, body)
                self._cleanup_stage(row)
                return self._result(row)
            existing = None
            with suppress(FileNotFoundError):
                existing = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if existing is not None:
                if row["state"] == "prepared" and _file_identity(existing) == (
                    row["stage_dev"],
                    row["stage_ino"],
                ):
                    self._verify_destination(row, parent, leaf, body, prepared=True)
                    row = self._update(
                        row,
                        state="exported",
                        destination_dev=existing.st_dev,
                        destination_ino=existing.st_ino,
                        terminal_at=self.store._now(None),
                    )
                    self._cleanup_stage(row)
                    return self._result(row)
                raise _unsafe(
                    "source export destination already exists; choose a new path"
                )
            if row["state"] == "prepared":
                self._verify_stage(row, stage_parent, stage_path, body)
            else:
                name = (
                    row["stage_name"]
                    if row["state"] == "writing"
                    else "." + new_id("source_export") + ".tmp"
                )
                if row["state"] == "writing":
                    self._verify_stage_location(row, stage_parent, stage_path)
                    descriptor = os.open(
                        name,
                        os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=stage_parent,
                    )
                    try:
                        existing_stage = os.fstat(descriptor)
                        if (
                            _file_identity(existing_stage)
                            != (row["stage_dev"], row["stage_ino"])
                            or not stat.S_ISREG(existing_stage.st_mode)
                            or existing_stage.st_uid != os.geteuid()
                            or existing_stage.st_mode & 0o077
                            or existing_stage.st_nlink != 1
                            or existing_stage.st_size > len(body)
                        ):
                            raise _unsafe("source export staging identity changed")
                        with os.fdopen(
                            descriptor, "rb", closefd=False
                        ) as prefix_reader:
                            prefix = prefix_reader.read(len(body) + 1)
                        observed = os.fstat(descriptor)
                        named = os.stat(
                            name, dir_fd=stage_parent, follow_symlinks=False
                        )
                        if not body.startswith(prefix) or any(
                            getattr(existing_stage, key) != getattr(current, key)
                            for current in (observed, named)
                            for key in (
                                "st_dev",
                                "st_ino",
                                "st_size",
                                "st_mtime_ns",
                                "st_ctime_ns",
                            )
                        ):
                            raise _unsafe("source export staging contents changed")
                        os.ftruncate(descriptor, 0)
                        os.lseek(descriptor, 0, os.SEEK_SET)
                    except BaseException:
                        os.close(descriptor)
                        raise
                else:
                    descriptor = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=stage_parent,
                    )
                    try:
                        value = os.fstat(descriptor)
                        parent_value = os.fstat(stage_parent)
                        os.fsync(stage_parent)
                        row = self._update(
                            row,
                            state="writing",
                            stage_parent=str(stage_path),
                            stage_name=name,
                            stage_dev=value.st_dev,
                            stage_ino=value.st_ino,
                            parent_dev=parent_value.st_dev,
                            parent_ino=parent_value.st_ino,
                        )
                        self.fault("stage_recorded")
                    except BaseException:
                        os.close(descriptor)
                        raise
                with os.fdopen(descriptor, "wb") as handle:
                    split = max(1, len(body) // 2)
                    handle.write(body[:split])
                    handle.flush()
                    self.fault("stage_partial")
                    handle.write(body[split:])
                    handle.flush()
                    os.fsync(handle.fileno())
                self._verify_stage(row, stage_parent, stage_path, body)
                row = self._update(row, state="prepared")
                self.fault("destination_prepared")
            verify()
            verify_stage()
            if self.store.authority is not None:
                self.store.authority.validate_current()
            os.link(
                row["stage_name"],
                leaf,
                src_dir_fd=stage_parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
            os.fsync(parent)
            verify()
            verify_stage()
            self.fault("destination_published")
            self._verify_destination(row, parent, leaf, body, prepared=True)
            row = self._update(
                row,
                state="exported",
                destination_dev=row["stage_dev"],
                destination_ino=row["stage_ino"],
                terminal_at=self.store._now(None),
            )
            self.fault("export_recorded")
            self._cleanup_stage(row)
            return self._result(row)

    def _verify_stage_location(
        self, row: dict[str, Any], parent: int, path: Path
    ) -> None:
        if str(path) != row["stage_parent"] or _file_identity(os.fstat(parent)) != (
            row["parent_dev"],
            row["parent_ino"],
        ):
            raise _unsafe("source export staging parent changed")
        if Path(str(row["stage_name"])).name != row["stage_name"]:
            raise _unsafe("source export staging name is invalid")

    def _verify_stage(
        self, row: dict[str, Any], parent: int, path: Path, body: bytes
    ) -> None:
        self._verify_stage_location(row, parent, path)
        value = os.stat(row["stage_name"], dir_fd=parent, follow_symlinks=False)
        if _file_identity(value) != (row["stage_dev"], row["stage_ino"]):
            raise _unsafe("source export staging identity changed")
        self._verify_file(parent, row["stage_name"], row, body, prepared=True)

    def _verify_destination(
        self,
        row: dict[str, Any],
        parent: int,
        leaf: str,
        body: bytes,
        *,
        prepared: bool = False,
    ) -> None:
        self._verify_file(parent, leaf, row, body, prepared=prepared)

    def _verify_file(
        self,
        parent: int,
        leaf: str,
        row: dict[str, Any],
        body: bytes,
        *,
        prepared: bool,
    ) -> None:
        descriptor = os.open(
            leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            before = os.fstat(descriptor)
            expected = (
                (row["stage_dev"], row["stage_ino"])
                if prepared
                else (row["destination_dev"], row["destination_ino"])
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_mode & 0o077
                or _file_identity(before) != expected
                or before.st_size != len(body)
            ):
                raise _unsafe("source export destination identity changed")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                actual = handle.read(len(body) + 1)
            after = os.fstat(descriptor)
            named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if actual != body or any(
                getattr(before, key) != getattr(value, key)
                for value in (after, named)
                for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            ):
                raise _unsafe("source export destination identity changed")
        finally:
            os.close(descriptor)

    def _cleanup_stage(self, row: dict[str, Any]) -> None:
        if not row["stage_parent"] or not row["stage_name"]:
            return
        with _destination_parent(
            Path(row["stage_parent"]) / row["stage_name"], administrative=True
        ) as (parent, name, verify, _):
            self._verify_stage_location(row, parent, Path(row["stage_parent"]))
            with suppress(FileNotFoundError):
                value = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if _file_identity(value) != (row["stage_dev"], row["stage_ino"]):
                    raise _unsafe("source export staging identity changed")
                verify()
                os.unlink(name, dir_fd=parent)
                os.fsync(parent)

    def _result(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "export_intent_id": row["export_intent_id"],
            "state": "exported",
            "source_kind": row["source_kind"],
            "source_id": row["source_id"],
            "format": row["strategy"],
            "destination": row["destination"],
            "files": len(json.loads(row["source_manifest_json"])),
            "bytes": row["artifact_size"],
            "sha256": row["artifact_hash"],
        }


def export_source(
    store: ControlStore,
    paths: AppPaths,
    params: Mapping[str, Any],
    isolated: IsolatedWorkspaceManager | None = None,
) -> dict[str, Any]:
    required = ("identifier", "destination", "export_intent_id")
    if any(
        not isinstance(params.get(name), str) or not params[name] for name in required
    ):
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID,
            "source export requires ID, destination, and intent",
        )
    kind, strategy = params.get("kind"), params.get("format", "patch")
    if kind not in (None, *_KINDS) or strategy not in ("patch", "bundle"):
        raise LlmCoordError(
            ErrorCode.CONFIG_INVALID, "invalid source export kind or format"
        )
    protected: tuple[Path, ...] = (
        paths.config_dir,
        paths.data_dir,
        paths.state_dir,
        paths.runtime_dir,
        paths.runtime_dir.parent,
        paths.data_dir.parent.parent,
        paths.state_dir.parent.parent,
    )
    if store.authority is not None:
        protected += (store.authority.root,)
    return SourceExporter(
        store,
        paths.candidate_dir,
        paths.data_dir / "source-exports",
        isolated=isolated,
        protected_roots=protected,
    ).export(
        params["identifier"],
        Path(params["destination"]),
        intent_id=params["export_intent_id"],
        kind=kind,
        strategy=strategy,
        into_checkout=params.get("into_checkout", False),
    )
