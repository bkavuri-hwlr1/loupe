"""Durable proposals, review actions, cancellation and task-local diffs.

Only the coordinator and existing batch journal apply checkout changes. A held
proposal carries no live claim; applying it is a new fenced task.
"""

from __future__ import annotations

import base64
import difflib
import functools
import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from llm_cli.agent.modes import publication_mode, validate_agent_mode
from llm_cli.agent.source_policy import ensure_safe_content
from llm_cli.coordination.coordinator import RepositoryCoordinator
from llm_cli.coordination.models import (
    SessionRecord,
    WorkspaceExecutionRecord,
    WorkspaceRecord,
)
from llm_cli.errors import LlmCoordError
from llm_cli.execution.workspace import Execution
from llm_cli.ids import new_id
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.batches import (
    BatchFile,
    SharedBatchPublisher,
    _identity_from_json,
    _identity_json,
    candidate_target,
    result_identity,
)
from llm_cli.workspace.identity import (
    DIRECTORY_MODE,
    SYMLINK_MODE,
    ObjectKind,
    read_identified_path,
)
from llm_cli.workspace.merge import has_conflict_markers, merge_text

if TYPE_CHECKING:
    from llm_cli.execution.environment import EnvironmentProvider, EnvironmentSupplier
    from llm_cli.workspace.isolated_sync import IsolatedSynchronizer


def encode_files(files: tuple[BatchFile, ...]) -> str:
    return json.dumps(
        [
            {
                "path": f.relative_path,
                "base": _identity_json(f.base),
                "mode": f.mode,
                "permissions": f.permissions,
                **({"binary": True} if f.binary else {}),
                "content": base64.b64encode(f.content).decode()
                if f.content is not None
                else None,
                "original": base64.b64encode(f.original).decode()
                if f.original is not None
                else None,
            }
            for f in files
        ],
        sort_keys=True,
    )


def decode_files(value: str) -> tuple[BatchFile, ...]:
    return tuple(
        BatchFile(
            item["path"],
            _identity_from_json(item["base"]),
            base64.b64decode(item["content"], validate=True)
            if item["content"] is not None
            else None,
            item["mode"],
            base64.b64decode(item["original"], validate=True)
            if item["original"] is not None
            else None,
            item.get("permissions"),
            item.get("binary", False),
        )
        for item in json.loads(value)
    )


