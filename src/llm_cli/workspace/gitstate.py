"""Read-only Git safety checks and observations at existing publication boundaries.

Git's index is never locked or edited here. A preflight is advisory with respect
to a concurrent Git process, so exact file journals remain the write authority.
The postflight detects that race and forces reconciliation before another apply.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
import weakref
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from llm_cli.coordination.scopes import normalize_changed_path
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.storage.connection import immediate_transaction

if TYPE_CHECKING:
    from llm_cli.storage.control import ControlStore

_OPERATIONS = (
    "index.lock",
    "rebase-merge",
    "rebase-apply",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "sequencer",
)
_MAX_INDEX = 64 * 1024 * 1024
_MAX_PATHS = 100_000
_FAILED: weakref.WeakKeyDictionary[ControlStore, set[Path]] = (
    weakref.WeakKeyDictionary()
)
_SEEN: weakref.WeakKeyDictionary[ControlStore, set[Path]] = weakref.WeakKeyDictionary()
_LOG = logging.getLogger(__name__)


def _git(root: Path, *args: str, allow_missing: bool = False) -> bytes:
    try:
        result = run_git(root, args, check=False, text=False, remaining=lambda: 10.0)
    except LlmCoordError as exc:
        raise LlmCoordError(
            ErrorCode.GIT_OPERATION_ACTIVE,
            "Git state could not be verified; retry publication",
            {"retryable": True},
        ) from exc
    if result.returncode and not allow_missing:
        raise LlmCoordError(
            ErrorCode.GIT_OPERATION_ACTIVE,
            "Git state could not be verified; retry after the operation finishes",
            {"retryable": True},
        )
    return cast(bytes, result.stdout) if result.returncode == 0 else b""


def _admin(root: Path) -> Path:
    dot = root / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        raw = dot.read_text().removesuffix("\n")
        if raw.startswith("gitdir: "):
            return (root / raw[8:]).resolve()
    raise LlmCoordError(
        ErrorCode.REPOSITORY_UNSAFE, "checkout Git directory is unavailable"
    )


def _digest(path: Path, *, maximum: int = _MAX_INDEX) -> str:
    try:
        with path.open("rb") as handle:
            content = handle.read(maximum + 1)
    except FileNotFoundError:
        return "absent"
    if len(content) > maximum:
        raise LlmCoordError(
            ErrorCode.GIT_OPERATION_ACTIVE,
            "Git state exceeds the safety budget",
            {"retryable": True},
        )
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class GitFingerprint:
    head: str
    index: str
    operations: tuple[tuple[str, str], ...]


def fingerprint(root: Path) -> GitFingerprint:
    admin = _admin(root)
    operations = []
    for name in _OPERATIONS:
        marker = admin / name
        if marker.is_dir():
            # Directory identity and bounded contents cover sequencer/rebase updates.
            values = [
                (p.name, _digest(p, maximum=1024 * 1024))
                for p in sorted(marker.iterdir())
                if p.is_file()
            ]
            operations.append((name, _encode(values)))
        elif marker.exists() or marker.is_symlink():
            operations.append((name, _digest(marker, maximum=1024 * 1024)))
    return GitFingerprint(
        _git(root, "rev-parse", "--verify", "HEAD", allow_missing=True)
        .decode()
        .strip(),
        _digest(admin / "index"),
        tuple(operations),
    )


def preflight(
    root: Path,
    paths: Sequence[str],
    *,
    refuse_staged_paths: bool = False,
    case_insensitive_filesystem: bool = False,
) -> tuple[GitFingerprint, tuple[str, ...]]:
    """Check the real worktree index, including linked-worktree administration."""
    normalized = tuple(normalize_changed_path(path) for path in paths)
    prefix = ":(literal,icase)" if case_insensitive_filesystem else ":(literal)"
    pathspecs = tuple(prefix + path for path in normalized)
    before = fingerprint(root)
    if before.operations:
        raise LlmCoordError(
            ErrorCode.GIT_OPERATION_ACTIVE,
            "Git operation is active; retry publication after it finishes",
            {"retryable": True, "operations": [name for name, _ in before.operations]},
        )
    staged: dict[str, tuple[str, str]] = {}
    unmerged = []
    for entry in _git(
        root,
        "ls-files",
        "--stage",
        "-z",
        "--",
        *pathspecs,
    ).split(b"\0"):
        if not entry:
            continue
        header, path_bytes = entry.split(b"\t", 1)
        mode, oid, stage = header.decode().split()
        path = os.fsdecode(path_bytes)
        if stage != "0":
            unmerged.append(path)
        else:
            staged[path] = (mode, oid)
    if unmerged:
        raise LlmCoordError(
            ErrorCode.INDEX_PATH_UNMERGED,
            "publication paths have unresolved Git index stages",
            {"retryable": True, "paths": sorted(set(unmerged))},
        )
    if before.head:
        # diff-index supports icase pathspecs; ls-tree does not. This also finds
        # staged deletions, whose canonical name no longer exists in the index.
        changed = tuple(
            sorted(
                os.fsdecode(path)
                for path in _git(
                    root,
                    "diff-index",
                    "--cached",
                    "--name-only",
                    "--no-renames",
                    "-z",
                    before.head,
                    "--",
                    *pathspecs,
                ).split(b"\0")
                if path
            )
        )
    else:
        changed = tuple(sorted(staged))
    if changed and refuse_staged_paths:
        raise LlmCoordError(
            ErrorCode.INDEX_PATH_STAGED,
            "publication paths have staged Git changes",
            {"retryable": True, "paths": list(changed)},
        )
    # Detect index changes made while the two read-only Git queries ran.
    if fingerprint(root) != before:
        raise LlmCoordError(
            ErrorCode.GIT_OPERATION_ACTIVE,
            "Git state changed during preflight; retry publication",
            {"retryable": True},
        )
    return before, changed


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _stamp(root: Path, path: str) -> list[int] | None:
    try:
        s = (root / path).lstat()
    except FileNotFoundError:
        return None
    return [s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def _files(root: Path) -> dict[str, list[int] | None]:
    entries = _git(
        root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
    ).split(b"\0")
    if len(entries) > _MAX_PATHS + 1:
        raise LlmCoordError(
            ErrorCode.CONTEXT_TOO_LARGE, "Git reconciliation exceeds its path budget"
        )
    return {
        os.fsdecode(path): _stamp(root, os.fsdecode(path)) for path in entries if path
    }


def _binding(store: ControlStore, root: Path) -> sqlite3.Row | None:
    with store.connection() as con:
        row = con.execute(
            "SELECT w.workspace_id,w.checkout_id,w.workspace_epoch,"
            "w.workspace_revision,c.path_case_insensitive "
            "FROM workspaces w JOIN checkouts c ON c.checkout_id=w.checkout_id "
            "WHERE w.canonical_path=? AND w.kind='shared_checkout' AND "
            "w.mode='shared' AND w.state='active'",
            (str(root),),
        ).fetchone()
        return cast(sqlite3.Row | None, row)


def _pending(con: sqlite3.Connection, workspace: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM workspace_batches WHERE workspace_id=? AND state "
            "IN ('applying','operator_attention') UNION ALL SELECT 1 FROM "
            "workspace_publications WHERE workspace_id=? AND operation_state "
            "IN ('prepared','operator_attention') LIMIT 1",
            (workspace, workspace),
        ).fetchone()
        is not None
    )


def _event(
    store: ControlStore,
    con: sqlite3.Connection,
    binding: sqlite3.Row,
    kind: str,
    payload: dict[str, Any],
) -> None:
    store.append_checkout_event(
        con,
        checkout_id=str(binding["checkout_id"]),
        event_type=kind,
        caused_by_session_id=None,
        payload=payload,
        now=int(time.time() * 1000),
    )


def reconcile(
    store: ControlStore, root: Path, *, candidate_dir: Path | None = None
) -> None:
    """Observe HEAD changes once per shared workspace, without a new scan loop.

    Full path metadata is collected only on bootstrap or HEAD movement. Ordinary
    passes read HEAD and the index fingerprint, without rehashing source files.
    """
    binding = _binding(store, root)
    if binding is None:
        return
    current = fingerprint(root)
    with store.connection() as con:
        if _pending(con, str(binding["workspace_id"])):
            return
        previous = con.execute(
            "SELECT * FROM workspace_git_state WHERE workspace_id=?",
            (binding["workspace_id"],),
        ).fetchone()
    if (
        previous is not None
        and previous["head_oid"] == current.head
        and not previous["rescan_required"]
        and root not in _FAILED.get(store, set())
        and root in _SEEN.get(store, set())
    ):
        return
    files = _files(root)
    # Rebase/merge/index writers must finish before a structural baseline advances.
    if fingerprint(root) != current or current.operations:
        return
    previous_files = json.loads(previous["files_json"]) if previous is not None else {}
    changed = sorted(
        path
        for path in previous_files.keys() | files.keys()
        if previous_files.get(path) != files.get(path)
    )
    head_changed = previous is not None and previous["head_oid"] != current.head
    links = (
        _proven_batches(
            store, root, str(binding["workspace_id"]), current.head, candidate_dir
        )
        if head_changed and not changed
        else []
    )
    with store.connection() as con, immediate_transaction(con):
        # A publication has its own identity journal. Do not observe a partial
        # tree, or race a publication that completed during metadata collection.
        revision = con.execute(
            "SELECT workspace_revision FROM workspaces WHERE workspace_id=?",
            (binding["workspace_id"],),
        ).fetchone()
        if (
            revision is None
            or revision[0] != binding["workspace_revision"]
            or _pending(con, str(binding["workspace_id"]))
        ):
            return
        # Compare-and-swap: another session may have observed the same movement.
        live = con.execute(
            "SELECT head_oid,files_json,rescan_required FROM workspace_git_state "
            "WHERE workspace_id=?",
            (binding["workspace_id"],),
        ).fetchone()
        if previous is not None and (
            live is None
            or tuple(live)
            != (
                previous["head_oid"],
                previous["files_json"],
                previous["rescan_required"],
            )
        ):
            return
        if previous is None and live is not None:
            return
        if head_changed:
            if changed:
                con.execute(
                    "UPDATE workspaces SET workspace_epoch=workspace_epoch+1 WHERE "
                    "workspace_id=?",
                    (binding["workspace_id"],),
                )
                epoch = int(
                    con.execute(
                        "SELECT workspace_epoch FROM workspaces WHERE workspace_id=?",
                        (binding["workspace_id"],),
                    ).fetchone()[0]
                )
                sessions = con.execute(
                    "SELECT session_id FROM sessions WHERE checkout_id=? AND state "
                    "IN ('active','disconnected')",
                    (binding["checkout_id"],),
                ).fetchall()
                for session in sessions:
                    _event(
                        store,
                        con,
                        binding,
                        "git.checkout_changed",
                        {
                            "before": previous["head_oid"],
                            "head": current.head,
                            "paths": changed[:1000],
                            "path_count": len(changed),
                            "epoch": epoch,
                            "affected_session_id": session[0],
                            "candidates": "exact_base_revalidation_required",
                        },
                    )
            else:
                _event(
                    store,
                    con,
                    binding,
                    "git.commit_observed",
                    {
                        "before": previous["head_oid"],
                        "head": current.head,
                        "batch_ids": links,
                    },
                )
                for batch in links:
                    con.execute(
                        "INSERT OR IGNORE INTO "
                        "workspace_git_batch_commits(batch_id,commit_oid) VALUES "
                        "(?,?)",
                        (batch, current.head),
                    )
        con.execute(
            "INSERT INTO "
            "workspace_git_state(workspace_id,head_oid,index_digest,files_json,rescan_required)"
            " VALUES (?,?,?,?,0) ON CONFLICT(workspace_id) DO UPDATE SET "
            "head_oid=excluded.head_oid,index_digest=excluded.index_digest,files_json=excluded.files_json,rescan_required=0",
            (binding["workspace_id"], current.head, current.index, _encode(files)),
        )

    _FAILED.get(store, set()).discard(root)
    _SEEN.setdefault(store, set()).add(root)


def _proven_batches(
    store: ControlStore,
    root: Path,
    workspace: str,
    head: str,
    candidate_dir: Path | None,
) -> list[str]:
    if not head or candidate_dir is None:
        return []
    from llm_cli.workspace.broker import load_candidate_content

    tree: dict[str, tuple[str, str]] = {}
    for entry in _git(root, "ls-tree", "-r", "-z", head).split(b"\0"):
        if entry:
            header, raw_path = entry.split(b"\t", 1)
            mode, _, oid = header.decode().split()
            tree[os.fsdecode(raw_path)] = (mode, oid)
    with store.connection() as con:
        rows = con.execute(
            "SELECT batch_id,request_json FROM workspace_batches WHERE "
            "workspace_id=? AND state='published' AND NOT EXISTS (SELECT 1 FROM "
            "workspace_git_batch_commits l WHERE "
            "l.batch_id=workspace_batches.batch_id AND l.commit_oid=?) ORDER BY "
            "created_at DESC LIMIT 1000",
            (workspace, head),
        ).fetchall()
    proven = []
    for row in rows:
        for item in json.loads(row["request_json"])["files"]:
            path = item["relative_path"]
            result = item["result"]
            if result["kind"] == "absent":
                matches = path not in tree
            elif result["kind"] == "directory_marker":
                # Empty directory creation has no Git tree representation.
                # Do not infer proof from an unrelated descendant.
                matches = False
            else:
                try:
                    body = load_candidate_content(candidate_dir, item["content_hash"])
                except (OSError, ValueError):
                    break
                digest = hashlib.sha256 if len(head) == 64 else hashlib.sha1
                blob = digest(
                    b"blob " + str(len(body)).encode() + b"\0" + body
                ).hexdigest()
                matches = tree.get(path) == (result["mode"], blob)
            if not matches:
                break
        else:
            proven.append(str(row["batch_id"]))
    return proven


@contextmanager
def publication(
    store: ControlStore,
    root: Path,
    paths: Sequence[str],
    *,
    refuse_staged_paths: bool = False,
    candidate_dir: Path | None = None,
) -> Iterator[None]:
    """Guard a new apply; recovery callers continue to use exact journals alone."""
    reconcile(store, root, candidate_dir=candidate_dir)
    binding = _binding(store, root)
    before, staged = preflight(
        root,
        paths,
        refuse_staged_paths=refuse_staged_paths,
        case_insensitive_filesystem=bool(binding["path_case_insensitive"])
        if binding is not None
        else False,
    )
    if binding is not None:
        with store.connection() as con:
            state = con.execute(
                "SELECT head_oid,rescan_required FROM workspace_git_state "
                "WHERE workspace_id=?",
                (binding["workspace_id"],),
            ).fetchone()
        if (
            state is None
            or state["head_oid"] != before.head
            or state["rescan_required"]
            or root in _FAILED.get(store, set())
        ):
            raise LlmCoordError(
                ErrorCode.GIT_OPERATION_ACTIVE,
                "Git reconciliation is incomplete; retry publication",
                {"retryable": True},
            )
    if staged and binding is not None:
        with store.connection() as con, immediate_transaction(con):
            _event(
                store,
                con,
                binding,
                "git.staged_paths_notice",
                {
                    "paths": list(staged),
                    "message": "Publication changes the working files; staged Git "
                    "versions are retained.",
                },
            )
    try:
        yield
    finally:
        try:
            _postflight(store, root, paths, before, binding)
        except (LlmCoordError, OSError, ValueError, sqlite3.Error):
            # Publication success is decided by its exact-identity journal.
            # Even an unavailable DB must not turn confirmed writes into failure.
            _FAILED.setdefault(store, set()).add(root)
            _LOG.warning(
                "Git postflight observation incomplete; "
                "next publication requires a rescan"
            )


def _postflight(
    store: ControlStore,
    root: Path,
    paths: Sequence[str],
    before: GitFingerprint,
    binding: sqlite3.Row | None,
) -> None:
    after: GitFingerprint | None
    try:
        after = fingerprint(root)
    except (LlmCoordError, OSError, ValueError):
        # A failed postflight must not turn an already confirmed publication
        # into a failed task. Preserve the durable journal and require scan.
        after = None
    if binding is not None:
        with store.connection() as con, immediate_transaction(con):
            if before != after:
                _event(
                    store,
                    con,
                    binding,
                    "git.concurrent_operation_attention",
                    {
                        "paths": list(paths),
                        "before_head": before.head,
                        "head": after.head if after is not None else None,
                        "message": "Git state changed during apply. The file "
                        "journal retains exact identities; rescan "
                        "before another publication.",
                    },
                )
                con.execute(
                    "UPDATE workspace_git_state SET rescan_required=1 WHERE "
                    "workspace_id=?",
                    (binding["workspace_id"],),
                )
            else:
                row = con.execute(
                    "SELECT files_json FROM workspace_git_state WHERE workspace_id=?",
                    (binding["workspace_id"],),
                ).fetchone()
                if row is not None:
                    files = json.loads(row[0])
                    for path in paths:
                        value = _stamp(root, path)
                        if value is None or (root / path).is_dir():
                            files.pop(path, None)
                        else:
                            files[path] = value
                    con.execute(
                        "UPDATE workspace_git_state SET files_json=? WHERE "
                        "workspace_id=?",
                        (_encode(files), binding["workspace_id"]),
                    )


def observe_paths(
    store: ControlStore, root: Path, samples: Sequence[tuple[str, str]]
) -> None:
    """Advance only C4's successfully observed metadata under the same HEAD.

    These samples describe observations, never publication authority. Keeping
    them current distinguishes a later ordinary commit from a checkout switch.
    """
    if not samples:
        return
    binding = _binding(store, root)
    if binding is None:
        return
    current = fingerprint(root)
    with store.connection() as con, immediate_transaction(con):
        row = con.execute(
            "SELECT head_oid,files_json FROM workspace_git_state WHERE workspace_id=?",
            (binding["workspace_id"],),
        ).fetchone()
        if row is None or row["head_oid"] != current.head or current.operations:
            return
        files = json.loads(row["files_json"])
        for path, encoded in samples:
            if encoded == "absent":
                files.pop(path, None)
            elif encoded != "unavailable":
                files[path] = json.loads(encoded)
        con.execute(
            "UPDATE workspace_git_state SET files_json=? WHERE workspace_id=?",
            (_encode(files), binding["workspace_id"]),
        )
