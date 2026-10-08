"""Durable proposals, review actions, cancellation and task-local diffs.

Only the coordinator and existing batch journal apply checkout changes. A held
proposal carries no live claim; applying it is a new fenced task.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
from pathlib import Path
from typing import Any

from llm_cli.agent.modes import publication_mode, validate_agent_mode
from llm_cli.coordination.coordinator import RepositoryCoordinator
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
from llm_cli.workspace.identity import DIRECTORY_MODE, ObjectKind, read_identified_path


def encode_files(files: tuple[BatchFile, ...]) -> str:
    return json.dumps(
        [
            {
                "path": f.relative_path,
                "base": _identity_json(f.base),
                "mode": f.mode,
                "permissions": f.permissions,
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
                                "SELECT b.state FROM task_executions e "
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
            if launch else default_mode
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
                    else mode[0] if mode else publication_mode(agent_mode),
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
        self.update(task_id, attempt, status=status)
        self.coordinator.settle_shared_execution(
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
        if claim and claim.state.value in {"publishing", "active_integration"}:
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
                        AND state IN ('publishing','active_integration'))""",
                (task_id, task.attempt, task_id, task.attempt),
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
            claim = self.store.get_claim(execution.claim_id) if execution else None
            if saved and execution and claim:
                broker = SharedToolBroker(Path(execution.worktree_path), claim.scopes)
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

    def apply(
        self, task_id: str, *, undo: bool = False, allow_unverified: bool = False
    ) -> dict[str, Any]:
        with self.publisher.lock:
            self.reconcile()
            return self._apply(task_id, undo=undo, allow_unverified=allow_unverified)

    def _apply(
        self, task_id: str, *, undo: bool, allow_unverified: bool
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
                self.update(
                    task_id, row["attempt"], status="undone" if undo else "published"
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
        workspace = self.store.get_workspace(session.workspace_id or "")
        assert workspace is not None
        root = Path(workspace.canonical_path)
        files = decode_files(row["proposal_json"])
        if not files:
            raise ValueError("This task has no file changes.")
        if undo:
            inverses = []
            for f in files:
                if f.base.kind is ObjectKind.REGULAR and f.original is None:
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
                    )
                )
            files = tuple(inverses)
        elif not allow_unverified:
            from llm_cli.execution.checks import verification_current

            if not verification_current(self.store, row, root, files):
                raise ValueError(
                    "Required checks are missing, failed, or stale. Ask for th"
                    "e change again in the conversation to rerun them, or use "
                    "--allow-unverified."
                )
        for f in files:
            identity, _ = read_identified_path(candidate_target(root, f.relative_path))
            if identity != f.base:
                raise ValueError(f"Conflict at {f.relative_path}; no files changed.")
        original_claim = self.store.get_claim(task.current_claim_id or "")
        assert original_claim is not None
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
        claim = self.coordinator.request_claim(
            child.task_id,
            original_claim.scopes,
            scheduling_mode="exclusive",
        )
        if claim.state.value != "active_work":
            self.coordinator.release_claim(
                claim_id=claim.claim_id,
                reason="manual action could not acquire authority",
            )
            raise ValueError(
                "Another exclusive task blocks this action; retry after it finishes."
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
        self.update(task_id, row["attempt"], **{action_key: child.task_id})
        assert claim.fencing_token is not None
        self.coordinator.reserve_shared_publication(
            execution.execution_id,
            fencing_token=claim.fencing_token,
            changed_paths=(f.relative_path for f in files),
        )
        result = self.publisher.publish(
            execution.execution_id,
            session.session_id,
            files,
            claim.scopes,
            case_insensitive_filesystem=False,
        )
        if result.state != "operator_attention":
            self.coordinator.settle_shared_execution(
                execution.execution_id, outcome=result.state
            )
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
                task_id, row["attempt"], status="undone" if undo else "published"
            )
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