class TaskWorkflow:
    def __init__(
        self,
        store: ControlStore,
        coordinator: RepositoryCoordinator,
        publisher: SharedBatchPublisher,
    ) -> None:
        self.store, self.coordinator, self.publisher = store, coordinator, publisher
        self.isolated_sync: IsolatedSynchronizer | None = None
        self.command_environment: EnvironmentProvider | None = None

    def reconcile(self) -> None:
        """Repair disposition after journal settlement without replaying actions."""
        with self.store.connection() as con:
            con.execute(
                """UPDATE task_workflows SET status='published',
                publication_id=execution_id, expires_at=COALESCE(expires_at, ?)
                WHERE execution_id IN (SELECT batch_id FROM workspace_batches
                    WHERE state='published')
                    AND status IN ('running','awaiting_review')""",
                (self.store._now(None) + self.coordinator.terminal_retention_ms,),
            )
            # Unchanged completions publish no batch. Settlement commits before
            # the disposition, so an interrupted daemon can leave it running,
            # which would retain a closed workspace forever.
            con.execute(
                """UPDATE task_workflows SET status='published',
                expires_at=COALESCE(expires_at, ?)
                WHERE status='running' AND execution_id IN (
                    SELECT e.execution_id FROM all_task_executions e
                    JOIN tasks t ON t.task_id=e.task_id
                        AND t.attempt=e.task_attempt
                    WHERE e.state='published' AND t.state='completed')""",
                (self.store._now(None) + self.coordinator.terminal_retention_ms,),
            )
            rows = con.execute(
                "SELECT * FROM task_workflows WHERE action_task_id IS NO"
                "T NULL OR undo_task_id IS NOT NULL"
            ).fetchall()
            for row in rows:
                for key, status in (
                    ("action_task_id", "published"),
                    ("undo_task_id", "undone"),
                ):
                    if not row[key]:
                        continue
                    child = con.execute(
                        "SELECT state FROM tasks WHERE task_id=?", (row[key],)
                    ).fetchone()
                    if child and child[0] == "completed":
                        if key == "action_task_id":
                            publication = con.execute(
                                "SELECT e.execution_id FROM all_task_executions e "
                                "JOIN tasks t ON t.task_id=e.task_id "
                                "AND t.attempt=e.task_attempt WHERE t.task_id=?",
                                (row[key],),
                            ).fetchone()
                            if publication:
                                con.execute(
                                    "UPDATE task_workflows SET publication_id=? "
                                    "WHERE task_id=? AND attempt=?",
                                    (publication[0], row["task_id"], row["attempt"]),
                                )
                        con.execute(
                            (
                                "UPDATE task_workflows SET status=?, "
                                "expires_at=COALESCE(expires_at,?) "
                                "WHERE task_id=? AND attempt=?"
                            ),
                            (
                                status,
                                self.store._now(None)
                                + self.coordinator.terminal_retention_ms,
                                row["task_id"],
                                row["attempt"],
                            ),
                        )
                        con.execute(
                            (
                                "UPDATE tasks SET state='completed' "
                                "WHERE task_id=? AND attempt=?"
                            ),
                            (row["task_id"], row["attempt"]),
                        )
                    elif child and child[0] in {"failed", "cancelled"}:
                        batch = con.execute(
                            (
                                "SELECT b.state FROM all_task_executions e "
                                "JOIN workspace_batches b "
                                "ON b.batch_id=e.execution_id WHERE e.task_id=?"
                            ),
                            (row[key],),
                        ).fetchone()
                        if batch is None or batch[0] == "diverged":
                            con.execute(
                                f"UPDATE task_workflows SET {key}=NULL "
                                "WHERE task_id=? AND attempt=?",
                                (row["task_id"], row["attempt"]),
                            )
            # Pending proposals and unresolved journals have no expiry.
            con.execute(
                """UPDATE task_workflows SET proposal_json=NULL
                WHERE expires_at IS NOT NULL AND expires_at<=?
                    AND status IN ('published','undone','discarded')
                    AND execution_id NOT IN (SELECT batch_id FROM workspace_batches
                        WHERE state IN ('applying','operator_attention'))""",
                (self.store._now(None),),
            )

    def get(self, task_id: str, attempt: int | None = None) -> dict[str, Any] | None:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError("task")
        with self.store.connection() as con:
            row = con.execute(
                "SELECT * FROM task_workflows WHERE task_id=? AND attempt=?",
                (task_id, attempt or task.attempt),
            ).fetchone()
        return dict(row) if row else None

    def ensure(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        assert task is not None
        session = self.store.get_session(task.session_id or "")
        launch = self.store.get_execution_launch(task_id, task.attempt)
        default_mode = session.agent_mode if session else "auto"
        agent_mode = validate_agent_mode(
            launch.parameters.get("agent_mode", default_mode)
            if launch
            else default_mode
        )
        with self.store.connection() as con:
            config = con.execute(
                "SELECT config_json FROM check_configurations WHERE checkout_id=?",
                (session.checkout_id if session else "",),
            ).fetchone()
            mode = con.execute(
                "SELECT mode FROM session_publication_policy WHERE session_id=?",
                (task.session_id,),
            ).fetchone()
            con.execute(
                (
                    "INSERT OR IGNORE INTO task_workflows(task_id,attempt,pu"
                    "blish_mode,config_json,created_at,agent_mode) VALUES (?,?,?,?,?,?)"
                ),
                (
                    task_id,
                    task.attempt,
                    publication_mode(agent_mode)
                    if launch and "agent_mode" in launch.parameters
                    else mode[0]
                    if mode
                    else publication_mode(agent_mode),
                    config[0] if config else "{}",
                    self.store._now(None),
                    agent_mode,
                ),
            )
        result = self.get(task_id)
        assert result is not None
        return result

    def update(self, task_id: str, attempt: int, **fields: Any) -> None:
        allowed = {
            "config_json",
            "execution_id",
            "proposal_json",
            "status",
            "stop_requested",
            "publication_id",
            "action_task_id",
            "undo_task_id",
            "expires_at",
        }
        if not fields or not set(fields) <= allowed:
            raise ValueError("invalid workflow update")
        with self.store.connection() as con:
            con.execute(
                "UPDATE task_workflows SET "
                + ",".join(f"{key}=?" for key in fields)
                + " WHERE task_id=? AND attempt=?",
                (*fields.values(), task_id, attempt),
            )

    def stopped(self, task_id: str, attempt: int) -> bool:
        row = self.get(task_id, attempt)
        return bool(row and row["stop_requested"])

    def retain(
        self,
        task_id: str,
        attempt: int,
        execution_id: str,
        files: tuple[BatchFile, ...],
    ) -> None:
        self.ensure(task_id)
        task = self.store.get_task(task_id)
        session = self.store.get_session(task.session_id or "") if task else None
        if (
            files
            and session
            and session.workspace_mode == "isolated"
            and self.isolated_sync
        ):
            from llm_cli.workspace.runtime_refresh import RuntimeRefresher

            workspace = self.store.get_workspace(session.workspace_id or "")
            assert workspace is not None
            RuntimeRefresher(
                self.store, Path(workspace.canonical_path).parent, self.publisher.lock
            ).pin_task(workspace.workspace_id, task_id, attempt)
        self.update(
            task_id,
            attempt,
            execution_id=execution_id,
            proposal_json=encode_files(files),
        )

    def hold(
        self,
        task_id: str,
        attempt: int,
        execution_id: str,
        *,
        cancelled: bool = False,
        completion_outcome: str = "completed",
        verification: str | None = None,
        file_count: int | None = None,
    ) -> None:
        # Persist disposition before releasing authority, so startup cannot
        # accidentally resume a completed-but-held proposal.
        status = "cancelled" if cancelled else "awaiting_review"
        from llm_cli.workspace.runtime_refresh import RuntimeRefresher

        frozen_config = RuntimeRefresher(
            self.store, self.publisher.candidate_dir, self.publisher.lock
        ).task_config(task_id, attempt)
        self.update(
            task_id,
            attempt,
            status=status,
            **({"config_json": frozen_config} if frozen_config is not None else {}),
        )
        execution = self.store.get_execution_by_id(execution_id)
        settle = (
            self.coordinator.settle_workspace_execution
            if isinstance(execution, WorkspaceExecutionRecord)
            else self.coordinator.settle_shared_execution
        )
        settle(
            execution_id,
            outcome="failed_safe",
            failure_code="CANCELLED" if cancelled else "REVIEW_REQUIRED",
        )
        self.store.record_task_event(
            claim_id=None,
            task_id=task_id,
            event_type=f"workflow.{status}",
            payload={
                "attempt": attempt,
                "completion_outcome": completion_outcome,
                "verification": verification,
                "file_count": file_count,
            },
        )

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError("task")
        row = self.ensure(task_id)
        claim = self.store.get_claim(task.current_claim_id or "")
        execution = self.store.get_execution(task_id, task.attempt)
        if (claim and claim.state.value in {"publishing", "active_integration"}) or (
            isinstance(execution, WorkspaceExecutionRecord)
            and execution.state == "publishing"
        ):
            return {
                "state": "publishing",
                "message": (
                    "Publication has begun; it must finish or recover before stopping."
                ),
            }
        if (
            task.state in {"completed", "failed", "cancelled"}
            or row["status"] == "awaiting_review"
        ):
            return {
                "state": row["status"] if row["status"] != "running" else task.state
            }
        with self.store.connection() as con:
            changed = con.execute(
                """UPDATE task_workflows SET stop_requested=1
                WHERE task_id=? AND attempt=? AND NOT EXISTS (
                    SELECT 1 FROM claims WHERE task_id=? AND task_attempt=?
                        AND state IN ('publishing','active_integration'))
                    AND NOT EXISTS (SELECT 1 FROM workspace_executions
                        WHERE task_id=? AND task_attempt=? AND state='publishing')""",
                (task_id, task.attempt, task_id, task.attempt, task_id, task.attempt),
            ).rowcount
        if not changed:
            return {"state": "publishing"}
        if claim and claim.state.value == "queued":
            self.coordinator.release_claim(claim_id=claim.claim_id, reason="cancelled")
            self.update(task_id, task.attempt, status="cancelled")
        self.store.record_task_event(
            claim_id=None, task_id=task_id, event_type="workflow.stopping", payload={}
        )
        return {"state": "stopping"}

    def inspect(self, task_id: str) -> dict[str, Any]:
        row = self.get(task_id)
        if row is None:
            return {
                "task_id": task_id,
                "status": "legacy",
                "diff": "Task-local history unavailable for this older task.",
                "undo_available": False,
            }
        value = row["proposal_json"]
        if value is None and row["execution_id"] and row["status"] == "running":
            # Running tasks expose their latest durable checkpoint, never a
            # concurrently-mutating in-memory overlay.
            from llm_cli.agent.shared_tools import SharedToolBroker

            saved = self.store.get_execution_checkpoint(row["execution_id"])
            execution = self.store.get_execution(task_id, row["attempt"])
            claim = (
                self.store.get_claim(execution.claim_id)
                if execution and execution.claim_id
                else None
            )
            if (
                saved
                and execution
                and (claim or isinstance(execution, WorkspaceExecutionRecord))
            ):
                scopes = (
                    execution.scopes
                    if isinstance(execution, WorkspaceExecutionRecord)
                    else claim.scopes
                    if claim
                    else ()
                )
                broker = SharedToolBroker(Path(execution.worktree_path), scopes)
                usage = saved.checkpoint.get("tool_usage", {})
                if not isinstance(usage, dict):
                    raise ValueError("Invalid saved tool state")
                broker.restore_usage(usage)
                value = encode_files(broker.candidates())
        files = decode_files(value) if value else ()
        sections: list[str] = []
        for f in files:
            if f.mode == DIRECTORY_MODE:
                sections.append(f"Create directory {f.relative_path}/\n")
                continue
            if f.binary:
                sections.append(
                    f"Binary change {f.relative_path}: {len(f.original or b'')} -> "
                    f"{len(f.content or b'')} bytes\n"
                )
                continue
            if f.mode == SYMLINK_MODE or f.base.kind is ObjectKind.SYMLINK:
                sections.append(
                    f"Symlink {f.relative_path}: {f.original!r} -> {f.content!r}\n"
                )
                continue
            sections.extend(
                difflib.unified_diff(
                    (f.original or b"").decode().splitlines(keepends=True),
                    (f.content or b"").decode().splitlines(keepends=True),
                    fromfile="/dev/null" if f.base.absent else "a/" + f.relative_path,
                    tofile="/dev/null" if f.content is None else "b/" + f.relative_path,
                )
            )
        diff = "".join(sections)
        return {
            "task_id": task_id,
            "status": row["status"],
            "paths": [f.relative_path for f in files],
            "diff": diff[: 512 * 1024],
            "truncated": len(diff) > 512 * 1024,
            "undo_available": bool(files)
            and row["status"] == "published"
            and not row["undo_task_id"],
            "checks": self.checks(task_id),
        }

    def checks(self, task_id: str) -> list[dict[str, Any]]:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError("task")
        with self.store.connection() as con:
            rows = con.execute(
                (
                    "SELECT run_id,name,state,exit_code,duration,truncated,c"
                    "reated_at FROM check_runs WHERE task_id=? AND attempt=?"
                    " ORDER BY created_at,run_id"
                ),
                (task_id, task.attempt),
            ).fetchall()
        return [dict(row) for row in rows]

    def discard(self, task_id: str) -> dict[str, Any] | None:
        row = self.get(task_id)
        if not row or row["status"] not in {
            "awaiting_review",
            "cancelled",
            "discarded",
        }:
            return None
        self.update(
            task_id,
            row["attempt"],
            status="discarded",
            proposal_json=None,
            expires_at=self.store._now(None) + self.coordinator.terminal_retention_ms,
        )
        with self.store.connection() as con:
            con.execute(
                "UPDATE tasks SET state='cancelled' WHERE task_id=? AND attempt=?",
                (task_id, row["attempt"]),
            )
        return {"state": "discarded", "task_id": task_id}

    def _task_session(
        self, session: SessionRecord, task_id: str, attempt: int
    ) -> SessionRecord:
        """Retained proposals keep the workspace/runtime of their original turn."""
        execution = self.store.get_execution(task_id, attempt)
        workspace_id = execution.workspace_id if execution else None
        if workspace_id is None:
            with self.store.connection() as con:
                binding = con.execute(
                    "SELECT workspace_id FROM switch_proposal_bindings "
                    "WHERE task_id=? AND attempt=?",
                    (task_id, attempt),
                ).fetchone()
            workspace_id = str(binding[0]) if binding else None
        workspace = self.store.get_workspace(workspace_id or "")
        return (
            replace(
                session,
                workspace_id=workspace.workspace_id,
                workspace_mode=workspace.mode,
            )
            if workspace
            else session
        )

    def _publication_workspace(
        self,
        session: SessionRecord,
        *,
        allow_removed: bool = False,
        task_id: str | None = None,
        attempt: int | None = None,
    ) -> tuple[WorkspaceRecord, Path | None, str | None]:
        """Resolve a retained action to shared source and its private runtime.

        A held proposal is frozen. Later isolated edits are preserved locally,
        never swept into this action. Checks therefore materialize current shared
        source plus only that proposal, while copying the owning session's runtime.
        """
        workspace = self.store.get_workspace(session.workspace_id or "")
        if workspace is None:
            raise ValueError("The task's workspace is unavailable.")
        if session.workspace_mode != "isolated":
            return workspace, None, None
        if self.isolated_sync is None:
            raise ValueError(
                "Isolated publication requires the isolated batch publisher."
            )
        with self.store.connection() as con:
            active = con.execute(
                "SELECT 1 FROM claims c JOIN tasks t ON t.task_id=c.task_id "
                "JOIN sessions s ON s.session_id=t.session_id "
                "WHERE s.workspace_id=? AND c.state IN "
                "('active_work','publishing','active_integration') "
                "UNION ALL SELECT 1 FROM workspace_executions e "
                "WHERE e.workspace_id=? AND e.state IN "
                "('preparing','running','validated','publishing','operator_attention') "
                "LIMIT 1",
                (workspace.workspace_id, workspace.workspace_id),
            ).fetchone()
        if active is not None:
            raise ValueError(
                "Wait for the isolated session's active task before "
                "applying or undoing."
            )
        with self.store.connection() as con:
            binding = con.execute(
                "SELECT source_workspace_id FROM workspaces WHERE workspace_id=?",
                (workspace.workspace_id,),
            ).fetchone()
        source = self.store.get_workspace(str(binding[0])) if binding else None
        if (
            source is None
            or source.mode != "shared"
            or source.state != "active"
            or source.checkout_id != workspace.checkout_id
        ):
            raise ValueError("The isolated workspace's shared checkout is unavailable.")
        if workspace.state == "removed":
            if allow_removed:
                # Undo uses the retained published batch's exact identities;
                # no private files or runtime remain necessary after close.
                return source, None, None
            raise ValueError("The isolated proposal's private workspace was removed.")
        runtime_root = Path(workspace.canonical_path)
        if task_id is not None and attempt is not None:
            from llm_cli.workspace.runtime_refresh import RuntimeRefresher

            runtime_root = RuntimeRefresher(
                self.store, runtime_root.parent, self.publisher.lock
            ).task_root(workspace.workspace_id, task_id, attempt)
        # Pin legacy held proposals before the first newly supported refresh.
        self.isolated_sync.synchronize(workspace.workspace_id)
        return source, runtime_root, workspace.workspace_id

    def apply(
        self,
        task_id: str,
        *,
        undo: bool = False,
        allow_unverified: bool = False,
        environment: EnvironmentSupplier | None = None,
        before_publish: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        with self._apply_catch_up() as follow_up, self.publisher.lock:
            self.reconcile()
            row = self.get(task_id)
            # Undo and completed/recovering actions retain their exact-base
            # semantics, and invalid actions fail before starting checks.
            if (
                undo
                or row is None
                or row["proposal_json"] is None
                or row["action_task_id"]
                or row["status"] not in {"awaiting_review", "cancelled"}
                or row["agent_mode"] == "plan"
            ):
                return self._apply(
                    task_id,
                    undo=undo,
                    allow_unverified=allow_unverified,
                    follow_up=follow_up,
                )
            task = self.store.get_task(task_id)
            assert task is not None
            session = self.store.get_session(task.session_id or "")
            if session is None:
                raise ValueError("Apply currently requires a shared-session task.")
            session = self._task_session(session, task_id, int(row["attempt"]))
            workspace, runtime_root, _ = self._publication_workspace(
                session, task_id=task_id, attempt=int(row["attempt"])
            )
            sensitive_paths: tuple[str, ...] = ()
            if runtime_root is not None:
                with self.store.connection() as con:
                    private = con.execute(
                        "SELECT source_manifest_json FROM isolated_workspaces "
                        "WHERE workspace_id=?",
                        (session.workspace_id,),
                    ).fetchone()
                assert private is not None
                sensitive_paths = tuple(
                    path
                    for path, entry in json.loads(private[0]).items()
                    if entry.get("screened")
                )
            root = Path(workspace.canonical_path)
            files = self._merge_proposal(root, decode_files(row["proposal_json"]))

        if not allow_unverified:
            from llm_cli.execution.checks import check_retained_proposal

            # Configured commands must never hold the shared publication lock.
            failure = check_retained_proposal(
                self,
                row,
                root,
                files,
                runtime_root=runtime_root,
                sensitive_paths=sensitive_paths,
                environment=environment
                or (
                    functools.partial(self.command_environment, session.session_id)
                    if self.command_environment is not None
                    else None
                ),
            )
            if failure is not None:
                raise ValueError(failure)
        with self._apply_catch_up() as follow_up, self.publisher.lock:
            current = self.get(task_id)
            if current is None or current["proposal_json"] != row["proposal_json"]:
                raise ValueError("Proposal changed during checks; retry apply.")
            if before_publish is not None:
                before_publish()
            return self._apply(
                task_id,
                undo=False,
                allow_unverified=allow_unverified,
                prepared=files,
                follow_up=follow_up,
            )

    @contextmanager
    def _apply_catch_up(self) -> Iterator[list[tuple[str, str]]]:
        follow_up: list[tuple[str, str]] = []
        try:
            yield follow_up
        finally:
            # This context precedes the apply lock, so it exits after the
            # confirmed publication and settlement have released that lock.
            for task_id, workspace_id in follow_up:
                assert self.isolated_sync is not None
                try:
                    self.isolated_sync.synchronize(workspace_id)
                except (ValueError, OSError, LlmCoordError):
                    self.store.record_task_event(
                        claim_id=None,
                        task_id=task_id,
                        event_type="workspace.isolated_sync_pending",
                        payload={
                            "message": (
                                "Published to the shared checkout; private "
                                "synchronization needs recovery."
                            )
                        },
                    )

    @staticmethod
    def _merge_proposal(
        root: Path, files: tuple[BatchFile, ...]
    ) -> tuple[BatchFile, ...]:
        merged: list[BatchFile] = []
        conflicts: list[str] = []
        for f in files:
            try:
                current, theirs = read_identified_path(
                    candidate_target(root, f.relative_path)
                )
                if (
                    not f.binary
                    and f.mode != SYMLINK_MODE
                    and has_conflict_markers(f.content)
                ):
                    raise ValueError("unresolved merge")
                if current == f.base:
                    merged.append(f)
                    continue
                # Delete/modify, add/add and type changes require a fresh edit.
                if (
                    f.binary
                    or f.base.kind is not ObjectKind.REGULAR
                    or current.kind is not ObjectKind.REGULAR
                    or f.original is None
                    or f.content is None
                    or theirs is None
                    or f.mode == DIRECTORY_MODE
                ):
                    raise ValueError("unmergeable change")
                ensure_safe_content(theirs)
                result = merge_text(f.original, f.content, theirs)
                if result.conflicts or has_conflict_markers(result.content):
                    raise ValueError("overlapping edits")
                merged.append(
                    BatchFile(
                        f.relative_path,
                        current,
                        result.content,
                        current.mode if f.mode == f.base.mode else f.mode,
                        theirs,
                        current.permissions,
                    )
                )
            except (ValueError, OSError, RuntimeError, LlmCoordError):
                conflicts.append(f.relative_path)
        if conflicts:
            raise ValueError(
                "Conflict at "
                + ", ".join(conflicts)
                + "; no files changed. Ask the session to make the change again."
            )
        return tuple(merged)

    def _apply(
        self,
        task_id: str,
        *,
        undo: bool,
        allow_unverified: bool,
        prepared: tuple[BatchFile, ...] | None = None,
        follow_up: list[tuple[str, str]],
    ) -> dict[str, Any]:
        row = self.get(task_id)
        if row is None or row["proposal_json"] is None:
            raise ValueError("This task has no retained proposal or undo history.")
        if row["agent_mode"] == "plan":
            raise ValueError("Plan tasks have no changes to apply or undo.")
        action_key = "undo_task_id" if undo else "action_task_id"
        if row[action_key]:
            child = self.store.get_task(row[action_key])
            if child and child.state == "completed":
                publication = self.store.get_execution(child.task_id, child.attempt)
                self.update(
                    task_id,
                    row["attempt"],
                    status="undone" if undo else "published",
                    **(
                        {"publication_id": publication.execution_id}
                        if not undo and publication
                        else {}
                    ),
                )
                return {
                    "state": "undone" if undo else "published",
                    "task_id": child.task_id,
                }
            raise ValueError(
                "This action already exists; inspect its task and run ta"
                "sk recover if needed."
            )
        if undo and row["status"] != "published":
            raise ValueError("Only a published task can be undone.")
        if not undo and row["status"] == "published":
            return {"state": "published", "task_id": task_id}
        if not undo and row["status"] not in {"awaiting_review", "cancelled"}:
            raise ValueError("Only a retained, settled proposal can be applied.")
        task = self.store.get_task(task_id)
        assert task is not None
        session = self.store.get_session(task.session_id or "")
        if session is None:
            raise ValueError("Apply and undo currently require a shared-session task.")
        session = self._task_session(session, task_id, int(row["attempt"]))
        workspace, runtime_root, isolated_workspace_id = self._publication_workspace(
            session,
            allow_removed=undo,
            task_id=None if undo else task_id,
            attempt=None if undo else int(row["attempt"]),
        )
        root = Path(workspace.canonical_path)
        files = prepared if prepared is not None else decode_files(row["proposal_json"])
        if not files:
            raise ValueError("This task has no file changes.")
        if undo:
            inverses = []
            for f in files:
                if (
                    f.base.kind in {ObjectKind.REGULAR, ObjectKind.SYMLINK}
                    and f.original is None
                ):
                    raise ValueError(
                        "Original contents unavailable; undo is not supported fo"
                        "r this task."
                    )
                if f.mode == DIRECTORY_MODE:
                    # Files are reverted first. Keep created directories when
                    # another writer has added entries beneath them.
                    children = list((root / f.relative_path).rglob("*"))
                    owned = {x.relative_path for x in files}
                    if any(
                        p.relative_to(root).as_posix() not in owned for p in children
                    ):
                        continue
                inverses.append(
                    BatchFile(
                        f.relative_path,
                        result_identity(f),
                        f.original if not f.base.absent else None,
                        f.base.mode,
                        f.content if f.mode != DIRECTORY_MODE else None,
                        f.base.permissions,
                        f.binary,
                    )
                )
            files = tuple(inverses)
        elif not allow_unverified:
            from llm_cli.execution.checks import verification_current

            if not verification_current(
                self.store, row, root, files, runtime_root=runtime_root
            ):
                raise ValueError(
                    "Required checks became stale during apply; retry apply, "
                    "or use --allow-unverified."
                )
        for f in files:
            identity, _ = read_identified_path(candidate_target(root, f.relative_path))
            if identity != f.base:
                raise ValueError(f"Conflict at {f.relative_path}; no files changed.")
        from llm_cli.workspace.gitstate import preflight, reconcile

        # Refuse before creating the action task/session, so the held proposal
        # remains directly retryable after the user's Git operation finishes.
        reconcile(self.store, root, candidate_dir=self.publisher.candidate_dir)
        checkout = self.store.get_checkout(session.checkout_id)
        assert checkout is not None
        preflight(
            root,
            tuple(f.relative_path for f in files),
            refuse_staged_paths=self.publisher.refuse_staged_paths,
            case_insensitive_filesystem=checkout.path_case_insensitive,
        )
        original_execution = self.store.get_execution(task.task_id, int(row["attempt"]))
        original_claim = self.store.get_claim(task.current_claim_id or "")
        scopes = (
            original_execution.scopes
            if isinstance(original_execution, WorkspaceExecutionRecord)
            else original_claim.scopes
            if original_claim
            else None
        )
        if scopes is None:
            with self.store.connection() as con:
                binding = con.execute(
                    "SELECT scopes_json FROM switch_proposal_bindings "
                    "WHERE task_id=? AND attempt=?",
                    (task_id, int(row["attempt"])),
                ).fetchone()
            scopes = tuple(json.loads(binding[0])) if binding else None
        if scopes is None:
            raise ValueError("The proposal's original path authority is unavailable.")
        authority = self.store.authority
        regime = authority.regime(root) if authority else {"regime": "legacy"}
        if regime["regime"] not in {"legacy", "workspace"}:
            raise ValueError("Checkout cutover must finish before applying a proposal.")
        # Manual actions remain usable after the conversation was closed and
        # do not replace another live conversation's provider checkpoint.
        operation_session = new_id("operation")
        session, _, _ = self.store.open_session(
            session_id=operation_session,
            checkout_id=session.checkout_id,
            resume_token_hash=hashlib.sha256(new_id("secret").encode()).hexdigest(),
            provider="workflow",
            model="local",
            workspace_id=workspace.workspace_id,
        )
        with self.store.connection() as con:
            con.execute(
                "UPDATE sessions SET state='active' WHERE session_id=?",
                (session.session_id,),
            )
        child = self.store.create_task(
            repository_id=task.repository_id,
            session_id=session.session_id,
            title=("Undo " if undo else "Apply ") + task_id,
        )
        execution: Execution
        if regime["regime"] == "workspace":
            assert authority is not None
            authority.assert_regime(
                root, allowed=("workspace",), generation=int(regime["generation"])
            )
            execution = self.store.create_workspace_execution(
                task_id=child.task_id,
                attempt=child.attempt,
                session_id=session.session_id,
                workspace_id=workspace.workspace_id,
                scopes=scopes,
                physical_key=str(regime["physical_key"]),
                regime_generation=int(regime["generation"]),
                driver="workflow",
                worktree_path=str(root),
                base_oid=f"workspace:{workspace.workspace_epoch}:{workspace.workspace_revision}",
                boot_id="workflow",
            )
            self.store.update_execution(execution.execution_id, state="validated")
            claim = None
        else:
            if authority is not None:
                authority.assert_regime(root, allowed=("legacy",))
            claim = self.coordinator.request_claim(
                child.task_id, scopes, scheduling_mode="exclusive"
            )
            if claim.state.value != "active_work":
                self.coordinator.release_claim(
                    claim_id=claim.claim_id,
                    reason="manual action could not acquire authority",
                )
                raise ValueError(
                    "Another exclusive task blocks this action; "
                    "retry after it finishes."
                )
            execution = self.store.create_execution(
                task_id=child.task_id,
                attempt=child.attempt,
                claim_id=claim.claim_id,
                driver="workflow",
                worktree_path=str(root),
                base_oid=f"workspace:{workspace.workspace_epoch}:{workspace.workspace_revision}",
                boot_id="workflow",
                workspace_id=workspace.workspace_id,
            )
        self.ensure(child.task_id)
        self.retain(child.task_id, child.attempt, execution.execution_id, files)
        self.update(
            task_id,
            row["attempt"],
            **{action_key: child.task_id},
            # Undo must revert only this applied change, retaining the newer
            # checkout bytes that the held proposal was merged onto.
            **({"proposal_json": encode_files(files)} if not undo else {}),
        )
        if isinstance(execution, WorkspaceExecutionRecord):
            self.coordinator.reserve_workspace_publication(
                execution.execution_id,
                regime_generation=execution.regime_generation,
                changed_paths=(f.relative_path for f in files),
            )
        else:
            assert claim is not None and claim.fencing_token is not None
            self.coordinator.reserve_shared_publication(
                execution.execution_id,
                fencing_token=claim.fencing_token,
                changed_paths=(f.relative_path for f in files),
            )
        result = self.publisher.publish(
            execution.execution_id,
            session.session_id,
            files,
            scopes,
            case_insensitive_filesystem=False,
        )
        if result.state != "operator_attention":
            settle = (
                self.coordinator.settle_workspace_execution
                if isinstance(execution, WorkspaceExecutionRecord)
                else self.coordinator.settle_shared_execution
            )
            settle(execution.execution_id, outcome=result.state)
        if result.state == "published":
            with self.store.connection() as con:
                con.execute(
                    (
                        "UPDATE tasks SET state='completed' WHERE task_id=? AND "
                        "attempt=?"
                    ),
                    (task_id, row["attempt"]),
                )
            self.store.close_session(session.session_id, reason="workflow_complete")
            self.update(
                child.task_id,
                child.attempt,
                expires_at=self.store._now(None)
                + self.coordinator.terminal_retention_ms,
                status="published",
                publication_id=execution.execution_id,
            )
            self.update(
                task_id,
                row["attempt"],
                status="undone" if undo else "published",
                **({"publication_id": execution.execution_id} if not undo else {}),
            )
            if isolated_workspace_id is not None:
                follow_up.append((task_id, isolated_workspace_id))
        self.store.record_task_event(
            claim_id=None,
            task_id=child.task_id,
            event_type="workflow.undo" if undo else "workflow.apply",
            payload={
                "source_task_id": task_id,
                "allow_unverified": allow_unverified,
                "verification": "unverified" if undo or allow_unverified else "passed",
            },
        )
        return {
            "state": result.state,
            "task_id": child.task_id,
            "source_task_id": task_id,
        }
