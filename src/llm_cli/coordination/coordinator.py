"""Transactional claim scheduling for exclusive and optimistic repository tasks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from llm_cli.coordination.models import (
    ClaimAuthorityError,
    ClaimConflict,
    ClaimNotFound,
    ClaimRecord,
    ClaimState,
    PublicationError,
    PublicationIntentRecord,
    ReconcileResult,
    ReleaseResult,
    ScopeAuthorityError,
)
from llm_cli.coordination.scopes import (
    normalize_changed_path,
    normalize_scopes,
    scope_sets_overlap,
    uncovered_paths,
)
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.storage.connection import immediate_transaction
from llm_cli.storage.control import ControlStore, new_identifier
from llm_cli.workspace.regime import assert_legacy_claim, legacy_activation_allowed

_TERMINAL_STATES = {"released", "expired", "cancelled"}
# ``operator_attention`` records that an outcome could not be established, not
# that it was decided against.  Later evidence -- a repository that is readable
# again, a reference an operator restored -- can still settle it, so it stays
# open to a decision while ``confirmed`` and ``failed_safe`` do not.
_UNDECIDED_INTENT_STATES = {"prepared", "side_effect_unknown", "operator_attention"}
_LIVE_ACTIVE_STATES = {"active_work", "publishing", "active_integration"}
# Wall-clock jumps shorter than this are clock noise, not a suspension.
_SUSPENSION_TOLERANCE_MS = 2_000


class RepositoryCoordinator:
    """Serialize claim decisions at one repository-head row.

    Every read/decide/write sequence runs under ``BEGIN IMMEDIATE``.  Queued
    scopes reserve against later overlapping exclusive work. Optimistic tasks
    prepare private edits concurrently and resolve conflicts at publication;
    they still respect every overlapping exclusive reservation in FIFO order.
    """

    def __init__(
        self,
        store: ControlStore,
        *,
        launch_lease_ms: int = 600_000,
        work_lease_ms: int = 90_000,
        terminal_retention_ms: int = 30 * 24 * 60 * 60 * 1_000,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(launch_lease_ms, work_lease_ms, terminal_retention_ms) <= 0:
            raise ValueError("lease and retention durations must be positive")
        self.store = store
        self.launch_lease_ms = launch_lease_ms
        self.work_lease_ms = work_lease_ms
        self.terminal_retention_ms = terminal_retention_ms
        # The monotonic clock stops while the machine sleeps; the wall clock
        # does not. Their difference is time this process spent suspended.
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._suspension_lock = threading.Lock()
        self._clock_reference = (wall_clock(), monotonic_clock())
        self._unapplied_suspension_ms = 0

    def request_claim(
        self,
        task_id: str,
        scopes: Iterable[str],
        *,
        now: int | None = None,
        source: str = "user",
        scheduling_mode: str = "exclusive",
        optimistic_driver: str | None = None,
        checkout_id: str | None = None,
    ) -> ClaimRecord:
        """Reserve scopes, optionally allowing concurrent private preparation.

        ``optimistic_driver`` is the trusted execution registry's attestation
        that the saved launch can prepare scoped, resumable workspace edits. It
        must match the immutable launch; it is never a user-supplied RPC flag.
        The workspace binding and scheduling mode are immutable per attempt.
        """

        if scheduling_mode not in {"exclusive", "optimistic"}:
            raise ValueError("claim scheduling mode must be exclusive or optimistic")
        timestamp = self.store._now(now)
        with self._transaction() as connection:
            task = self._task_row(connection, task_id)
            repo_key = str(task["repo_key"])
            binding = self._legacy_claim_binding(
                connection, task, checkout_id=checkout_id
            )
            assert_legacy_claim(
                connection,
                repo_key,
                physical_key=binding["physical_key"] if binding else None,
            )
            if not self._legacy_physical_mappings_available(
                connection, repo_key, binding=binding
            ):
                raise LlmCoordError(
                    ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED,
                    "legacy claims are unavailable for a converted or "
                    "unverified checkout",
                    {"required_contract": "legacy"},
                )
            fold_case = self._path_case_insensitive(connection, repo_key)
            canonical = normalize_scopes(scopes, case_insensitive_filesystem=fold_case)
            effective_now = self._effective_now(connection, repo_key, timestamp)

            existing = connection.execute(
                """
                SELECT * FROM claims
                WHERE repo_key = ? AND task_id = ? AND task_attempt = ?
                """,
                (repo_key, task_id, int(task["attempt"])),
            ).fetchone()
            if existing is not None:
                if str(existing["state"]) in _TERMINAL_STATES:
                    raise ClaimConflict(
                        "this task attempt is already finished; start a new "
                        "attempt with 'llm-coord task retry TASK_ID' before "
                        "requesting another claim"
                    )
                if (
                    str(existing["scheduling_mode"]) != scheduling_mode
                    or self._claim_scopes(connection, existing) != canonical
                ):
                    raise ClaimConflict(
                        "task attempt already has different claim scopes or "
                        "scheduling mode"
                    )
                return self.store.claim_from_row(connection, existing)

            workspace_id = (
                self._optimistic_workspace(connection, task, optimistic_driver)
                if scheduling_mode == "optimistic"
                else None
            )
            self._expire_stale(connection, repo_key, effective_now)
            self._activate_waiters(connection, repo_key, effective_now)

            sequence = self._next_queue_sequence(connection, repo_key, effective_now)
            contenders = connection.execute(
                """
                SELECT * FROM claims
                WHERE repo_key = ? AND state IN (
                    'queued', 'active_work', 'publishing', 'active_integration'
                )
                ORDER BY queue_sequence
                """,
                (repo_key,),
            ).fetchall()
            blockers = [
                str(row["claim_id"])
                for row in contenders
                if self._reservations_conflict(
                    canonical,
                    scheduling_mode,
                    self._claim_scopes(connection, row),
                    str(row["scheduling_mode"]),
                    case_insensitive_filesystem=fold_case,
                )
                and self._legacy_claim_can_activate(connection, repo_key, row)
            ]
            claim_id = new_identifier("claim")
            if blockers:
                state = ClaimState.QUEUED
                fence = None
                lease_expires = None
                activated_at = None
                task_state = "waiting_for_repository"
            else:
                state = ClaimState.ACTIVE_WORK
                fence = self._next_fence(connection, repo_key, effective_now)
                lease_expires = effective_now + self.launch_lease_ms
                activated_at = effective_now
                task_state = "preparing"

            connection.execute(
                """
                INSERT INTO claims(
                    claim_id, task_id, task_attempt, repo_key, state,
                    queue_sequence, fencing_token, lease_expires_at,
                    created_at, activated_at, updated_at, scheduling_mode,
                    workspace_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    task_id,
                    int(task["attempt"]),
                    repo_key,
                    state.value,
                    sequence,
                    fence,
                    lease_expires,
                    effective_now,
                    activated_at,
                    effective_now,
                    scheduling_mode,
                    workspace_id,
                ),
            )
            if binding is not None:
                connection.execute(
                    "INSERT INTO legacy_claim_bindings(claim_id,checkout_id,"
                    "physical_key,canonical_path,target_ref,integration_adapter) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        claim_id,
                        binding["checkout_id"],
                        binding["physical_key"],
                        binding["canonical_path"],
                        binding["target_ref"],
                        binding["integration_adapter"],
                    ),
                )
            self._insert_scopes(connection, claim_id, canonical, source)
            self._replace_blockers(
                connection, claim_id, blockers, repo_key, effective_now
            )
            connection.execute(
                """
                UPDATE tasks SET state = ?, coordination_state = ?,
                    current_claim_id = ?, current_fencing_token = ?,
                    queued_at = COALESCE(queued_at, ?),
                    started_at = CASE WHEN ? IS NOT NULL
                        THEN COALESCE(started_at, ?) ELSE started_at END,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    task_state,
                    state.value,
                    claim_id,
                    fence,
                    effective_now,
                    fence,
                    effective_now,
                    effective_now,
                    task_id,
                ),
            )
            event_type = "claim.queued" if blockers else "claim.granted"
            self._event_and_outbox(
                connection,
                task_id=task_id,
                claim_id=claim_id,
                event_type=event_type,
                payload={
                    "queue_sequence": sequence,
                    "fencing_token": fence,
                    "blocking_claim_ids": blockers,
                    "scheduling_mode": scheduling_mode,
                    "workspace_id": workspace_id,
                },
                now=effective_now,
            )
            row = connection.execute(
                "SELECT * FROM claims WHERE claim_id = ?", (claim_id,)
            ).fetchone()
            assert row is not None
            return self.store.claim_from_row(connection, row)

    def renew_claim(
        self,
        *,
        task_id: str,
        claim_id: str,
        fencing_token: int,
        attempt: int,
        now: int | None = None,
    ) -> ClaimRecord:
        timestamp = self.store._now(now)
        with self._transaction() as connection:
            row = self._claim_row(connection, claim_id)
            repo_key = str(row["repo_key"])
            effective_now = self._effective_now(connection, repo_key, timestamp)
            updated = connection.execute(
                """
                UPDATE claims SET lease_expires_at = ?, updated_at = ?
                WHERE claim_id = ? AND task_id = ? AND task_attempt = ?
                    AND repo_key = ? AND state = 'active_work'
                    AND fencing_token = ? AND lease_expires_at > ?
                """,
                (
                    effective_now + self.work_lease_ms,
                    effective_now,
                    claim_id,
                    task_id,
                    attempt,
                    repo_key,
                    fencing_token,
                    effective_now,
                ),
            )
            if updated.rowcount != 1:
                raise ClaimAuthorityError("claim lease or fencing authority is stale")
            fresh = self._claim_row(connection, claim_id)
            return self.store.claim_from_row(connection, fresh)

    def assert_current_claim(
        self,
        *,
        task_id: str,
        claim_id: str,
        fencing_token: int,
        attempt: int,
        now: int | None = None,
    ) -> ClaimRecord:
        timestamp = self.store._now(now)
        with self._transaction() as connection:
            row = self._claim_row(connection, claim_id)
            repo_key = str(row["repo_key"])
            effective_now = self._effective_now(connection, repo_key, timestamp)
            if not self._has_work_authority(
                row,
                task_id=task_id,
                claim_id=claim_id,
                fencing_token=fencing_token,
                attempt=attempt,
                now=effective_now,
            ):
                raise ClaimAuthorityError("claim lease or fencing authority is stale")
            return self.store.claim_from_row(connection, row)

    def release_claim(
        self,
        claim_id: str,
        *,
        reason: str,
        expected_fencing_token: int | None = None,
        task_state: str = "cancelled",
        failure_code: str | None = None,
        now: int | None = None,
    ) -> ReleaseResult:
        """Release a live claim and record why its task stopped.

        ``task_state`` distinguishes an operator cancelling work from a worker
        that failed: both release the reservation, but only one of them is a
        defect an operator needs to see.  ``failure_code`` is preserved so the
        durable record explains the outcome rather than merely ending it.
        """

        if not reason.strip():
            raise ValueError("release reason must not be empty")
        if task_state not in {"cancelled", "failed"}:
            raise ValueError("release task state must be cancelled or failed")
        timestamp = self.store._now(now)
        with self._transaction() as connection:
            row = self._claim_row(connection, claim_id)
            repo_key = str(row["repo_key"])
            effective_now = self._effective_now(connection, repo_key, timestamp)
            state = str(row["state"])
            if state in _TERMINAL_STATES:
                return ReleaseResult(
                    released=False, claim=self.store.claim_from_row(connection, row)
                )
            if state in {"publishing", "active_integration"}:
                raise ClaimConflict(
                    "publication/integration reservations require verified "
                    "discard or integration"
                )
            if (
                expected_fencing_token is not None
                and row["fencing_token"] != expected_fencing_token
            ):
                raise ClaimAuthorityError("fencing token does not match")
            terminal_state = "cancelled" if state == "queued" else "released"
            connection.execute(
                """
                UPDATE claims SET state = ?, lease_expires_at = NULL,
                    release_reason = ?, terminal_expires_at = ?, released_at = ?,
                    updated_at = ? WHERE claim_id = ?
                """,
                (
                    terminal_state,
                    reason[:200],
                    effective_now + self.terminal_retention_ms,
                    effective_now,
                    effective_now,
                    claim_id,
                ),
            )
            connection.execute(
                """
                UPDATE tasks SET state = ?, coordination_state = ?,
                    current_fencing_token = NULL,
                    failure_code = COALESCE(?, failure_code),
                    finished_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    task_state,
                    terminal_state,
                    failure_code,
                    effective_now,
                    effective_now,
                    row["task_id"],
                ),
            )
            self._event_and_outbox(
                connection,
                task_id=str(row["task_id"]),
                claim_id=claim_id,
                event_type=f"claim.{terminal_state}",
                payload={
                    "reason": reason[:200],
                    "task_state": task_state,
                    "failure_code": failure_code,
                },
                now=effective_now,
            )
            activated = self._activate_waiters(connection, repo_key, effective_now)
            fresh = self._claim_row(connection, claim_id)
            return ReleaseResult(
                released=True,
                claim=self.store.claim_from_row(connection, fresh),
                activated=activated,
            )

    def reconcile_expired(
        self, repo_key: str | None = None, *, now: int | None = None
    ) -> ReconcileResult:
        timestamp = self.store._now(now)
        if repo_key is None:
            with self.store.connection() as connection:
                keys = tuple(
                    str(row["repo_key"])
                    for row in connection.execute(
                        "SELECT repo_key FROM repository_heads ORDER BY repo_key"
                    )
                )
        else:
            keys = (repo_key,)
        expired_claims: list[str] = []
        expired_tasks: list[str] = []
        activated: list[ClaimRecord] = []
        for key in keys:
            with self._transaction() as connection:
                effective_now = self._effective_now(connection, key, timestamp)
                expired = self._expire_stale(connection, key, effective_now)
                expired_claims.extend(item[0] for item in expired)
                expired_tasks.extend(item[1] for item in expired)
                activated.extend(self._activate_waiters(connection, key, effective_now))
        return ReconcileResult(
            expired_claim_ids=tuple(expired_claims),
            expired_task_ids=tuple(expired_tasks),
            activated=tuple(activated),
        )

    def begin_publication(
        self,
        *,
        task_id: str,
        claim_id: str,
        fencing_token: int,
        attempt: int,
        changed_paths: Iterable[str],
        patch_hash: str,
        result_tree_id: str,
        result_commit_id: str | None = None,
        expected_old_ref: str | None = None,
        now: int | None = None,
    ) -> PublicationIntentRecord:
        paths = tuple(sorted({normalize_changed_path(path) for path in changed_paths}))
        if not paths:
            raise ScopeAuthorityError("trusted changed paths must not be empty")
        if len(patch_hash) != 64 or any(
            char not in "0123456789abcdef" for char in patch_hash
        ):
            raise PublicationError("patch hash must be a lowercase SHA-256 digest")
        if not result_tree_id:
            raise PublicationError("result tree ID must not be empty")
        timestamp = self.store._now(now)
        with self._transaction() as connection:
            stopped = connection.execute(
                (
                    "SELECT stop_requested FROM task_workflows WHERE task_id"
                    "=? AND attempt=?"
                ),
                (task_id, attempt),
            ).fetchone()
            if stopped and stopped[0]:
                raise ClaimAuthorityError("task was cancelled before publication")
            row = self._claim_row(connection, claim_id)
            if row["scheduling_mode"] == "optimistic":
                raise PublicationError(
                    "optimistic claims require shared workspace batch publication"
                )
            repo_key = str(row["repo_key"])
            effective_now = self._effective_now(connection, repo_key, timestamp)
            identity_material = "\0".join(
                (
                    repo_key,
                    task_id,
                    str(attempt),
                    claim_id,
                    str(fencing_token),
                    patch_hash,
                    result_tree_id,
                    expected_old_ref or "",
                    *paths,
                )
            )
            idempotency_key = hashlib.sha256(
                identity_material.encode("utf-8")
            ).hexdigest()
            existing = connection.execute(
                "SELECT * FROM publication_intents WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return self.store.publication_from_row(connection, existing)
            if not self._has_work_authority(
                row,
                task_id=task_id,
                claim_id=claim_id,
                fencing_token=fencing_token,
                attempt=attempt,
                now=effective_now,
            ):
                raise ClaimAuthorityError("claim lease or fencing authority is stale")
            scopes = self._claim_scopes(connection, row)
            missing = uncovered_paths(
                scopes,
                paths,
                case_insensitive_filesystem=self._path_case_insensitive(
                    connection, repo_key
                ),
            )
            if missing:
                raise ScopeAuthorityError(
                    f"trusted changed paths are outside the claim: {', '.join(missing)}"
                )
            repository = connection.execute(
                "SELECT * FROM repositories WHERE repo_key = ?", (repo_key,)
            ).fetchone()
            assert repository is not None
            intent_id = new_identifier("intent")
            task_ref = f"refs/llm-coord/tasks/{task_id}"
            connection.execute(
                """
                INSERT INTO publication_intents(
                    intent_id, idempotency_key, claim_id, task_id, task_attempt,
                    repo_key, fencing_token, patch_hash, result_tree_id,
                    result_commit_id, expected_old_ref, target_ref, task_ref,
                    started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    idempotency_key,
                    claim_id,
                    task_id,
                    attempt,
                    repo_key,
                    fencing_token,
                    patch_hash,
                    result_tree_id,
                    result_commit_id,
                    expected_old_ref,
                    repository["target_ref"],
                    task_ref,
                    effective_now,
                    effective_now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO publication_paths(intent_id, ordinal, path)
                VALUES (?, ?, ?)
                """,
                ((intent_id, index, path) for index, path in enumerate(paths)),
            )
            updated = connection.execute(
                """
                UPDATE claims SET state = 'publishing', lease_expires_at = NULL,
                    publication_patch_hash = ?, publication_tree_id = ?,
                    result_revision = ?, publication_started_at = ?, updated_at = ?
                WHERE claim_id = ? AND state = 'active_work'
                    AND fencing_token = ? AND lease_expires_at > ?
                """,
                (
                    patch_hash,
                    result_tree_id,
                    result_commit_id,
                    effective_now,
                    effective_now,
                    claim_id,
                    fencing_token,
                    effective_now,
                ),
            )
            if updated.rowcount != 1:
                raise ClaimAuthorityError("claim became stale before publication")
            connection.execute(
                """
                UPDATE tasks SET state = 'integrating',
                    coordination_state = 'publishing', result_revision = ?,
                    updated_at = ? WHERE task_id = ?
                """,
                (result_commit_id, effective_now, task_id),
            )
            self._event_and_outbox(
                connection,
                task_id=task_id,
                claim_id=claim_id,
                event_type="publication.prepared",
                payload={"intent_id": intent_id, "path_count": len(paths)},
                now=effective_now,
            )
            intent = connection.execute(
                "SELECT * FROM publication_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            assert intent is not None
            return self.store.publication_from_row(connection, intent)

    def confirm_publication(
        self,
        intent_id: str,
        *,
        result_commit_id: str,
        task_ref: str,
        now: int | None = None,
    ) -> PublicationIntentRecord:
        if not result_commit_id or not task_ref.startswith("refs/llm-coord/tasks/"):
            raise PublicationError("confirmed publication identity is invalid")
        timestamp = self.store._now(now)
        with self._transaction() as connection:
            intent = connection.execute(
                "SELECT * FROM publication_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent is None:
                raise PublicationError("publication intent does not exist")
            if intent["operation_state"] == "confirmed":
                if (
                    intent["result_commit_id"] != result_commit_id
                    or intent["task_ref"] != task_ref
                ):
                    raise PublicationError(
                        "publication was confirmed with another result"
                    )
                return self.store.publication_from_row(connection, intent)
            if intent["operation_state"] not in _UNDECIDED_INTENT_STATES:
                raise PublicationError("publication intent is not confirmable")
            repo_key = str(intent["repo_key"])
            effective_now = self._effective_now(connection, repo_key, timestamp)
            claim = self._claim_row(connection, str(intent["claim_id"]))
            if claim["state"] != "publishing":
                raise PublicationError("claim is not in publishing state")
            connection.execute(
                """
                UPDATE publication_intents SET operation_state = 'confirmed',
                    result_commit_id = ?, task_ref = ?, confirmed_at = ?, updated_at = ?
                WHERE intent_id = ?
                """,
                (result_commit_id, task_ref, effective_now, effective_now, intent_id),
            )
            connection.execute(
                """
                UPDATE claims SET state = 'active_integration',
                    result_revision = ?, integration_identity = ?,
                    integration_started_at = ?, updated_at = ?
                WHERE claim_id = ? AND state = 'publishing'
                """,
                (
                    result_commit_id,
                    task_ref,
                    effective_now,
                    effective_now,
                    intent["claim_id"],
                ),
            )
            connection.execute(
                """
                UPDATE tasks SET state = 'ready_for_integration',
                    coordination_state = 'active_integration', result_revision = ?,
                    updated_at = ? WHERE task_id = ?
                """,
                (result_commit_id, effective_now, intent["task_id"]),
            )
            self._event_and_outbox(
                connection,
                task_id=str(intent["task_id"]),
                claim_id=str(intent["claim_id"]),
                event_type="publication.confirmed",
                payload={"intent_id": intent_id},
                now=effective_now,
            )
            fresh = connection.execute(
                "SELECT * FROM publication_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            assert fresh is not None
            return self.store.publication_from_row(connection, fresh)

    def fail_publication_safe(
        self,
        intent_id: str,
        *,
        reason: str,
        failure_code: str,
        now: int | None = None,
    ) -> ReleaseResult:
        """Close an intent whose Git side effect provably never happened.

        This is the only path that releases a publishing reservation without an
        integration decision, so the caller must have proved absence -- for the
        branch strategy, by reading the task ref and finding exactly the commit
        the intent expected to replace.  A merely unknown outcome belongs in
        :meth:`flag_publication_for_operator` instead.
        """

        if not reason.strip():
            raise ValueError("publication failure reason must not be empty")
        timestamp = self.store._now(now)
        with self._transaction() as connection:
            intent = connection.execute(
                "SELECT * FROM publication_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent is None:
                raise PublicationError("publication intent does not exist")
            claim_id = str(intent["claim_id"])
            if intent["operation_state"] == "failed_safe":
                claim_row = self._claim_row(connection, claim_id)
                return ReleaseResult(
                    released=False,
                    claim=self.store.claim_from_row(connection, claim_row),
                )
            if intent["operation_state"] not in _UNDECIDED_INTENT_STATES:
                raise PublicationError(
                    "publication intent already has a recorded outcome"
                )
            repo_key = str(intent["repo_key"])
            effective_now = self._effective_now(connection, repo_key, timestamp)
            connection.execute(
                """
                UPDATE publication_intents SET operation_state = 'failed_safe',
                    result_hash = NULL, updated_at = ? WHERE intent_id = ?
                """,
                (effective_now, intent_id),
            )
            released = connection.execute(
                """
                UPDATE claims SET state = 'released', release_reason = ?,
                    terminal_expires_at = ?, released_at = ?, updated_at = ?
                WHERE claim_id = ? AND state = 'publishing'
                """,
                (
                    reason[:200],
                    effective_now + self.terminal_retention_ms,
                    effective_now,
                    effective_now,
                    claim_id,
                ),
            )
            connection.execute(
                """
                UPDATE tasks SET state = 'failed', coordination_state = 'released',
                    current_fencing_token = NULL,
                    failure_code = COALESCE(?, failure_code),
                    finished_at = ?, updated_at = ? WHERE task_id = ?
                """,
                (failure_code, effective_now, effective_now, intent["task_id"]),
            )
            self._event_and_outbox(
                connection,
                task_id=str(intent["task_id"]),
                claim_id=claim_id,
                event_type="publication.failed_safe",
                payload={
                    "intent_id": intent_id,
                    "reason": reason[:200],
                    "failure_code": failure_code,
                },
                now=effective_now,
            )
            activated = (
                self._activate_waiters(connection, repo_key, effective_now)
                if released.rowcount == 1
                else ()
            )
            fresh = self._claim_row(connection, claim_id)
            return ReleaseResult(
                released=released.rowcount == 1,
                claim=self.store.claim_from_row(connection, fresh),
                activated=activated,
            )

    def flag_publication_for_operator(
        self,
        intent_id: str,
        *,
        reason: str,
        details: Mapping[str, object] | None = None,
        now: int | None = None,
    ) -> PublicationIntentRecord:
        """Record that a publication outcome is unknown and must stay blocking.

        The reservation is deliberately left in ``publishing``: it holds no
        lease, so expiry reconciliation cannot retire it, and no waiter is
        activated.  Overlapping work stays queued until an operator establishes
        what the repository actually contains.
        """

        if not reason.strip():
            raise ValueError("operator-attention reason must not be empty")
        timestamp = self.store._now(now)
        with self._transaction() as connection:
            intent = connection.execute(
                "SELECT * FROM publication_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent is None:
                raise PublicationError("publication intent does not exist")
            if intent["operation_state"] == "operator_attention":
                return self.store.publication_from_row(connection, intent)
            if intent["operation_state"] not in _UNDECIDED_INTENT_STATES:
                raise PublicationError(
                    "publication intent already has a recorded outcome"
                )
            repo_key = str(intent["repo_key"])
            effective_now = self._effective_now(connection, repo_key, timestamp)
            connection.execute(
                """
                UPDATE publication_intents
                SET operation_state = 'operator_attention', updated_at = ?
                WHERE intent_id = ?
                """,
                (effective_now, intent_id),
            )
            connection.execute(
                """
                UPDATE tasks SET state = 'operator_attention', updated_at = ?
                WHERE task_id = ?
                """,
                (effective_now, intent["task_id"]),
            )
            payload: dict[str, object] = {
                "intent_id": intent_id,
                "reason": reason[:200],
            }
            payload.update(details or {})
            self._event_and_outbox(
                connection,
                task_id=str(intent["task_id"]),
                claim_id=str(intent["claim_id"]),
                event_type="publication.operator_attention",
                payload=payload,
                now=effective_now,
            )
            fresh = connection.execute(
                "SELECT * FROM publication_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            assert fresh is not None
            return self.store.publication_from_row(connection, fresh)

    def reserve_shared_publication(
        self,
        execution_id: str,
        *,
        fencing_token: int,
        changed_paths: Iterable[str],
    ) -> None:
        """Fence a shared task's final batch before its filesystem journal.

        The shared batch journal is the only gateway to checkout side effects.
        A crash between this reservation and that journal is therefore a
        provably unpublished attempt. Publication reservations have no lease.
        """

        paths = tuple(normalize_changed_path(path) for path in changed_paths)
        with self._transaction() as connection:
            execution = connection.execute(
                "SELECT * FROM task_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if execution is None or execution["workspace_id"] is None:
                raise ClaimConflict("execution is not a shared workspace task")
            stopped = connection.execute(
                (
                    "SELECT stop_requested FROM task_workflows WHERE task_id"
                    "=? AND attempt=?"
                ),
                (execution["task_id"], execution["task_attempt"]),
            ).fetchone()
            if stopped and stopped[0]:
                raise ClaimAuthorityError("task was cancelled before publication")
            row = self._claim_row(connection, str(execution["claim_id"]))
            if (
                row["scheduling_mode"] == "optimistic"
                and row["workspace_id"] != execution["workspace_id"]
            ):
                raise ClaimAuthorityError(
                    "execution workspace does not match its optimistic claim"
                )
            now = self._effective_now(
                connection, str(row["repo_key"]), self.store._now(None)
            )
            if not self._has_work_authority(
                row,
                task_id=str(execution["task_id"]),
                claim_id=str(execution["claim_id"]),
                fencing_token=fencing_token,
                attempt=int(execution["task_attempt"]),
                now=now,
            ):
                raise ClaimAuthorityError("claim lease or fencing authority is stale")
            if uncovered_paths(
                self._claim_scopes(connection, row),
                paths,
                case_insensitive_filesystem=self._path_case_insensitive(
                    connection, str(row["repo_key"])
                ),
            ):
                raise ScopeAuthorityError("shared batch contains out-of-scope paths")
            connection.execute(
                """UPDATE claims SET state = 'publishing', lease_expires_at = NULL,
                    publication_started_at = ?, updated_at = ? WHERE claim_id = ?""",
                (now, now, row["claim_id"]),
            )
            connection.execute(
                """UPDATE tasks SET coordination_state = 'publishing', updated_at = ?
                    WHERE task_id = ?""",
                (now, execution["task_id"]),
            )
            connection.execute(
                """UPDATE task_executions SET state = 'publishing', updated_at = ?
                    WHERE execution_id = ?""",
                (now, execution_id),
            )
            self._event_and_outbox(
                connection,
                task_id=str(execution["task_id"]),
                claim_id=str(row["claim_id"]),
                event_type="execution.publishing",
                payload={"execution_id": execution_id, "paths": list(paths)},
                now=now,
            )

    def _workspace_execution_row(
        self, connection: sqlite3.Connection, execution_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT e.*,c.canonical_path,c.path_case_insensitive,c.checkout_id,"
            "COALESCE(w.source_workspace_id,w.workspace_id) "
            "AS publication_workspace_id,"
            "t.attempt "
            "AS current_attempt,t.repo_key FROM workspace_executions e "
            "JOIN tasks t ON t.task_id=e.task_id "
            "JOIN sessions s ON s.session_id=e.session_id "
            "AND t.session_id=e.session_id AND s.workspace_id=e.workspace_id "
            "JOIN workspaces w ON w.workspace_id=e.workspace_id "
            "AND w.checkout_id=s.checkout_id AND w.canonical_path=e.worktree_path "
            "JOIN checkouts c ON c.checkout_id=s.checkout_id "
            "WHERE e.execution_id=? AND t.current_claim_id IS NULL "
            "AND t.current_fencing_token IS NULL",
            (execution_id,),
        ).fetchone()
        if row is None or int(row["task_attempt"]) != int(row["current_attempt"]):
            raise ClaimAuthorityError("workspace execution attempt is stale")
        authority = self.store.authority
        if authority is None:
            raise ClaimAuthorityError("workspace execution requires physical authority")
        regime = authority.assert_regime(
            Path(row["canonical_path"]),
            allowed=("workspace",),
            generation=int(row["regime_generation"]),
        )
        if regime["physical_key"] != row["physical_key"]:
            raise ClaimAuthorityError("workspace execution physical binding changed")
        return cast(sqlite3.Row, row)

    def reserve_workspace_publication(
        self,
        execution_id: str,
        *,
        regime_generation: int,
        changed_paths: Iterable[str],
    ) -> None:
        """Reserve a claim-free batch using its immutable execution authority."""
        paths = tuple(normalize_changed_path(path) for path in changed_paths)
        with self._transaction() as connection:
            row = self._workspace_execution_row(connection, execution_id)
            if int(row["regime_generation"]) != regime_generation:
                raise ClaimAuthorityError("workspace regime generation is stale")
            if row["state"] not in {"running", "validated"}:
                raise ClaimConflict("workspace execution is not ready to publish")
            stopped = connection.execute(
                "SELECT stop_requested FROM task_workflows WHERE task_id=? "
                "AND attempt=?",
                (row["task_id"], row["task_attempt"]),
            ).fetchone()
            if stopped is not None and stopped[0]:
                raise ClaimAuthorityError("task was cancelled before publication")
            scopes = json.loads(str(row["scopes_json"]))
            if not isinstance(scopes, list) or any(
                not isinstance(x, str) for x in scopes
            ):
                raise ClaimAuthorityError("workspace execution scopes are invalid")
            if uncovered_paths(
                scopes,
                paths,
                case_insensitive_filesystem=bool(row["path_case_insensitive"]),
            ):
                raise ScopeAuthorityError("workspace batch contains out-of-scope paths")
            now = self.store._now(None)
            connection.execute(
                "UPDATE workspace_executions SET state='publishing',updated_at=? "
                "WHERE execution_id=?",
                (now, execution_id),
            )
            connection.execute(
                "UPDATE tasks SET coordination_state='publishing',updated_at=? "
                "WHERE task_id=? AND attempt=?",
                (now, row["task_id"], row["task_attempt"]),
            )
            self._workspace_event(
                connection,
                row,
                "execution.publishing",
                {"execution_id": execution_id, "paths": list(paths)},
                now,
            )

    def settle_workspace_execution(
        self, execution_id: str, *, outcome: str, failure_code: str | None = None
    ) -> None:
        """Settle only exact journal/checkpoint evidence, without any claim row."""
        if outcome not in {"published", "diverged", "no_changes", "failed_safe"}:
            raise ValueError("unknown workspace execution outcome")
        with self._transaction() as connection:
            row = self._workspace_execution_row(connection, execution_id)
            batch = connection.execute(
                "SELECT state,session_id,workspace_id,checkout_id "
                "FROM workspace_batches "
                "WHERE batch_id=?",
                (execution_id,),
            ).fetchone()
            if outcome in {"published", "diverged"}:
                if batch is None or batch["state"] != outcome:
                    raise ClaimConflict("workspace batch outcome is not established")
                if (
                    batch["session_id"] != row["session_id"]
                    or batch["workspace_id"] != row["publication_workspace_id"]
                    or batch["checkout_id"] != row["checkout_id"]
                ):
                    raise ClaimConflict(
                        "workspace batch belongs to another execution binding"
                    )
            elif batch is not None:
                raise ClaimConflict("a journaled batch requires its verified outcome")
            saved = connection.execute(
                "SELECT checkpoint_json FROM workspace_execution_checkpoints "
                "WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            checkpoint = json.loads(str(saved[0])) if saved is not None else {}
            if not isinstance(checkpoint, dict):
                raise ClaimConflict("workspace execution checkpoint is invalid")
            if outcome == "no_changes":
                tool_usage = checkpoint.get("tool_usage")
                state = (
                    tool_usage.get("shared_workspace_state")
                    if isinstance(tool_usage, dict)
                    else None
                )
                pending = state.get("files") if isinstance(state, dict) else None
                if (
                    checkpoint.get("phase") != "finished"
                    or not isinstance(pending, list)
                    or any(not isinstance(entry, dict) for entry in pending)
                    or any(
                        entry.get("pending", entry.get("content") is not None)
                        for entry in pending
                    )
                ):
                    raise ClaimConflict(
                        "read-only completion has no finished checkpoint"
                    )
            success = outcome in {"published", "no_changes"}
            if (
                success
                and row["driver"] != "workflow"
                and checkpoint.get("phase") != "finished"
            ):
                raise ClaimConflict("completion has no finished workspace checkpoint")
            failure = (
                None
                if success
                else "PATH_BASE_MISMATCH"
                if outcome == "diverged"
                else failure_code or "DAEMON_INTERRUPTED"
            )
            terminal = "published" if success else "failed"
            if row["state"] in {"published", "failed"}:
                if row["state"] != terminal or row["failure_code"] != failure:
                    raise ClaimConflict(
                        "workspace execution already has another outcome"
                    )
                return
            task_state = (
                "completed"
                if success
                else "cancelled"
                if failure == "CANCELLED"
                else "reviewing"
                if failure == "REVIEW_REQUIRED"
                else "failed"
            )
            now = self.store._now(None)
            if (
                (
                    success
                    or failure
                    in {"REVIEW_REQUIRED", "MODEL_BLOCKED", "RESPONSE_INCOMPLETE"}
                )
                and checkpoint.get("phase") == "finished"
                and row["driver"] != "workflow"
            ):
                self.store.promote_workspace_conversation(
                    connection, execution_id, now=now
                )
            connection.execute(
                "UPDATE workspace_executions SET state=?,failure_code=?,finished_at=?,"
                "updated_at=? WHERE execution_id=?",
                (terminal, failure, now, now, execution_id),
            )
            connection.execute(
                "UPDATE tasks SET state=?,coordination_state='released',"
                "current_claim_id=NULL,current_fencing_token=NULL,failure_code=?,"
                "finished_at=?,updated_at=? WHERE task_id=? AND attempt=?",
                (
                    task_state,
                    failure if task_state == "failed" else None,
                    now,
                    now,
                    row["task_id"],
                    row["task_attempt"],
                ),
            )
            self._workspace_event(
                connection,
                row,
                "execution.published" if success else "execution.failed",
                {
                    "execution_id": execution_id,
                    "outcome": outcome,
                    "failure_code": failure,
                },
                now,
            )

    def _workspace_event(
        self,
        connection: sqlite3.Connection,
        execution: sqlite3.Row,
        event_type: str,
        payload: Mapping[str, object],
        now: int,
    ) -> None:
        body = {**payload, "execution_contract": "workspace_v1"}
        event_id = self.store.append_task_event(
            connection,
            task_id=str(execution["task_id"]),
            claim_id=None,
            event_type=event_type,
            payload=body,
            now=now,
        )
        self.store.enqueue_outbox(
            connection,
            idempotency_key=f"{event_type}:{event_id}",
            event_type=event_type,
            aggregate_type="workspace_execution",
            aggregate_id=str(execution["execution_id"]),
            payload=body,
            now=now,
        )

    def settle_read_only_execution(
        self,
        execution_id: str,
        *,
        fencing_token: int,
        result_tree_id: str,
        patch_hash: str,
        summary: str,
        tool_calls: int,
        usage: Mapping[str, int],
    ) -> ReleaseResult:
        """Complete an isolated execution whose validated tree has no changes.

        The runner supplies identities from the trusted Git validation step. The
        finished checkpoint, conversation promotion, execution outcome, task
        outcome, claim release, and public lifecycle event commit together, so a
        direct answer never needs a fake publication or a failure state.
        """

        if (
            not result_tree_id
            or len(patch_hash) != 64
            or tool_calls < 0
            or any(
                not isinstance(key, str)
                or not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                for key, value in usage.items()
            )
        ):
            raise ValueError("read-only execution evidence is invalid")
        with self._transaction() as connection:
            execution = connection.execute(
                "SELECT * FROM task_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if execution is None or execution["workspace_id"] is not None:
                raise ClaimConflict("execution is not an isolated task")
            row = self._claim_row(connection, str(execution["claim_id"]))
            repo_key = str(row["repo_key"])
            now = self._effective_now(connection, repo_key, self.store._now(None))
            if not self._has_work_authority(
                row,
                task_id=str(execution["task_id"]),
                claim_id=str(execution["claim_id"]),
                fencing_token=fencing_token,
                attempt=int(execution["task_attempt"]),
                now=now,
            ):
                raise ClaimAuthorityError("claim lease or fencing authority is stale")
            if execution["state"] != "running":
                raise ClaimConflict("read-only execution is not running")
            open_intent = connection.execute(
                """SELECT 1 FROM publication_intents
                    WHERE task_id = ? AND task_attempt = ? LIMIT 1""",
                (execution["task_id"], execution["task_attempt"]),
            ).fetchone()
            if open_intent is not None:
                raise ClaimConflict(
                    "read-only execution cannot have a publication intent"
                )
            saved = connection.execute(
                "SELECT checkpoint_json FROM execution_checkpoints "
                "WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            checkpoint = json.loads(str(saved["checkpoint_json"])) if saved else {}
            if (
                not isinstance(checkpoint, dict)
                or checkpoint.get("phase") != "finished"
            ):
                raise ClaimConflict("read-only completion has no finished checkpoint")
            self.store.promote_execution_conversation(connection, execution_id, now=now)
            connection.execute(
                """UPDATE task_executions SET state = 'published',
                    result_tree_id = ?, patch_hash = ?, summary = ?, tool_calls = ?,
                    usage_json = ?, failure_code = NULL, finished_at = ?, updated_at = ?
                    WHERE execution_id = ?""",
                (
                    result_tree_id,
                    patch_hash,
                    summary,
                    tool_calls,
                    json.dumps(dict(usage), sort_keys=True),
                    now,
                    now,
                    execution_id,
                ),
            )
            connection.execute(
                """UPDATE claims SET state = 'released', lease_expires_at = NULL,
                    release_reason = 'read_only_completed', terminal_expires_at = ?,
                    released_at = ?, updated_at = ? WHERE claim_id = ?""",
                (
                    now + self.terminal_retention_ms,
                    now,
                    now,
                    row["claim_id"],
                ),
            )
            connection.execute(
                """UPDATE tasks SET state = 'completed',
                    coordination_state = 'released', current_fencing_token = NULL,
                    failure_code = NULL, finished_at = ?, updated_at = ?
                    WHERE task_id = ? AND attempt = ?""",
                (now, now, execution["task_id"], execution["task_attempt"]),
            )
            self._event_and_outbox(
                connection,
                task_id=str(execution["task_id"]),
                claim_id=str(row["claim_id"]),
                event_type="execution.published",
                payload={"execution_id": execution_id, "outcome": "no_changes"},
                now=now,
            )
            activated = self._activate_waiters(connection, repo_key, now)
            fresh = self._claim_row(connection, str(row["claim_id"]))
            return ReleaseResult(
                released=True,
                claim=self.store.claim_from_row(connection, fresh),
                activated=activated,
            )

    def settle_shared_execution(
        self, execution_id: str, *, outcome: str, failure_code: str | None = None
    ) -> ReleaseResult:
        """Release only a batch outcome established by durable authority.

        The execution ID is its immutable batch ID. No batch means no checkout
        write can have begun; otherwise only published/diverged are terminal.
        The journal, claim, task and execution are checked/settled together.
        """

        if outcome not in {"published", "diverged", "no_changes", "failed_safe"}:
            raise ValueError("unknown shared execution outcome")
        with self._transaction() as connection:
            execution = connection.execute(
                "SELECT * FROM task_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if execution is None or execution["workspace_id"] is None:
                raise ClaimConflict("execution is not a shared workspace task")
            batch = connection.execute(
                "SELECT state FROM workspace_batches WHERE batch_id = ?",
                (execution_id,),
            ).fetchone()
            if outcome in {"published", "diverged"}:
                if batch is None or batch["state"] != outcome:
                    raise ClaimConflict("shared batch outcome is not established")
            elif batch is not None:
                raise ClaimConflict("a journaled batch requires its verified outcome")
            if outcome == "no_changes":
                saved = connection.execute(
                    "SELECT checkpoint_json FROM execution_checkpoints "
                    "WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                state = json.loads(str(saved["checkpoint_json"])) if saved else {}
                usage = state.get("tool_usage", {})
                files = usage.get("shared_workspace_state", {}).get("files", [])
                if state.get("phase") != "finished" or any(
                    entry.get("pending", entry.get("content") is not None)
                    for entry in files
                ):
                    raise ClaimConflict(
                        "read-only completion has no finished checkpoint"
                    )
            row = self._claim_row(connection, str(execution["claim_id"]))
            if str(row["state"]) in _TERMINAL_STATES:
                # Cancellation/expiry may have terminalized the claim while
                # its worker was still preparing private edits. Settle the
                # abandoned execution too, without rewriting that task's
                # disposition or any newer attempt's session conversation.
                terminal = (
                    "published" if outcome in {"published", "no_changes"} else "failed"
                )
                if execution["state"] not in {"published", "failed"}:
                    timestamp = self.store._now(None)
                    connection.execute(
                        """UPDATE task_executions SET state = ?, failure_code = ?,
                            finished_at = ?, updated_at = ? WHERE execution_id = ?""",
                        (
                            terminal,
                            failure_code or "DAEMON_INTERRUPTED",
                            timestamp,
                            timestamp,
                            execution_id,
                        ),
                    )
                return ReleaseResult(
                    released=False, claim=self.store.claim_from_row(connection, row)
                )
            success = outcome in {"published", "no_changes"}
            failure = (
                None
                if success
                else (
                    "PATH_BASE_MISMATCH"
                    if outcome == "diverged"
                    else (failure_code or "DAEMON_INTERRUPTED")
                )
            )
            # A held proposal is a settled task, not a transient failure.
            # Commit its final disposition with the claim release so polling
            # clients cannot stop watching on an intermediate failed state.
            task_state = "completed" if success else "failed"
            if failure == "CANCELLED":
                task_state = "cancelled"
            elif failure == "REVIEW_REQUIRED":
                task_state = "reviewing"
            now = self._effective_now(
                connection, str(row["repo_key"]), self.store._now(None)
            )
            finished_failure = False
            if failure in {
                "REVIEW_REQUIRED",
                "MODEL_BLOCKED",
                "RESPONSE_INCOMPLETE",
            }:
                saved = connection.execute(
                    "SELECT checkpoint_json FROM execution_checkpoints "
                    "WHERE execution_id=?", (execution_id,),
                ).fetchone()
                checkpoint = json.loads(saved["checkpoint_json"]) if saved else None
                finished_failure = (
                    isinstance(checkpoint, dict)
                    and checkpoint.get("phase") == "finished"
                )
            if (success or finished_failure) and execution["driver"] != "workflow":
                self.store.promote_shared_conversation(
                    connection, execution_id, now=now
                )
            connection.execute(
                """UPDATE claims SET state = 'released', lease_expires_at = NULL,
                    release_reason = ?, terminal_expires_at = ?, released_at = ?,
                    updated_at = ? WHERE claim_id = ?""",
                (
                    f"shared_{outcome}",
                    now + self.terminal_retention_ms,
                    now,
                    now,
                    row["claim_id"],
                ),
            )
            connection.execute(
                """UPDATE tasks SET state = ?, coordination_state = 'released',
                    current_fencing_token = NULL, failure_code = ?, finished_at = ?,
                    updated_at = ? WHERE task_id = ? AND attempt = ?""",
                (
                    task_state,
                    failure if task_state == "failed" else None,
                    now,
                    now,
                    execution["task_id"],
                    execution["task_attempt"],
                ),
            )
            connection.execute(
                """UPDATE task_executions SET state = ?, failure_code = ?,
                    finished_at = ?, updated_at = ? WHERE execution_id = ?""",
                ("published" if success else "failed", failure, now, now, execution_id),
            )
            self._event_and_outbox(
                connection,
                task_id=str(execution["task_id"]),
                claim_id=str(row["claim_id"]),
                event_type="execution.published" if success else "execution.failed",
                payload={
                    "execution_id": execution_id,
                    "outcome": outcome,
                    "failure_code": failure,
                },
                now=now,
            )
            activated = self._activate_waiters(connection, str(row["repo_key"]), now)
            return ReleaseResult(
                released=True,
                claim=self.store.claim_from_row(
                    connection, self._claim_row(connection, str(row["claim_id"]))
                ),
                activated=activated,
            )

    def settle_publication(
        self, task_id: str, *, now: int | None = None
    ) -> ReleaseResult:
        """Release a confirmed reservation while keeping its published result.

        A task owned by a durable session is a short-lived child of that
        session, so it must not keep write authority once its result is safely
        on the internal ref: the operator is now thinking about the next
        prompt, and every overlapping session -- including their own next one
        -- would otherwise wait behind them.

        This is the opposite of :meth:`discard_integration`, which throws the
        result away.  Here the ref stands and the task is recorded as
        completed; only the path reservation ends.
        """

        timestamp = self.store._now(now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM claims WHERE task_id = ?
                    AND state IN ('publishing', 'active_integration')
                ORDER BY queue_sequence DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise ClaimConflict("task has no publication reservation to settle")
            if row["state"] == "publishing":
                raise ClaimConflict(
                    "publishing outcome is not confirmed; reconcile it before settling"
                )
            repo_key = str(row["repo_key"])
            effective_now = self._effective_now(connection, repo_key, timestamp)
            claim_id = str(row["claim_id"])
            connection.execute(
                """
                UPDATE claims SET state = 'released',
                    release_reason = 'session_task_completed',
                    terminal_expires_at = ?, released_at = ?, updated_at = ?
                WHERE claim_id = ? AND state = 'active_integration'
                """,
                (
                    effective_now + self.terminal_retention_ms,
                    effective_now,
                    effective_now,
                    claim_id,
                ),
            )
            connection.execute(
                """
                UPDATE tasks SET state = 'completed', coordination_state = 'released',
                    finished_at = ?, updated_at = ? WHERE task_id = ?
                """,
                (effective_now, effective_now, task_id),
            )
            self._event_and_outbox(
                connection,
                task_id=task_id,
                claim_id=claim_id,
                event_type="integration.settled",
                payload={"result_revision": row["result_revision"]},
                now=effective_now,
            )
            activated = self._activate_waiters(connection, repo_key, effective_now)
            fresh = self._claim_row(connection, claim_id)
            return ReleaseResult(
                released=True,
                claim=self.store.claim_from_row(connection, fresh),
                activated=activated,
            )

    def discard_integration(
        self, task_id: str, *, now: int | None = None
    ) -> ReleaseResult:
        """Explicitly discard a confirmed branch result and release its reservation."""

        timestamp = self.store._now(now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM claims WHERE task_id = ?
                    AND state IN ('publishing', 'active_integration')
                ORDER BY queue_sequence DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise ClaimConflict("task has no publication reservation to discard")
            if row["state"] == "publishing":
                raise ClaimConflict(
                    "publishing outcome is not confirmed; reconcile it before discard"
                )
            repo_key = str(row["repo_key"])
            effective_now = self._effective_now(connection, repo_key, timestamp)
            claim_id = str(row["claim_id"])
            connection.execute(
                """
                UPDATE claims SET state = 'released',
                    release_reason = 'explicit_discard',
                    terminal_expires_at = ?, released_at = ?, updated_at = ?
                WHERE claim_id = ? AND state = 'active_integration'
                """,
                (
                    effective_now + self.terminal_retention_ms,
                    effective_now,
                    effective_now,
                    claim_id,
                ),
            )
            connection.execute(
                """
                UPDATE tasks SET state = 'cancelled', coordination_state = 'released',
                    finished_at = ?, updated_at = ? WHERE task_id = ?
                """,
                (effective_now, effective_now, task_id),
            )
            self._event_and_outbox(
                connection,
                task_id=task_id,
                claim_id=claim_id,
                event_type="integration.discarded",
                payload={},
                now=effective_now,
            )
            activated = self._activate_waiters(connection, repo_key, effective_now)
            fresh = self._claim_row(connection, claim_id)
            return ReleaseResult(
                released=True,
                claim=self.store.claim_from_row(connection, fresh),
                activated=activated,
            )

    def _expire_stale(
        self, connection: sqlite3.Connection, repo_key: str, now: int
    ) -> tuple[tuple[str, str], ...]:
        rows = connection.execute(
            """
            SELECT * FROM claims WHERE repo_key = ? AND state = 'active_work'
                AND lease_expires_at <= ? ORDER BY queue_sequence
            """,
            (repo_key, now),
        ).fetchall()
        expired: list[tuple[str, str]] = []
        for row in rows:
            updated = connection.execute(
                """
                UPDATE claims SET state = 'expired', lease_expires_at = NULL,
                    terminal_expires_at = ?, expired_at = ?, updated_at = ?
                WHERE claim_id = ? AND state = 'active_work'
                    AND lease_expires_at <= ?
                """,
                (
                    now + self.terminal_retention_ms,
                    now,
                    now,
                    row["claim_id"],
                    now,
                ),
            )
            if updated.rowcount != 1:
                continue
            task_id = str(row["task_id"])
            claim_id = str(row["claim_id"])
            connection.execute(
                """
                UPDATE tasks SET state = 'failed', coordination_state = 'expired',
                    current_fencing_token = NULL, failure_code = 'CLAIM_STALE',
                    finished_at = ?, updated_at = ? WHERE task_id = ?
                """,
                (now, now, task_id),
            )
            self._event_and_outbox(
                connection,
                task_id=task_id,
                claim_id=claim_id,
                event_type="claim.expired",
                payload={},
                now=now,
            )
            expired.append((claim_id, task_id))
        return tuple(expired)

    def _legacy_claim_binding(
        self,
        connection: sqlite3.Connection,
        task: sqlite3.Row,
        *,
        claim: sqlite3.Row | None = None,
        checkout_id: str | None = None,
    ) -> dict[str, str] | None:
        """Resolve trusted context or immutable legacy proof, never a latest path."""
        authority = self.store.authority
        if authority is None:
            return None
        claim_id = (
            str(claim["claim_id"])
            if claim is not None
            else str(task["current_claim_id"])
            if task["current_claim_id"]
            else None
        )
        saved = (
            connection.execute(
                "SELECT * FROM legacy_claim_bindings WHERE claim_id=?", (claim_id,)
            ).fetchone()
            if claim_id
            else None
        )
        session = (
            connection.execute(
                "SELECT checkout_id FROM sessions WHERE session_id=?",
                (task["session_id"],),
            ).fetchone()
            if task["session_id"]
            else None
        )
        if session is not None:
            if checkout_id is not None and checkout_id != session["checkout_id"]:
                raise ClaimAuthorityError("claim session checkout binding changed")
            checkout_id = str(session["checkout_id"])
        if checkout_id is None and saved is not None:
            checkout_id = str(saved["checkout_id"])
        if checkout_id is None and claim_id is not None:
            proofs = connection.execute(
                "SELECT DISTINCT a.physical_key,c.checkout_id,c.canonical_path "
                "FROM authority_journals j JOIN authority_checkouts a "
                "ON a.physical_key=j.physical_key JOIN checkouts c "
                "ON c.checkout_id=a.checkout_id WHERE c.repo_key=? AND "
                "((j.kind='legacy_claim' AND j.reference_id=?) OR "
                "(j.kind='legacy_execution' AND j.reference_id IN "
                "(SELECT execution_id FROM task_executions WHERE task_id=? "
                "AND task_attempt=?)))",
                (task["repo_key"], claim_id, task["task_id"], task["attempt"]),
            ).fetchall()
            if len(proofs) == 1:
                checkout_id = str(proofs[0]["checkout_id"])
                saved = proofs[0]
        if checkout_id is None:
            return None
        checkout = connection.execute(
            "SELECT c.checkout_id,c.canonical_path,r.target_ref,r.integration_adapter "
            "FROM checkouts c JOIN repositories r ON r.repository_id=c.repository_id "
            "WHERE c.checkout_id=? AND c.repo_key=?",
            (checkout_id, task["repo_key"]),
        ).fetchone()
        if checkout is None:
            raise ClaimAuthorityError("claim physical checkout evidence is missing")
        regime = authority.regime(Path(checkout["canonical_path"]))
        result = {str(key): str(value) for key, value in dict(checkout).items()}
        result["physical_key"] = str(regime["physical_key"])
        if saved is not None and (
            saved["physical_key"] != result["physical_key"]
            or str(Path(saved["canonical_path"]).resolve())
            != str(Path(result["canonical_path"]).resolve())
        ):
            raise ClaimAuthorityError("claim physical checkout evidence changed")
        if saved is not None and any(
            key in dict(saved) and saved[key] != result[key]
            for key in ("target_ref", "integration_adapter")
        ):
            raise ClaimAuthorityError("claim target or adapter evidence changed")
        return result

    def _legacy_claim_can_activate(
        self, connection: sqlite3.Connection, repo_key: str, claim: sqlite3.Row
    ) -> bool:
        try:
            task = self._task_row(connection, str(claim["task_id"]))
            binding = self._legacy_claim_binding(connection, task, claim=claim)
            return legacy_activation_allowed(
                connection,
                repo_key,
                physical_key=binding["physical_key"] if binding else None,
            ) and self._legacy_physical_mappings_available(
                connection, repo_key, binding=binding
            )
        except (LlmCoordError, ClaimAuthorityError, OSError, ValueError):
            return False

    def _legacy_physical_mappings_available(
        self,
        connection: sqlite3.Connection,
        repo_key: str,
        *,
        binding: dict[str, str] | None = None,
    ) -> bool:
        """Check global markers; unbound older clients stay conservative."""
        authority = self.store.authority
        if authority is None:
            return True
        if binding is not None:
            regime = authority.regime(Path(binding["canonical_path"]))
            return bool(
                regime["physical_key"] == binding["physical_key"]
                and regime["regime"] == "legacy"
            )
        rows = connection.execute(
            "SELECT canonical_path AS path FROM checkouts WHERE repo_key=? "
            "UNION SELECT main_worktree_path AS path FROM repositories "
            "WHERE repo_key=?",
            (repo_key, repo_key),
        ).fetchall()
        for row in rows:
            try:
                if authority.regime(Path(row["path"]))["regime"] != "legacy":
                    return False
            except (LlmCoordError, OSError, ValueError):
                return False
        return True

    def _activate_waiters(
        self, connection: sqlite3.Connection, repo_key: str, now: int
    ) -> tuple[ClaimRecord, ...]:
        queued = connection.execute(
            """
            SELECT * FROM claims WHERE repo_key = ? AND state = 'queued'
            ORDER BY queue_sequence
            """,
            (repo_key,),
        ).fetchall()
        if not queued:
            return ()
        fold_case = self._path_case_insensitive(connection, repo_key)
        live = connection.execute(
            """
            SELECT * FROM claims WHERE repo_key = ?
                AND state IN ('active_work', 'publishing', 'active_integration')
            ORDER BY queue_sequence
            """,
            (repo_key,),
        ).fetchall()
        reserved: list[tuple[str, tuple[str, ...], str]] = [
            (
                str(row["claim_id"]),
                self._claim_scopes(connection, row),
                str(row["scheduling_mode"]),
            )
            for row in live
            if self._legacy_claim_can_activate(connection, repo_key, row)
        ]
        activated: list[ClaimRecord] = []
        for row in queued:
            if not self._legacy_claim_can_activate(connection, repo_key, row):
                continue
            claim_id = str(row["claim_id"])
            scopes = self._claim_scopes(connection, row)
            scheduling_mode = str(row["scheduling_mode"])
            blockers = [
                reserved_id
                for reserved_id, reserved_scopes, reserved_mode in reserved
                if self._reservations_conflict(
                    scopes,
                    scheduling_mode,
                    reserved_scopes,
                    reserved_mode,
                    case_insensitive_filesystem=fold_case,
                )
            ]
            self._replace_blockers(connection, claim_id, blockers, repo_key, now)
            if blockers:
                reserved.append((claim_id, scopes, scheduling_mode))
                continue
            fence = self._next_fence(connection, repo_key, now)
            updated = connection.execute(
                """
                UPDATE claims SET state = 'active_work', fencing_token = ?,
                    lease_expires_at = ?, activated_at = ?, updated_at = ?
                WHERE claim_id = ? AND state = 'queued'
                """,
                (fence, now + self.launch_lease_ms, now, now, claim_id),
            )
            if updated.rowcount != 1:
                continue
            connection.execute(
                "DELETE FROM claim_blockers WHERE waiting_claim_id = ?", (claim_id,)
            )
            connection.execute(
                """
                UPDATE tasks SET state = 'preparing',
                    coordination_state = 'active_work', current_fencing_token = ?,
                    started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE task_id = ?
                """,
                (fence, now, now, row["task_id"]),
            )
            self._event_and_outbox(
                connection,
                task_id=str(row["task_id"]),
                claim_id=claim_id,
                event_type="claim.granted",
                payload={
                    "fencing_token": fence,
                    "scheduling_mode": scheduling_mode,
                    "workspace_id": row["workspace_id"],
                },
                now=now,
            )
            fresh = self._claim_row(connection, claim_id)
            record = self.store.claim_from_row(connection, fresh)
            activated.append(record)
            reserved.append((claim_id, scopes, scheduling_mode))
        return tuple(activated)

    @staticmethod
    def _reservations_conflict(
        scopes: tuple[str, ...],
        scheduling_mode: str,
        reserved_scopes: tuple[str, ...],
        reserved_mode: str,
        *,
        case_insensitive_filesystem: bool,
    ) -> bool:
        # Use the same predicate for a new request and waiter activation. An
        # older exclusive waiter reserves its scopes even against optimistic
        # newcomers, so a stream of private preparation cannot starve it.
        return (
            scheduling_mode != "optimistic" or reserved_mode != "optimistic"
        ) and scope_sets_overlap(
            scopes,
            reserved_scopes,
            case_insensitive_filesystem=case_insensitive_filesystem,
        )

    @staticmethod
    def _optimistic_workspace(
        connection: sqlite3.Connection,
        task: sqlite3.Row,
        optimistic_driver: str | None,
    ) -> str:
        """Bind registry-approved private preparation to its durable workspace."""

        binding = connection.execute(
            """
            SELECT workspace.workspace_id
            FROM sessions AS session
            JOIN workspaces AS workspace
                ON workspace.workspace_id = session.workspace_id
            JOIN checkouts AS checkout
                ON checkout.checkout_id = session.checkout_id
            JOIN task_execution_launches AS launch
                ON launch.task_id = ? AND launch.task_attempt = ?
            LEFT JOIN isolated_workspaces AS isolated
                ON isolated.workspace_id = workspace.workspace_id
            LEFT JOIN workspaces AS source
                ON source.workspace_id = workspace.source_workspace_id
            WHERE session.session_id = ? AND session.state = 'active'
                AND workspace.state = 'active'
                AND workspace.checkout_id = checkout.checkout_id
                AND (
                    (session.workspace_mode = 'shared'
                        AND workspace.mode = 'shared'
                        AND workspace.kind = 'shared_checkout'
                        AND workspace.canonical_path = checkout.canonical_path)
                    OR
                    (session.workspace_mode = 'isolated'
                        AND workspace.mode = 'isolated'
                        AND workspace.kind = 'linked_worktree'
                        AND isolated.session_id = session.session_id
                        AND isolated.stage = 'ready'
                        AND source.state = 'active' AND source.mode = 'shared'
                        AND source.kind = 'shared_checkout'
                        AND source.checkout_id = checkout.checkout_id
                        AND source.canonical_path = checkout.canonical_path)
                )
                AND checkout.repository_id = ? AND checkout.repo_key = ?
                AND launch.driver = ?
            """,
            (
                task["task_id"],
                task["attempt"],
                task["session_id"],
                task["repository_id"],
                task["repo_key"],
                optimistic_driver,
            ),
        ).fetchone()
        if binding is None:
            raise ClaimAuthorityError(
                "optimistic scheduling requires an approved saved launch and "
                "an active shared session workspace or ready isolated session workspace"
            )
        workspace_id = str(binding["workspace_id"])
        execution = connection.execute(
            "SELECT workspace_id, driver FROM task_executions "
            "WHERE task_id = ? AND task_attempt = ?",
            (task["task_id"], task["attempt"]),
        ).fetchone()
        if execution is not None and (
            execution["workspace_id"] != workspace_id
            or execution["driver"] != optimistic_driver
        ):
            raise ClaimAuthorityError(
                "optimistic scheduling cannot reuse a different execution workspace"
            )
        return workspace_id

    @staticmethod
    def _insert_scopes(
        connection: sqlite3.Connection,
        claim_id: str,
        scopes: tuple[str, ...],
        source: str,
    ) -> None:
        if source not in {"user", "planner", "fallback", "policy"}:
            raise ValueError("unknown scope source")
        connection.executemany(
            """
            INSERT INTO claim_scopes(
                claim_id, ordinal, value, is_directory, source
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                (claim_id, index, scope, int(scope.endswith("/")), source)
                for index, scope in enumerate(scopes)
            ),
        )

    @staticmethod
    def _replace_blockers(
        connection: sqlite3.Connection,
        waiting_claim_id: str,
        blocker_ids: Iterable[str],
        repo_key: str,
        now: int,
    ) -> None:
        connection.execute(
            "DELETE FROM claim_blockers WHERE waiting_claim_id = ?",
            (waiting_claim_id,),
        )
        revision = connection.execute(
            "SELECT revision FROM repository_heads WHERE repo_key = ?", (repo_key,)
        ).fetchone()
        assert revision is not None
        connection.executemany(
            """
            INSERT INTO claim_blockers(
                waiting_claim_id, blocking_claim_id, observed_revision, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                (waiting_claim_id, blocker_id, int(revision["revision"]), now)
                for blocker_id in blocker_ids
            ),
        )

    @staticmethod
    def _path_case_insensitive(connection: sqlite3.Connection, repo_key: str) -> bool:
        """Return the registered working-tree case behavior for one repository.

        Read inside the authority transaction so a contention decision cannot
        be made against a stale or absent registration.  An unregistered head
        fails closed to case-insensitive: over-contending is safe, while
        under-contending hands two claims the same physical path.
        """

        row = connection.execute(
            "SELECT path_case_insensitive FROM repositories WHERE repo_key = ?",
            (repo_key,),
        ).fetchone()
        if row is None:
            return True
        return bool(row["path_case_insensitive"])

    @staticmethod
    def _claim_scopes(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> tuple[str, ...]:
        return tuple(
            str(item["value"])
            for item in connection.execute(
                "SELECT value FROM claim_scopes WHERE claim_id = ? ORDER BY ordinal",
                (row["claim_id"],),
            )
        )

    @staticmethod
    def _task_row(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"task {task_id!r} does not exist")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _claim_row(connection: sqlite3.Connection, claim_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        if row is None:
            raise ClaimNotFound(f"claim {claim_id!r} does not exist")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _has_work_authority(
        row: sqlite3.Row,
        *,
        task_id: str,
        claim_id: str,
        fencing_token: int,
        attempt: int,
        now: int,
    ) -> bool:
        return bool(
            row["claim_id"] == claim_id
            and row["task_id"] == task_id
            and int(row["task_attempt"]) == attempt
            and row["state"] == "active_work"
            and row["fencing_token"] == fencing_token
            and row["lease_expires_at"] is not None
            and int(row["lease_expires_at"]) > now
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Open one ``BEGIN IMMEDIATE`` coordination transaction.

        Any time the process just spent suspended is first taken off every
        live lease, in its own transaction, so a rollback of the caller's
        work cannot undo it.
        """

        self._discount_suspension()
        with self.store.connection() as connection, immediate_transaction(connection):
            yield connection

    def _discount_suspension(self) -> None:
        """Extend live leases by the time this process spent suspended.

        A lease is a crash backstop: it measures how long the daemon has gone
        without hearing from a worker. While the machine sleeps, the daemon --
        the only authority that can grant a claim's scopes -- is suspended with
        every worker, so that time is not evidence that a worker died, and no
        one can have taken the scopes meanwhile. Without this, a laptop that
        slept longer than a lease would fail every running task on waking.
        """

        with self._suspension_lock:
            wall, monotonic = self._wall_clock(), self._monotonic_clock()
            last_wall, last_monotonic = self._clock_reference
            self._clock_reference = (wall, monotonic)
            gap_ms = int(((wall - last_wall) - (monotonic - last_monotonic)) * 1_000)
            if gap_ms > _SUSPENSION_TOLERANCE_MS:
                self._unapplied_suspension_ms += gap_ms
            if self._unapplied_suspension_ms == 0:
                return
            with (
                self.store.connection() as connection,
                immediate_transaction(connection),
            ):
                connection.execute(
                    """
                    UPDATE claims SET lease_expires_at = lease_expires_at + ?
                    WHERE state = 'active_work' AND lease_expires_at IS NOT NULL
                    """,
                    (self._unapplied_suspension_ms,),
                )
            self._unapplied_suspension_ms = 0

    @staticmethod
    def _effective_now(
        connection: sqlite3.Connection, repo_key: str, candidate: int
    ) -> int:
        head = connection.execute(
            "SELECT last_effective_time FROM repository_heads WHERE repo_key = ?",
            (repo_key,),
        ).fetchone()
        if head is None:
            raise KeyError(f"repository head {repo_key!r} does not exist")
        effective = max(candidate, int(head["last_effective_time"]))
        connection.execute(
            """
            UPDATE repository_heads SET last_effective_time = ?, updated_at = ?
            WHERE repo_key = ?
            """,
            (effective, effective, repo_key),
        )
        return effective

    @staticmethod
    def _next_queue_sequence(
        connection: sqlite3.Connection, repo_key: str, now: int
    ) -> int:
        row = connection.execute(
            """
            UPDATE repository_heads SET next_queue_sequence = next_queue_sequence + 1,
                revision = revision + 1, updated_at = ? WHERE repo_key = ?
            RETURNING next_queue_sequence
            """,
            (now, repo_key),
        ).fetchone()
        assert row is not None
        return int(row["next_queue_sequence"])

    @staticmethod
    def _next_fence(connection: sqlite3.Connection, repo_key: str, now: int) -> int:
        row = connection.execute(
            """
            UPDATE repository_heads SET next_fencing_token = next_fencing_token + 1,
                revision = revision + 1, updated_at = ? WHERE repo_key = ?
            RETURNING next_fencing_token
            """,
            (now, repo_key),
        ).fetchone()
        assert row is not None
        return int(row["next_fencing_token"])

    def _event_and_outbox(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        claim_id: str,
        event_type: str,
        payload: dict[str, object],
        now: int,
    ) -> None:
        # The event ID keys the outbox row.  A timestamp is not distinguishing:
        # effective time is clamped monotonically, so several events for one
        # claim share a millisecond and ``ON CONFLICT DO NOTHING`` would drop
        # all but the first audit record.
        event_id = self.store.append_task_event(
            connection,
            task_id=task_id,
            claim_id=claim_id,
            event_type=event_type,
            payload=payload,
            now=now,
        )
        self.store.enqueue_outbox(
            connection,
            idempotency_key=f"{event_type}:{claim_id}:{event_id}",
            event_type=event_type,
            aggregate_type="claim",
            aggregate_id=claim_id,
            payload=payload,
            now=now,
        )


__all__ = ["RepositoryCoordinator"]
