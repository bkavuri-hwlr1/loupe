"""Durable, between-task workspace switches with an owner bootstrap handshake.

A switch never changes a task attempt. Its source binding remains authoritative
until the owner consumes the exact target bootstrap and confirms the intent.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import SessionRecord
from llm_cli.errors import LlmCoordError
from llm_cli.storage.connection import immediate_transaction
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.batches import SharedBatchPublisher
from llm_cli.workspace.isolated import IsolatedWorkspaceManager, capture_source
from llm_cli.workspace.isolated_sync import IsolatedSynchronizer
from llm_cli.workspace.patches import portable_patch
from llm_cli.workspace.runtime_refresh import _signature
from llm_cli.workspace.workflow import TaskWorkflow, decode_files, encode_files

TERMINAL = frozenset({"confirmed", "rolled_back"})
_DISPOSITIONS = frozenset({"publish", "hold", "export", "retain", "discard"})


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class WorkspaceModeSwitcher:
    def __init__(
        self,
        store: ControlStore,
        isolated: IsolatedWorkspaceManager,
        sync: IsolatedSynchronizer,
        publisher: SharedBatchPublisher,
    ) -> None:
        self.store, self.isolated, self.sync, self.publisher = (
            store,
            isolated,
            sync,
            publisher,
        )
        self.workflow = TaskWorkflow(store, RepositoryCoordinator(store), publisher)
        self.workflow.isolated_sync = sync
        self.fault: Callable[[str], None] = lambda state: None
        self.worker_busy: Callable[[str], bool] = lambda session_id: False
        self._operation_lock = threading.Lock()
        self._operations: set[str] = set()

    def get(self, intent_id: str) -> dict[str, Any] | None:
        with self.store.connection() as con:
            row = con.execute(
                "SELECT * FROM mode_switch_intents WHERE switch_intent_id=?",
                (intent_id,),
            ).fetchone()
        return dict(row) if row else None

    def pending(self, session_id: str) -> dict[str, Any] | None:
        with self.store.connection() as con:
            row = con.execute(
                (
                    "SELECT * FROM mode_switch_intents WHERE session_id=? AND "
                    "state NOT IN ('confirmed','rolled_back')"
                ),
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def pending_response(self, session_id: str) -> dict[str, Any] | None:
        row = self.pending(session_id)
        if row is None:
            return None
        response = self._response(row)
        response["request_json"] = row["request_json"]
        return response

    def require_idle(self, session: SessionRecord) -> None:
        # Deliberately stricter than advisory session_has_active_task: ambiguous
        # recovery/queued launches also forbid changing the execution binding.
        with self.store.connection() as con:
            busy = con.execute(
                "SELECT 1 FROM tasks t WHERE t.session_id=? AND (t.state NOT IN "
                "('completed','failed','cancelled','reviewing','ready_for_int"
                "egration') "
                "OR EXISTS (SELECT 1 FROM all_task_executions e WHERE "
                "e.task_id=t.task_id "
                "AND e.state NOT IN ('published','failed','cleanup_failed')) "
                "OR EXISTS (SELECT 1 FROM claims c WHERE c.task_id=t.task_id "
                "AND c.state IN ('queued','active_work','publishing','active_"
                "integration'))) LIMIT 1",
                (session.session_id,),
            ).fetchone()
        if busy or self.worker_busy(session.session_id):
            raise ValueError(
                "finish or stop the active task and reconcile pending "
                "launches before switching workspace"
            )
        if self.isolated.runtime.busy(session.workspace_id or ""):
            raise ValueError(
                "wait for the active workspace command, export, or runtime "
                "refresh before switching"
            )
        self.isolated.ensure_available(session.workspace_id or "")

    def _scope(self, workspace_id: str) -> dict[str, Any]:
        workspace = self.store.get_workspace(workspace_id)
        if workspace is None or workspace.state != "active":
            raise ValueError("workspace binding is unavailable")
        root = Path(workspace.canonical_path)
        if root.resolve(strict=True) != root:
            raise ValueError("workspace root identity changed")
        status = root.stat(follow_symlinks=False)
        scope: dict[str, Any] = {
            "workspace_id": workspace_id,
            "checkout_id": workspace.checkout_id,
            "mode": workspace.mode,
            "canonical_path": str(root),
            "git_dir": workspace.git_dir,
            "device": status.st_dev,
            "inode": status.st_ino,
            "epoch": workspace.workspace_epoch,
            "local_generation": workspace.local_generation,
        }
        if workspace.mode == "shared":
            scope["source_manifest"] = hashlib.sha256(
                _json(capture_source(root, self.publisher.candidate_dir)).encode()
            ).hexdigest()
        if workspace.mode == "isolated":
            state = self.isolated.validate(workspace_id)
            scope["creation_token"] = state.creation_token
            scope["source_manifest"] = hashlib.sha256(
                _json(
                    capture_source(
                        root,
                        self.publisher.candidate_dir,
                        protected_paths=state.runtime_paths,
                    )
                ).encode()
            ).hexdigest()
            with self.store.connection() as con:
                sync = con.execute(
                    (
                        "SELECT generation,manifest_json FROM isolated_sync_state "
                        "WHERE workspace_id=?"
                    ),
                    (workspace_id,),
                ).fetchone()
                runtime = con.execute(
                    (
                        "SELECT generation FROM isolated_runtime_state WHERE "
                        "workspace_id=?"
                    ),
                    (workspace_id,),
                ).fetchone()
            scope["sync_generation"] = int(sync["generation"]) if sync else None
            scope["baseline"] = (
                hashlib.sha256(str(sync["manifest_json"]).encode()).hexdigest()
                if sync
                else None
            )
            scope["runtime_generation"] = int(runtime[0]) if runtime else None
            scope["runtime_inventory"] = {
                path: _signature(root, path) for path in state.runtime_files
            }
        return scope

    def _binding(self, row: dict[str, Any]) -> SessionRecord:
        session = self.store.get_session(row["session_id"])
        with self.store.connection() as con:
            generation = con.execute(
                "SELECT workspace_binding_generation FROM sessions WHERE session_id=?",
                (row["session_id"],),
            ).fetchone()
        if (
            session is None
            or generation is None
            or session.workspace_id != row["source_workspace_id"]
            or int(generation[0]) != row["source_binding_generation"]
        ):
            raise ValueError(
                "switch source binding changed; operator attention required"
            )
        return session

    def request(
        self,
        session_id: str,
        intent_id: str,
        target_mode: str,
        *,
        disposition: str | None = None,
        approved_secrets: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if (
            target_mode not in {"shared", "isolated"}
            or not intent_id
            or len(intent_id) > 128
        ):
            raise ValueError(
                "a switch intent ID and shared or isolated target are required"
            )
        if disposition is not None and disposition not in _DISPOSITIONS:
            raise ValueError(
                "disposition must be publish, hold, export, retain, or discard"
            )
        if target_mode == "isolated" and disposition is not None:
            raise ValueError(
                "source disposition applies only when returning to shared mode"
            )
        if target_mode == "shared" and approved_secrets:
            raise ValueError("runtime consent applies only to isolated mode")
        request = _json(
            {
                "target": target_mode,
                "disposition": disposition,
                "approved": sorted(set(approved_secrets)),
            }
        )
        with self.publisher.lock:
            prior = self.get(intent_id)
            if prior:
                if (
                    prior["session_id"] != session_id
                    or prior["request_json"] != request
                ):
                    raise ValueError(
                        "switch intent ID is already bound to another request"
                    )
                if prior["state"] in TERMINAL or prior["state"] == "attention_required":
                    return self._response(prior)
            else:
                session = self.store.get_session(session_id)
                if session is None or session.state != "active":
                    raise ValueError("workspace switching requires an active session")
                if session.workspace_mode == target_mode:
                    raise ValueError(
                        "conversation already uses the requested workspace mode"
                    )
                if self.pending(session_id):
                    raise ValueError(
                        "an earlier workspace switch needs confirmation or recovery"
                    )
                self.require_idle(session)
                files_json, classification, runtime_summary = "[]", "clean", ""
                held: list[dict[str, Any]] = []
                if session.workspace_mode == "isolated":
                    state = self.isolated.validate(session.workspace_id or "")
                    self.sync.register(
                        state.workspace_id,
                        state.source_manifest,
                        state.source_sequence,
                        state.runtime_paths,
                    )
                    self.sync.synchronize(state.workspace_id)
                    with self.store.connection() as con:
                        held = [
                            dict(item)
                            for item in con.execute(
                                (
                                    "SELECT w.task_id,w.attempt,w.proposal_json FROM "
                                    "task_workflows w JOIN tasks t USING(task_id) "
                                    "WHERE "
                                    "t.session_id=? "
                                    "AND w.proposal_json IS NOT NULL "
                                    "AND w.status "
                                    "IN ('awaiting_review','cancelled')"
                                ),
                                (session_id,),
                            ).fetchall()
                        ]
                    # publication_files refuses unresolved incoming divergence and
                    # source conflict markers even for retain/discard switches.
                    files = self.sync.publication_files(state.workspace_id, ())
                    if held and disposition in {"publish", "export"}:
                        overlay = tuple(
                            file
                            for item in held
                            for file in decode_files(item["proposal_json"])
                        )
                        if len({file.relative_path for file in overlay}) != len(
                            overlay
                        ):
                            raise ValueError(
                                "apply overlapping held proposals before switching"
                            )
                        files = self.sync.publication_files(state.workspace_id, overlay)
                    local_workspace = self.store.get_workspace(state.workspace_id)
                    assert local_workspace is not None
                    current_source = self.sync.capture(
                        Path(local_workspace.canonical_path),
                        state.runtime_paths,
                    )
                    with self.store.connection() as con:
                        accepted = json.loads(
                            con.execute(
                                "SELECT manifest_json FROM isolated_sync_state "
                                "WHERE workspace_id=?",
                                (state.workspace_id,),
                            ).fetchone()[0]
                        )
                    screened_dirty = any(
                        (
                            (accepted.get(path) or {}).get("screened")
                            or (current_source.get(path) or {}).get("screened")
                        )
                        and any(
                            (accepted.get(path) or {}).get(key)
                            != (current_source.get(path) or {}).get(key)
                            for key in ("kind", "mode", "digest", "permissions")
                        )
                        for path in accepted.keys() | current_source.keys()
                    )
                    if screened_dirty and disposition not in {"retain", "discard"}:
                        raise ValueError(
                            "changed screened private source requires an explicit "
                            "retain or discard disposition"
                        )
                    files_json = encode_files(files)
                    classification = (
                        "unpublished_screened_source"
                        if screened_dirty
                        else "unpublished_source"
                        if files
                        else "held_proposal"
                        if held
                        else "clean"
                    )
                    with self.store.connection() as con:
                        runtime = con.execute(
                            (
                                "SELECT private_json FROM isolated_runtime_state WHERE "
                                "workspace_id=?"
                            ),
                            (state.workspace_id,),
                        ).fetchone()
                    private_workspace = self.store.get_workspace(state.workspace_id)
                    assert private_workspace is not None
                    observed = {
                        path: _signature(
                            Path(private_workspace.canonical_path),
                            path,
                        )
                        for path in state.runtime_files
                    }
                    if runtime and json.loads(runtime[0]) != observed:
                        runtime_summary = (
                            f"Private runtime inventory changed "
                            f"({len(observed)} roots); "
                            "runtime contents remain private."
                        )
                        if classification == "clean":
                            classification = "runtime_only"
                    if classification == "runtime_only" and disposition not in {
                        "retain",
                        "discard",
                    }:
                        raise ValueError(
                            runtime_summary
                            + " Choose an explicit retain or discard disposition."
                        )
                    if (files or held) and disposition is None:
                        raise ValueError(
                            "unpublished source requires an explicit disposition: "
                            "publish, hold, export, retain, or discard"
                        )
                    if disposition == "publish" and session.agent_mode != "auto":
                        raise ValueError(
                            "normal mode freezes a proposal for /apply; choose hold or "
                            "change to auto mode"
                        )
                scope = self._scope(session.workspace_id or "")
                now = self.store._now(None)
                with self.store.connection() as con, immediate_transaction(con):
                    self.require_idle(session)
                    generation = con.execute(
                        (
                            "SELECT workspace_binding_generation FROM sessions WHERE "
                            "session_id=?"
                        ),
                        (session_id,),
                    ).fetchone()[0]
                    con.execute(
                        (
                            "INSERT INTO mode_switch_intents("
                            "switch_intent_id,session_id,"
                            "source_workspace_id,source_mode,target_mode,source_binding_g"
                            "eneration,source_scope_json,request_json,disposition,classif"
                            "ication,files_json,held_json,runtime_summary,state,created_a"
                            "t,updated_at) VALUES "
                            "(?,?,?,?,?,?,?,?,?,?,?,?,?,'requested',?,?)"
                        ),
                        (
                            intent_id,
                            session_id,
                            session.workspace_id,
                            session.workspace_mode,
                            target_mode,
                            generation,
                            _json(scope),
                            request,
                            disposition,
                            classification,
                            files_json,
                            _json(held),
                            runtime_summary,
                            now,
                            now,
                        ),
                    )
                self.fault("requested")
        return self._advance(intent_id)

    def _set(self, intent_id: str, state: str, **fields: Any) -> None:
        with self.store.connection() as con, immediate_transaction(con):
            assignments = ["state=?", "state_version=state_version+1", "updated_at=?"]
            assignments.extend(f"{name}=?" for name in fields)
            con.execute(
                "UPDATE mode_switch_intents SET "
                + ",".join(assignments)
                + " WHERE switch_intent_id=?",
                (state, self.store._now(None), *fields.values(), intent_id),
            )
        self.fault(state)

    def busy(self, session_id: str) -> bool:
        with self._operation_lock:
            return session_id in self._operations

    def _advance(self, intent_id: str) -> dict[str, Any]:
        row = self.get(intent_id)
        assert row
        owner = str(row["session_id"])
        with self._operation_lock:
            if owner in self._operations:
                raise ValueError("workspace switch setup is still running")
            self._operations.add(owner)
        try:
            return self._advance_owned(intent_id)
        finally:
            with self._operation_lock:
                self._operations.discard(owner)

    def _advance_owned(self, intent_id: str) -> dict[str, Any]:
        row = self.get(intent_id)
        assert row
        if row["state"] in TERMINAL or row["state"] in {
            "confirming",
            "attention_required",
        }:
            return self._response(row)
        try:
            with self.publisher.lock:
                session = self._binding(row)
                self.require_idle(session)
                current_scope = self._scope(row["source_workspace_id"])
                expected_scope = json.loads(row["source_scope_json"])
                proposal = (
                    self.workflow.get(row["proposal_task_id"])
                    if row["proposal_task_id"]
                    else None
                )
                published = proposal and proposal["status"] == "published"
                if row["state"] == "transferring" and published:
                    for key in (
                        "workspace_id",
                        "checkout_id",
                        "canonical_path",
                        "git_dir",
                        "device",
                        "inode",
                        "creation_token",
                        "epoch",
                    ):
                        if current_scope.get(key) != expected_scope.get(key):
                            raise ValueError(
                                "published switch source physical identity changed"
                            )
                elif current_scope != expected_scope:
                    raise ValueError(
                        "source workspace changed after the switch was requested"
                    )
                if row["state"] == "requested":
                    self._set(intent_id, "prepared")
                if row["state"] != "transferring":
                    self._set(intent_id, "target_creating")
            checkout = self.store.get_checkout(session.checkout_id)
            assert checkout
            if row["state"] == "transferring":
                target = self.store.get_workspace(row["target_workspace_id"])
                if target is None or (
                    not published
                    and self._scope(target.workspace_id)
                    != json.loads(row["target_scope_json"])
                ):
                    raise ValueError("recorded switch target changed during recovery")
                notices = tuple(json.loads(row["notices_json"]))
            elif row["target_mode"] == "isolated":
                target, notices = self.isolated.open(
                    checkout,
                    session.session_id,
                    approved_secrets=tuple(json.loads(row["request_json"])["approved"]),
                    creation_key="switch:" + intent_id,
                )
                state = self.isolated.validate(target.workspace_id)
                self.sync.register(
                    target.workspace_id,
                    state.source_manifest,
                    state.source_sequence,
                    state.runtime_paths,
                )
            else:
                target, notices = self.store.ensure_shared_workspace(checkout), ()
            with self.publisher.lock:
                self._binding(row)
                if (
                    not published
                    and self._scope(row["source_workspace_id"]) != expected_scope
                ):
                    raise ValueError("source changed while creating the switch target")
                self._set(
                    intent_id,
                    "transferring",
                    target_workspace_id=target.workspace_id,
                    target_scope_json=_json(self._scope(target.workspace_id)),
                    notices_json=_json(notices),
                )
            # Required commands run without the publication or daemon barrier.
            self._transfer(self.get(intent_id))
            with self.publisher.lock:
                self._binding(row)
                # Publication can legitimately advance sync generations; freeze
                # the post-transfer tuple before issuing the target packet.
                source_scope = self._scope(row["source_workspace_id"])
                target_scope = self._scope(target.workspace_id)
                with self.store.connection() as con, immediate_transaction(con):
                    event = self.store.append_checkout_event(
                        con,
                        checkout_id=session.checkout_id,
                        event_type="session.workspace_switch_requested",
                        caused_by_session_id=session.session_id,
                        payload={
                            "switch_intent_id": intent_id,
                            "source_mode": row["source_mode"],
                            "target_mode": row["target_mode"],
                            "target_workspace_id": target.workspace_id,
                            "binding_generation": row["source_binding_generation"] + 1,
                        },
                        now=self.store._now(None),
                    )
                    con.execute(
                        (
                            "UPDATE mode_switch_intents SET "
                            "state='confirming',state_version=state_version+1,source_conf"
                            "irmation_scope_json=?,target_scope_json=?,bootstrap_sequence"
                            "=?,updated_at=? WHERE switch_intent_id=?"
                        ),
                        (
                            _json(source_scope),
                            _json(target_scope),
                            event.sequence,
                            self.store._now(None),
                            intent_id,
                        ),
                    )
                self.fault("confirming")
        except Exception as exc:
            self._set(
                intent_id,
                "attention_required",
                error=type(exc).__name__ + ": " + str(exc),
            )
            raise
        return self._response(self.get(intent_id))

    def _transfer(self, row: dict[str, Any] | None) -> None:
        assert row
        files = decode_files(row["files_json"])
        disposition = row["disposition"]
        if disposition == "publish" and files:
            self._hold(row)
            frozen = self._scope(row["source_workspace_id"])

            def unchanged() -> None:
                if self._scope(row["source_workspace_id"]) != frozen:
                    raise ValueError(
                        "private source or runtime changed during switch checks"
                    )

            proposal_task = "switch-proposal-" + row["switch_intent_id"]
            publication = self.workflow.apply(proposal_task, before_publish=unchanged)
            if publication["state"] != "published":
                raise ValueError(
                    "source publication conflicted; retained source needs attention"
                )
            action = self.store.get_task(publication["task_id"])
            execution = (
                self.store.get_execution(action.task_id, action.attempt)
                if action
                else None
            )
            assert execution is not None
            with self.store.connection() as con:
                for held in json.loads(row["held_json"]):
                    con.execute(
                        "UPDATE task_workflows SET status='published',publication_id=? "
                        "WHERE task_id=? AND attempt=?",
                        (execution.execution_id, held["task_id"], held["attempt"]),
                    )
        elif disposition == "hold" and files:
            self._hold(row)
        elif disposition == "export" and (files or json.loads(row["held_json"])):
            self._export(row)

    def _hold(self, row: dict[str, Any]) -> None:
        # A dedicated terminal task gives manual private edits a normal /apply
        # target without rewriting any completed execution or held proposal.
        task_id = "switch-proposal-" + row["switch_intent_id"]
        session = self.store.get_session(row["session_id"])
        checkout = self.store.get_checkout(session.checkout_id) if session else None
        assert checkout
        now = self.store._now(None)
        with self.store.connection() as con, immediate_transaction(con):
            con.execute(
                (
                    "UPDATE mode_switch_intents SET proposal_task_id=? WHERE "
                    "switch_intent_id=?"
                ),
                (task_id, row["switch_intent_id"]),
            )
        task = self.store.create_task(
            repository_id=checkout.repository_id,
            task_id=task_id,
            title="Private source retained while switching workspace",
            session_id=row["session_id"],
        )
        state = self.isolated.validate(row["source_workspace_id"])
        with self.store.connection() as con, immediate_transaction(con):
            config = con.execute(
                "SELECT config_json FROM check_configurations WHERE checkout_id=?",
                (checkout.checkout_id,),
            ).fetchone()
            configuration = json.loads(config[0]) if config else {}
            configuration["runtime_paths"] = sorted(
                set(configuration.get("runtime_paths", [])) | set(state.runtime_files)
            )
            con.execute(
                (
                    "INSERT OR IGNORE INTO "
                    "task_workflows(task_id,attempt,publish_mode,agent_mode,propo"
                    "sal_json,config_json,status,created_at) VALUES "
                    "(?,1,'review','normal',?,?,'awaiting_review',?)"
                ),
                (task_id, row["files_json"], _json(configuration), now),
            )
            con.execute(
                "UPDATE tasks SET state='completed',updated_at=? WHERE task_id=?",
                (now, task.task_id),
            )
            con.execute(
                (
                    "INSERT OR IGNORE INTO switch_proposal_bindings VALUES "
                    "(?,1,?,'[\"*\"]')"
                ),
                (task_id, row["source_workspace_id"]),
            )
        self.isolated.runtime.pin_task(row["source_workspace_id"], task_id, 1)

    def _export(self, row: dict[str, Any]) -> None:
        # The immutable bundle includes binary bodies, modes, symlinks and exact
        # before identities. It never includes runtime or screened source bytes.
        root = self.publisher.candidate_dir / "switch-exports"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or root.stat().st_mode & 0o077:
            raise ValueError("switch export directory must be private")
        bundle = _json(
            {
                "version": 1,
                "source": json.loads(row["files_json"]),
                "held_proposals": json.loads(row["held_json"]),
            }
        )
        digest = hashlib.sha256(bundle.encode()).hexdigest()
        path = root / (digest + ".patch.json")
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            if path.is_symlink() or path.read_text() != bundle:
                raise ValueError("switch export artifact identity changed") from None
        else:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(bundle)
                handle.flush()
                os.fsync(handle.fileno())
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        patch = portable_patch(decode_files(row["files_json"]))
        patch_path = root / (digest + ".patch")
        try:
            descriptor = os.open(
                patch_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            if patch_path.is_symlink() or patch_path.read_bytes() != patch:
                raise ValueError("switch patch artifact identity changed") from None
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(patch)
                handle.flush()
                os.fsync(handle.fileno())
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        with self.store.connection() as con:
            con.execute(
                "UPDATE mode_switch_intents SET export_path=? WHERE switch_intent_id=?",
                (str(patch_path), row["switch_intent_id"]),
            )

    def confirm(self, session_id: str, intent_id: str, sequence: int) -> dict[str, Any]:
        with self.publisher.lock:
            if self.busy(session_id):
                raise ValueError("workspace switch setup is still running")
            row = self.get(intent_id)
            if row is None or row["session_id"] != session_id:
                raise ValueError("switch intent does not belong to this conversation")
            if row["state"] == "confirmed":
                return self._response(row)
            if row["state"] != "confirming" or row["bootstrap_sequence"] != sequence:
                raise ValueError(
                    "consume the exact target workspace bootstrap before confirming"
                )
            session = self._binding(row)
            self.require_idle(session)
            if self._scope(row["source_workspace_id"]) != json.loads(
                row["source_confirmation_scope_json"]
            ) or self._scope(row["target_workspace_id"]) != json.loads(
                row["target_scope_json"]
            ):
                self._set(
                    intent_id,
                    "attention_required",
                    error="workspace scope changed before confirmation",
                )
                raise ValueError("workspace scope changed before confirmation")
            now = self.store._now(None)
            with self.store.connection() as con, immediate_transaction(con):
                changed = con.execute(
                    (
                        "UPDATE sessions SET "
                        "workspace_id=?,workspace_mode=?,workspace_binding_generation"
                        "=workspace_binding_generation+1,state_version=state_version+"
                        "1,updated_at=? WHERE session_id=? AND workspace_id=? AND "
                        "workspace_binding_generation=?"
                    ),
                    (
                        row["target_workspace_id"],
                        row["target_mode"],
                        now,
                        session_id,
                        row["source_workspace_id"],
                        row["source_binding_generation"],
                    ),
                ).rowcount
                if changed != 1:
                    raise ValueError(
                        "source workspace binding changed during confirmation"
                    )
                conversation = con.execute(
                    "SELECT conversation_json FROM sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
                if conversation is not None:
                    snapshot = json.loads(conversation)
                    if not isinstance(snapshot, dict):
                        raise ValueError("stored conversation checkpoint is malformed")
                    snapshot["coordination_sequence"] = None
                    con.execute(
                        "UPDATE sessions SET conversation_json=?,conversation_revision="
                        "conversation_revision+1 WHERE session_id=?",
                        (_json(snapshot), session_id),
                    )
                if row["source_mode"] == "isolated" and row["disposition"] == "retain":
                    con.execute(
                        (
                            "INSERT OR IGNORE INTO retained_switch_workspaces VALUES "
                            "(?,?,?,?)"
                        ),
                        (row["source_workspace_id"], intent_id, "retain", now),
                    )
                con.execute(
                    "DELETE FROM workspace_read_observations WHERE session_id=?",
                    (session_id,),
                )
                con.execute(
                    (
                        "UPDATE session_cursors SET "
                        "transport_received_sequence=?,context_consumed_sequence=?,la"
                        "st_delivered_sequence=?,updated_at=? WHERE session_id=?"
                    ),
                    (sequence, sequence, sequence, now, session_id),
                )
                self.store.append_checkout_event(
                    con,
                    checkout_id=session.checkout_id,
                    event_type="session.workspace_switched",
                    caused_by_session_id=session_id,
                    payload={
                        "switch_intent_id": intent_id,
                        "workspace_id": row["target_workspace_id"],
                        "mode": row["target_mode"],
                        "binding_generation": row["source_binding_generation"] + 1,
                    },
                    now=now,
                )
                con.execute(
                    (
                        "UPDATE mode_switch_intents SET "
                        "state='confirmed',state_version=state_version+1,terminal_at="
                        "?,updated_at=? WHERE switch_intent_id=?"
                    ),
                    (now, now, intent_id),
                )
                result = self._response(
                    dict(
                        con.execute(
                            (
                                "SELECT * FROM mode_switch_intents "
                                "WHERE switch_intent_id=?"
                            ),
                            (intent_id,),
                        ).fetchone()
                    )
                )
                result["session"] = asdict(
                    self.store.session_from_row(
                        con.execute(
                            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
                        ).fetchone()
                    )
                )
                con.execute(
                    (
                        "UPDATE mode_switch_intents SET result_json=? WHERE "
                        "switch_intent_id=?"
                    ),
                    (_json(result), intent_id),
                )
            self.fault("confirmed")
            return result

    def _response(self, row: dict[str, Any] | None) -> dict[str, Any]:
        assert row
        if row["result_json"]:
            result = json.loads(row["result_json"])
            assert isinstance(result, dict)
            return result
        session = self.store.get_session(row["session_id"])
        workspace = self.store.get_workspace(
            row["target_workspace_id"] or row["source_workspace_id"]
        )
        return {
            "switch_intent_id": row["switch_intent_id"],
            "state": row["state"],
            "session": asdict(session) if session else None,
            "workspace": asdict(workspace) if workspace else None,
            "bootstrap_sequence": row["bootstrap_sequence"],
            "classification": row["classification"],
            "runtime_summary": row["runtime_summary"],
            "workspace_notices": json.loads(row["notices_json"]),
            "proposal_task_id": row["proposal_task_id"],
            "export_path": row["export_path"],
            "export_artifact_path": row["export_path"] + ".json"
            if row["export_path"]
            else None,
            "error": row["error"],
        }

    def recover(self) -> None:
        with self.store.connection() as con:
            rows = con.execute(
                "SELECT switch_intent_id FROM mode_switch_intents WHERE "
                "state NOT IN ('confirmed','rolled_back','attention_required'"
                ",'confirming')"
            ).fetchall()
        for row in rows:
            try:
                intent = self.get(str(row[0]))
                session = (
                    self.store.get_session(intent["session_id"]) if intent else None
                )
                if (
                    intent
                    and intent["disposition"] == "hold"
                    and session
                    and session.state != "active"
                ):
                    continue
                if intent and intent["state"] == "rollback_pending":
                    self.rollback(intent["session_id"], str(row[0]))
                else:
                    self._advance(str(row[0]))
            except (ValueError, OSError, LlmCoordError):
                pass

    def rollback(self, session_id: str, intent_id: str) -> dict[str, Any]:
        with self.publisher.lock:
            if self.busy(session_id):
                raise ValueError("workspace switch setup is still running")
            row = self.get(intent_id)
            if (
                row is None
                or row["session_id"] != session_id
                or row["state"] == "confirmed"
            ):
                raise ValueError("confirmed switches cannot reopen their old binding")
            self._binding(row)
            self._set(intent_id, "rollback_pending")
            # Target worktrees and every candidate remain retained for inspection.
            if row["target_mode"] == "isolated" and row["target_workspace_id"]:
                with self.store.connection() as con:
                    con.execute(
                        (
                            "INSERT OR IGNORE INTO retained_switch_workspaces VALUES "
                            "(?,?,?,?)"
                        ),
                        (
                            row["target_workspace_id"],
                            intent_id,
                            "rollback",
                            self.store._now(None),
                        ),
                    )
            self._set(intent_id, "rolled_back", terminal_at=self.store._now(None))
            return self._response(self.get(intent_id))
