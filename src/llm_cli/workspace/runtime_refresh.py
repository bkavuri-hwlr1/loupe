"""Stat-based runtime refresh and private, retained runtime generations.

Only paths, stat receipts and consent enter SQLite. Runtime bytes stay in
owner-only disposable directories beside the session worktrees.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path
from threading import Lock
from typing import Any

from llm_cli.agent.source_policy import register_runtime_paths
from llm_cli.ids import new_id
from llm_cli.storage.connection import immediate_transaction
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.hydration import _evidence, plan_hydration


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _roots(paths: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for path in sorted(set(paths)):
        if not any(path.startswith(parent + "/") for parent in result):
            result.append(path)
    return tuple(result)


def _signature(root: Path, relative: str) -> dict[str, dict[str, int]]:
    """Bounded metadata traversal, without opening file contents or following links."""
    from llm_cli.workspace.isolated import _error, _parent

    result: dict[str, dict[str, int]] = {}
    deadline = time.monotonic() + 60

    def visit(parent: int, name: str, path: str, depth: int = 0) -> None:
        if len(result) >= 200_000 or depth > 64 or time.monotonic() > deadline:
            raise _error("runtime metadata exceeded its entry, depth or time budget")
        observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
        result[path] = _evidence(observed)
        if stat.S_ISDIR(observed.st_mode):
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            try:
                if _evidence(os.fstat(child)) != _evidence(observed):
                    raise _error("runtime directory changed during observation")
                for entry in sorted(os.listdir(child)):
                    visit(child, entry, path + "/" + entry, depth + 1)
                if _evidence(os.fstat(child)) != _evidence(observed):
                    raise _error("runtime directory changed during observation")
            finally:
                os.close(child)

    try:
        with _parent(root, relative) as (parent, name):
            visit(parent, name, relative)
    except FileNotFoundError:
        if result:
            raise
    return result


def _same(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # Renaming a staged inode changes ctime. All other evidence remains pinned.
    def stable(value: dict[str, Any]) -> dict[str, Any]:
        return {
            path: {key: item for key, item in fields.items() if key != "ctime_ns"}
            for path, fields in value.items()
        }

    return stable(left) == stable(right)


_ACTIVITY_LOCK = Lock()
_ACTIVITIES: dict[tuple[Path, str], int] = {}
_REFRESHING: set[tuple[Path, str]] = set()


class RuntimeRefresher:
    """One daemon-owned manager; activity locks never hold the publication lock."""

    def __init__(self, store: ControlStore, managed_root: Path, lock: Any) -> None:
        self.store, self.managed_root, self.lock = store, managed_root, lock
        self.fault: Callable[[str], None] = lambda stage: None
        self._activity_lock = _ACTIVITY_LOCK

    def _key(self, workspace_id: str) -> tuple[Path, str]:
        return self.store.path.resolve(), workspace_id

    def busy(self, workspace_id: str) -> bool:
        """Call under the publication lock before retiring a runtime binding."""
        with self._activity_lock:
            key = self._key(workspace_id)
            return bool(_ACTIVITIES.get(key, 0) or key in _REFRESHING)

    @contextmanager
    def activity(self, workspace_id: str) -> Iterator[None]:
        key = self._key(workspace_id)
        # The order is always publication lock -> tiny activity counter lock.
        # Installation cannot cross the beginning of a command/check snapshot.
        with self.lock:
            self.recover(workspace_id)
            with self._activity_lock:
                _ACTIVITIES[key] = _ACTIVITIES.get(key, 0) + 1
        try:
            yield
        finally:
            with self._activity_lock:
                _ACTIVITIES[key] -= 1

    def busy(self, workspace_id: str) -> bool:
        """Inspect activity under the publication lock before a binding transfer."""
        key = self._key(workspace_id)
        with self._activity_lock:
            return bool(_ACTIVITIES.get(key, 0) or key in _REFRESHING)

    def _binding(self, workspace_id: str) -> tuple[Path, Path, str]:
        from llm_cli.workspace.isolated import _error

        with self.store.connection() as con:
            row = con.execute(
                "SELECT w.canonical_path,c.canonical_path AS primary_path,"
                "i.creation_token FROM workspaces w JOIN checkouts c "
                "USING(checkout_id) "
                "JOIN isolated_workspaces i USING(workspace_id) WHERE w.workspace_id=?",
                (workspace_id,),
            ).fetchone()
        if row is None:
            raise _error("runtime workspace binding is unavailable")
        private = Path(row["canonical_path"])
        if private != self.managed_root.resolve() / workspace_id:
            raise _error("runtime workspace escaped its managed root")
        return Path(row["primary_path"]), private, str(row["creation_token"])

    def _directory(self, workspace_id: str) -> Path:
        from llm_cli.workspace.isolated import (
            _error,
            _parent,
            _read,
            _source,
            _write_body,
        )

        _, _, token = self._binding(workspace_id)
        root = self.managed_root / (".runtime-" + workspace_id)
        try:
            root.mkdir(mode=0o700)
            with _parent(root, "owner") as (parent, name):
                _write_body(parent, name, token.encode(), 0o600)
        except FileExistsError:
            pass
        if root.is_symlink() or root.stat().st_mode & 0o077:
            raise _error("runtime generation directory is not private")
        with _source(root, "owner") as descriptor:
            if _read(descriptor, 1024).decode() != token:
                raise _error("runtime generation ownership changed")
        return root

    def initialize(
        self,
        workspace_id: str,
        approved: Sequence[str] = (),
        *,
        rebuilt_roots: Sequence[str] = (),
    ) -> None:
        with self.lock:
            with self.store.connection() as con:
                if con.execute(
                    "SELECT 1 FROM isolated_runtime_state WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone():
                    return
                row = con.execute(
                    "SELECT runtime_paths_json,runtime_files_json FROM "
                    "isolated_workspaces WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()
            assert row is not None
            primary, private, _ = self._binding(workspace_id)
            from llm_cli.workspace.runtime import dependency_path

            paths = _roots(
                path
                for path in json.loads(row["runtime_paths_json"])
                if dependency_path(path) or not (primary / path).is_dir()
            )
            files = _roots(json.loads(row["runtime_files_json"]))
            rebuilt = tuple(rebuilt_roots) or self._rebuilt_roots(workspace_id)
            before = {
                path: _signature(primary, path) for path in paths if path not in rebuilt
            }
            copied = {path: _signature(private, path) for path in files}
            with self.store.connection() as con, immediate_transaction(con):
                con.execute(
                    "INSERT OR IGNORE INTO isolated_runtime_state VALUES "
                    "(?,0,?,?,?,'{}')",
                    (workspace_id, _json(before), _json(copied), _json(approved)),
                )
                con.execute(
                    "INSERT OR IGNORE INTO isolated_runtime_generations VALUES (?,0,?)",
                    (workspace_id, _json({"paths": files, "private": copied})),
                )

    def finish_creation(self, workspace_id: str, rebuilt: Sequence[str]) -> None:
        """Attach new roots without rebasing the opening copied-runtime receipts."""
        from llm_cli.workspace.isolated import _error

        with self.lock:
            _, private, _ = self._binding(workspace_id)
            added = {path: _signature(private, path) for path in rebuilt}
            with self.store.connection() as con, immediate_transaction(con):
                row = con.execute(
                    "SELECT generation,private_json FROM isolated_runtime_state "
                    "WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()
                if row is None or row["generation"] != 0:
                    raise _error("dependency creation runtime generation changed")
                copied = json.loads(row["private_json"])
                copied.update(added)
                con.execute(
                    "UPDATE isolated_runtime_state SET private_json=? "
                    "WHERE workspace_id=?",
                    (_json(copied), workspace_id),
                )
                con.execute(
                    "UPDATE isolated_runtime_generations SET paths_json=? "
                    "WHERE workspace_id=? AND generation=0",
                    (
                        _json({"paths": tuple(sorted(copied)), "private": copied}),
                        workspace_id,
                    ),
                )

    def _rebuilt_roots(self, workspace_id: str) -> tuple[str, ...]:
        from llm_cli.workspace.rebuild import configured_roots

        with self.store.connection() as con:
            row = con.execute(
                "SELECT w.git_dir,i.creation_token FROM workspaces w "
                "JOIN isolated_workspaces i USING(workspace_id) WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
        return (
            configured_roots(Path(row["git_dir"]), str(row["creation_token"]))
            if row and row["git_dir"]
            else ()
        )

    def _state(self, workspace_id: str) -> dict[str, Any]:
        self.initialize(workspace_id)
        with self.store.connection() as con:
            row = con.execute(
                "SELECT * FROM isolated_runtime_state WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def refresh(self, workspace_id: str) -> tuple[str, ...]:
        from llm_cli.errors import LlmCoordError

        key = self._key(workspace_id)
        with self._activity_lock:
            if _ACTIVITIES.get(key, 0) or key in _REFRESHING:
                return ()
            _REFRESHING.add(key)
        try:
            primary, private, _ = self._binding(workspace_id)
            authority = getattr(self.store, "authority", None)
            guard = authority.pin(primary) if authority is not None else nullcontext()
            with guard:
                self.recover(workspace_id)
                try:
                    return self._refresh(workspace_id, primary, private)
                except (LlmCoordError, OSError, ValueError):
                    return (
                        "Runtime refresh deferred; retrying at the next safe point.",
                    )
        finally:
            with self._activity_lock:
                _REFRESHING.discard(key)

    def _refresh(
        self, workspace_id: str, primary: Path, private: Path
    ) -> tuple[str, ...]:
        from llm_cli.workspace.isolated import copy_runtime_file
        from llm_cli.workspace.runtime import copy_dependency_tree

        state = self._state(workspace_id)
        approved = tuple(json.loads(state["approved_json"]))
        baseline = json.loads(state["primary_json"])
        copied = json.loads(state["private_json"])
        noticed = json.loads(state["noticed_json"])
        # Installer approval is frozen to opening. Refresh never imports a
        # primary dependency tree over an explicitly rebuilt private root.
        rebuilt = self._rebuilt_roots(workspace_id)
        plan = plan_hydration(
            primary,
            approved_secret_paths=approved,
            frozen_rebuild_roots=rebuilt,
        )
        if not plan.complete:
            return ("Runtime refresh deferred; hydration inventory is incomplete.",)
        entries = {
            entry.path: entry
            for entry in plan.entries
            if entry.action != "exclude_administration"
            and entry.classification == "ignored_runtime"
            and entry.path not in rebuilt
            and (entry.kind != "directory" or entry.action == "private_tree")
        }
        # Never let an edited ignore rule reclassify a pinned runtime path as source.
        paths = tuple(
            path for path in _roots((*baseline, *entries)) if path not in rebuilt
        )
        fresh = {path: _signature(primary, path) for path in paths}
        updates: dict[str, Any] = {}
        notices: list[str] = []
        receipts: dict[str, tuple[dict[str, Any], str]] = {}
        for path in paths:
            if fresh[path] == baseline.get(path, {}):
                continue
            local = _signature(private, path)
            entry = entries.get(path)
            if local != copied.get(path, {}):
                if noticed.get(path) != fresh[path]:
                    message = "Runtime conflict; preserved private copy: " + path
                    notices.append(message)
                    receipts[path] = (fresh[path], message)
                continue
            if entry is not None and entry.action not in {
                "private_copy",
                "clone_or_copy",
                "private_tree",
            }:
                if noticed.get(path) != fresh[path]:
                    message = (
                        "Runtime update omitted without opening consent or adapter: "
                        + path
                    )
                    notices.append(message)
                    receipts[path] = (fresh[path], message)
                continue
            if fresh[path] and entry is None:
                continue
            updates[path] = entry
        if not updates:
            return self._notice_result(workspace_id, noticed, receipts, notices)
        root = self._directory(workspace_id)
        token = new_id("refresh")
        stage = root / token
        stage.mkdir(mode=0o700)
        self.fault("runtime_staging")
        before = {path: _signature(private, path) for path in updates}
        try:
            for path, entry in updates.items():
                if entry is None:
                    continue
                if entry.action == "private_tree":
                    notices.extend(
                        copy_dependency_tree(
                            primary,
                            stage,
                            path,
                            approved_secret_paths=approved,
                            relocation_root=private,
                            workspace_root=private,
                        )
                    )
                else:
                    copy_runtime_file(primary, stage, entry)
            if any(_signature(primary, path) != fresh[path] for path in updates):
                raise ValueError("primary runtime changed during refresh")
            if any(_signature(private, path) != before[path] for path in updates):
                raise ValueError("private runtime changed during refresh")
            after = {path: _signature(stage, path) for path in updates}
            generation = int(state["generation"]) + 1
            accepted = {**baseline, **{path: fresh[path] for path in updates}}
            with self.lock:
                with self._activity_lock:
                    busy = bool(_ACTIVITIES.get(self._key(workspace_id), 0))
                current = self._state(workspace_id)
                if busy or current["generation"] != state["generation"]:
                    self._remove(root, token)
                    return self._notice_result(workspace_id, noticed, receipts, notices)
                if any(_signature(private, path) != before[path] for path in updates):
                    raise ValueError("private runtime changed before installation")
                with self._mutation(workspace_id) as operation:
                    with self.store.connection() as con, immediate_transaction(con):
                        con.execute(
                            "INSERT INTO isolated_runtime_journal VALUES "
                            "(?,?,?,?,?,?,?,'prepared')",
                            (
                                workspace_id,
                                generation,
                                _json(list(updates)),
                                _json(before),
                                _json(after),
                                _json(accepted),
                                token,
                            ),
                        )
                    self.fault("runtime_prepared")
                    self._recover(workspace_id)
                    if operation is not None:
                        operation.confirm()
                    self._retire_receipt(workspace_id, operation)
            notices.extend("Runtime refreshed: " + path for path in updates)
            return self._notice_result(workspace_id, noticed, receipts, notices)
        except Exception:
            with self.store.connection() as con:
                recorded = con.execute(
                    "SELECT 1 FROM isolated_runtime_journal WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()
            if not recorded:
                self._remove(root, token)
            raise

    def _notice_result(
        self,
        workspace_id: str,
        observed: dict[str, Any],
        receipts: dict[str, tuple[dict[str, Any], str]],
        notices: list[str],
    ) -> tuple[str, ...]:
        """Persist every emitted path notice, merging only unchanged receipts.

        A concurrently recorded newer observation wins; neither a successful
        unrelated refresh nor an aborted stage may replay an old notice.
        """
        if not receipts:
            return tuple(notices)
        suppressed: set[str] = set()
        with self.lock, self.store.connection() as con, immediate_transaction(con):
            row = con.execute(
                "SELECT noticed_json FROM isolated_runtime_state WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
            assert row is not None
            current = json.loads(row[0])
            for path, (signature, message) in receipts.items():
                if current.get(path) == signature or current.get(path) != observed.get(
                    path
                ):
                    suppressed.add(message)
                    continue
                current[path] = signature
            encoded = _json(current)
            if encoded != row[0]:
                con.execute(
                    "UPDATE isolated_runtime_state SET noticed_json=? "
                    "WHERE workspace_id=?",
                    (encoded, workspace_id),
                )
        return tuple(message for message in notices if message not in suppressed)

    @staticmethod
    def _remove(root: Path, name: str) -> None:
        from llm_cli.workspace.isolated import IsolatedWorkspaceManager, _parent

        with _parent(root, name) as (parent, leaf):
            IsolatedWorkspaceManager._remove_private_entry(parent, leaf)
            os.fsync(parent)

    @contextmanager
    def _mutation(self, workspace_id: str, *, recovery: bool = False) -> Iterator[Any]:
        authority = getattr(self.store, "authority", None)
        if authority is None:
            yield None
            return
        primary, _, _ = self._binding(workspace_id)
        with authority.mutation(
            primary, "runtime", workspace_id, recovery=recovery
        ) as operation:
            yield operation

    def recover(self, workspace_id: str) -> None:
        from llm_cli.workspace.isolated import _error

        # Beginning activity and replay share this brief lock. A command cannot
        # take a snapshot of a half-installed generation left by a live IO error.
        with self.lock:
            with self.store.connection() as con:
                pending = con.execute(
                    "SELECT 1 FROM isolated_runtime_journal WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()
            if pending is None:
                self._recover(workspace_id)
                return
            with self._activity_lock:
                busy = bool(_ACTIVITIES.get(self._key(workspace_id), 0))
            if busy:
                raise _error("Runtime recovery waits for running commands or checks.")
            with self._mutation(workspace_id, recovery=True) as operation:
                self._recover(workspace_id)
                if operation is not None:
                    operation.confirm()
                self._retire_receipt(workspace_id, operation)

    def _retire_receipt(self, workspace_id: str, operation: Any) -> None:
        # A delegated child cannot acknowledge the parent's physical marker.
        # Retain exact domain proof until a later nondelegated recovery can.
        if operation is not None and getattr(operation, "delegated", False):
            return
        self.fault("runtime_authority_confirmed")
        with self.store.connection() as con:
            con.execute(
                "DELETE FROM isolated_runtime_journal WHERE workspace_id=?",
                (workspace_id,),
            )

    def _recover(self, workspace_id: str) -> None:
        """Roll a prepared refresh forward, recognizing only recorded inodes."""
        from llm_cli.workspace.isolated import _error

        with self.lock:
            with self.store.connection() as con:
                row = con.execute(
                    "SELECT * FROM isolated_runtime_journal WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()
            orphan_root = self.managed_root / (".runtime-" + workspace_id)
            with self._activity_lock:
                active_staging = self._key(workspace_id) in _REFRESHING or bool(
                    _ACTIVITIES.get(self._key(workspace_id), 0)
                )
            if (
                orphan_root.exists() or orphan_root.is_symlink()
            ) and not active_staging:
                owned = self._directory(workspace_id)
                live = str(row["stage_token"]) if row else None
                for entry in owned.iterdir():
                    if entry.name.startswith("refresh_") and entry.name not in {
                        live,
                        (live + "-before") if live else None,
                    }:
                        self._remove(owned, entry.name)
                with self.store.connection() as con:
                    retained = {
                        int(item[0])
                        for item in con.execute(
                            "SELECT generation FROM isolated_runtime_holds "
                            "WHERE workspace_id=?",
                            (workspace_id,),
                        )
                    }
                for entry in owned.iterdir():
                    if (
                        entry.name.startswith("generation-")
                        and entry.name[len("generation-") :].isdigit()
                        and int(entry.name[len("generation-") :]) not in retained
                    ):
                        self._remove(owned, entry.name)
                with self.store.connection() as con:
                    con.execute(
                        "DELETE FROM isolated_runtime_generations "
                        "WHERE workspace_id=? AND generation NOT IN "
                        "(SELECT generation FROM isolated_runtime_holds "
                        "WHERE workspace_id=?)",
                        (workspace_id, workspace_id),
                    )
            if row is None:
                return
            _, private, _ = self._binding(workspace_id)
            root = self._directory(workspace_id)
            token = str(row["stage_token"])
            if not token.startswith("refresh_") or "/" in token:
                raise _error("runtime refresh journal token changed")
            stage = root / token
            backup = root / (token + "-before")
            if row["state"] == "installed":
                with self.store.connection() as con:
                    accepted = con.execute(
                        "SELECT generation,private_json FROM isolated_runtime_state "
                        "WHERE workspace_id=?",
                        (workspace_id,),
                    ).fetchone()
                if accepted is None or int(accepted["generation"]) < int(
                    row["generation"]
                ):
                    raise _error("installed runtime generation proof is unavailable")
                installed = json.loads(accepted["private_json"])
                if any(
                    _signature(private, path) != installed.get(path, {})
                    for path in json.loads(row["paths_json"])
                ):
                    raise _error(
                        "installed runtime changed before authority acknowledgment"
                    )
                self._remove(root, token)
                self._remove(root, token + "-before")
                self.fault("runtime_cleaned")
                return
            backup.mkdir(mode=0o700, exist_ok=True)
            before, after = (
                json.loads(row["before_json"]),
                json.loads(row["after_json"]),
            )
            paths = json.loads(row["paths_json"])
            # Verify every transition before the first write. Later user edits
            # are preserved and require recovery, never silently overwritten.
            for path in paths:
                local, staged, old = (
                    _signature(base, path) for base in (private, stage, backup)
                )
                if _same(local, after[path]) and not staged:
                    continue
                if not _same(staged, after[path]):
                    raise _error("runtime refresh staged input changed: " + path)
                if not (
                    (_same(local, before[path]) and not old)
                    or (not local and _same(old, before[path]))
                ):
                    raise _error("runtime refresh private input changed: " + path)
            for path in paths:
                if _same(_signature(private, path), after[path]) and not _signature(
                    stage, path
                ):
                    continue
                if _signature(private, path):
                    self._rename(private, backup, path)
                self.fault("runtime_removed:" + path)
                if after[path]:
                    self._rename(stage, private, path)
                self.fault("runtime_installed:" + path)
            with self.store.connection() as con, immediate_transaction(con):
                prior = con.execute(
                    "SELECT private_json FROM isolated_runtime_state WHERE "
                    "workspace_id=?",
                    (workspace_id,),
                ).fetchone()
                copied = json.loads(prior[0])
                copied.update({path: _signature(private, path) for path in paths})
                copied = {path: value for path, value in copied.items() if value}
                generation = int(row["generation"])
                con.execute(
                    "UPDATE isolated_runtime_state SET "
                    "generation=?,primary_json=?,private_json=? "
                    "WHERE workspace_id=?",
                    (generation, row["primary_json"], _json(copied), workspace_id),
                )
                con.execute(
                    "INSERT OR REPLACE INTO isolated_runtime_generations VALUES "
                    "(?,?,?)",
                    (
                        workspace_id,
                        generation,
                        _json({"paths": list(copied), "private": copied}),
                    ),
                )
                hydrated = tuple(sorted(copied))
                existing = con.execute(
                    "SELECT runtime_paths_json FROM isolated_workspaces "
                    "WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()
                all_paths = tuple(
                    sorted(
                        set(json.loads(existing[0]))
                        | set(json.loads(row["primary_json"]))
                    )
                )
                con.execute(
                    "UPDATE isolated_workspaces SET "
                    "runtime_paths_json=?,runtime_files_json=? WHERE workspace_id=?",
                    (_json(all_paths), _json(hydrated), workspace_id),
                )
                con.execute(
                    "UPDATE isolated_sync_state SET runtime_paths_json=? WHERE "
                    "workspace_id=?",
                    (_json(all_paths), workspace_id),
                )
                con.execute(
                    "UPDATE isolated_runtime_journal SET state='installed' WHERE "
                    "workspace_id=?",
                    (workspace_id,),
                )
            register_runtime_paths(private, all_paths)
            self.fault("runtime_committed")
            self._remove(root, token)
            self._remove(root, token + "-before")
            # Installed proof remains until physical authority acknowledges it.
            self.fault("runtime_cleaned")

    @staticmethod
    def _rename(source: Path, target: Path, relative: str) -> None:
        from llm_cli.workspace.isolated import _parent

        with (
            _parent(source, relative) as (before, name),
            _parent(target, relative, create=True) as (after, leaf),
        ):
            os.rename(name, leaf, src_dir_fd=before, dst_dir_fd=after)
            os.fsync(before)
            os.fsync(after)

    def pin_task(self, workspace_id: str, task_id: str, attempt: int) -> Path:
        """Freeze exact private inputs before retaining, without a long global lock."""
        from llm_cli.workspace.isolated import _error, copy_runtime_files
        from llm_cli.workspace.runtime import register_runtime_workspace

        with self.activity(workspace_id):
            self.recover(workspace_id)
            self._state(workspace_id)
            _, private, _ = self._binding(workspace_id)
            with self.store.connection() as con:
                current = con.execute(
                    "SELECT runtime_files_json FROM isolated_workspaces "
                    "WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()
                held = con.execute(
                    "SELECT h.generation,g.paths_json,w.status "
                    "FROM isolated_runtime_holds h "
                    "JOIN isolated_runtime_generations g "
                    "USING(workspace_id,generation) "
                    "LEFT JOIN task_workflows w ON w.task_id=h.task_id "
                    "AND w.attempt=h.attempt WHERE h.task_id=? AND h.attempt=?",
                    (task_id, attempt),
                ).fetchone()
            assert current is not None
            with self.store.connection() as con:
                workflow = con.execute(
                    "SELECT config_json FROM task_workflows "
                    "WHERE task_id=? AND attempt=?",
                    (task_id, attempt),
                ).fetchone()
            config_json = str(workflow[0]) if workflow else "{}"
            paths = _roots(json.loads(current[0]))
            observed = {path: _signature(private, path) for path in paths}
            root = self._directory(workspace_id)
            if held:
                metadata = json.loads(held["paths_json"])
                if held["status"] in {"awaiting_review", "cancelled", "published"} or (
                    metadata["private"] == observed
                    and metadata.get("config_json") == config_json
                ):
                    return self.task_root(workspace_id, task_id, attempt)
            # Reserve a generation number briefly; skipped numbers after crashes
            # are harmless because a hold becomes visible only after its copy.
            with self.lock, self.store.connection() as con, immediate_transaction(con):
                latest = con.execute(
                    "SELECT generation FROM isolated_runtime_state "
                    "WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()
                generation = int(latest[0]) + 1
                con.execute(
                    "UPDATE isolated_runtime_state SET generation=? "
                    "WHERE workspace_id=?",
                    (generation, workspace_id),
                )
            destination = root / ("generation-" + str(generation))
            destination.mkdir(mode=0o700)
            register_runtime_workspace(destination, private)
            try:
                copy_runtime_files(private, destination, paths)
                if observed != {path: _signature(private, path) for path in paths}:
                    raise _error("private runtime changed while retaining proposal")
                frozen = {path: _signature(destination, path) for path in paths}
                with (
                    self.lock,
                    self.store.connection() as con,
                    immediate_transaction(con),
                ):
                    con.execute(
                        "INSERT INTO isolated_runtime_generations VALUES (?,?,?)",
                        (
                            workspace_id,
                            generation,
                            _json(
                                {
                                    "paths": paths,
                                    "private": observed,
                                    "frozen": frozen,
                                    "config_json": config_json,
                                }
                            ),
                        ),
                    )
                    con.execute(
                        "INSERT OR REPLACE INTO isolated_runtime_holds VALUES "
                        "(?,?,?,?)",
                        (task_id, attempt, workspace_id, generation),
                    )
            except BaseException:
                self._remove(root, destination.name)
                raise
            return destination

    def task_config(self, task_id: str, attempt: int) -> str | None:
        with self.store.connection() as con:
            row = con.execute(
                "SELECT g.paths_json FROM isolated_runtime_holds h "
                "JOIN isolated_runtime_generations g USING(workspace_id,generation) "
                "WHERE h.task_id=? AND h.attempt=?",
                (task_id, attempt),
            ).fetchone()
        return json.loads(row[0]).get("config_json") if row else None

    def task_root(self, workspace_id: str, task_id: str, attempt: int) -> Path:
        from llm_cli.workspace.runtime import register_runtime_workspace

        with self.store.connection() as con:
            row = con.execute(
                "SELECT h.generation,g.paths_json FROM isolated_runtime_holds h "
                "JOIN isolated_runtime_generations g USING(workspace_id,generation) "
                "WHERE h.workspace_id=? AND h.task_id=? AND h.attempt=?",
                (workspace_id, task_id, attempt),
            ).fetchone()
        if row is None:
            # Upgrade path for proposals retained before runtime generations.
            return self.pin_task(workspace_id, task_id, attempt)
        root = self._directory(workspace_id) / ("generation-" + str(row["generation"]))
        if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077:
            raise ValueError("Retained runtime generation is unavailable.")
        metadata = json.loads(row["paths_json"])
        paths = tuple(metadata["paths"])
        if {path: _signature(root, path) for path in paths} != metadata["frozen"]:
            raise ValueError("Retained runtime generation changed.")
        register_runtime_paths(root, paths)
        register_runtime_workspace(root, self._binding(workspace_id)[1])
        return root

    def cleanup(self, workspace_id: str) -> None:
        candidate = self.managed_root / (".runtime-" + workspace_id)
        if candidate.exists() or candidate.is_symlink():
            root = self._directory(workspace_id)
            self._remove(self.managed_root, root.name)
        with self.store.connection() as con, immediate_transaction(con):
            for table in (
                "isolated_runtime_holds",
                "isolated_runtime_generations",
                "isolated_runtime_journal",
                "isolated_runtime_state",
            ):
                con.execute(
                    "DELETE FROM " + table + " WHERE workspace_id=?", (workspace_id,)
                )
