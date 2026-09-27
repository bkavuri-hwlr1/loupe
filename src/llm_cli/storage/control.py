"""Typed access to the authoritative control database."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from llm_cli.agent.finalization import (
    checkpoint_finalization,
    validate_finalization_transition,
)
from llm_cli.agent.limits import MAX_ANSWER_CHARACTERS, MAX_TASK_SUMMARY_CHARACTERS
from llm_cli.agent.modes import publication_mode, validate_agent_mode
from llm_cli.coordination.models import (
    EFFORT_LEVELS,
    CheckoutEventRecord,
    CheckoutRecord,
    ClaimConflict,
    ClaimRecord,
    ClaimState,
    ExecutionAnswerRecord,
    ExecutionCheckpointRecord,
    ExecutionLaunchRecord,
    ExecutionRecord,
    PublicationIntentRecord,
    RepositoryRecord,
    SessionCursorRecord,
    SessionIntentRecord,
    SessionRecord,
    TaskEventRecord,
    TaskRecord,
    WorkspaceCandidateRecord,
    WorkspaceDivergenceRecord,
    WorkspacePublicationRecord,
    WorkspaceRecord,
)
from llm_cli.coordination.scopes import normalize_scopes, scope_sets_overlap
from llm_cli.storage.connection import connect_control, immediate_transaction
from llm_cli.storage.migrations import apply_migrations
from llm_cli.workspace.identity import FileIdentity, ObjectKind

_REPO_KEY = re.compile(r"^[a-f0-9]{64}$")
_ANSWER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_ANSWER_CHUNK_CHARACTERS = 4096


def _accepted_answer(
    value: object, checkpoint: Mapping[str, object]
) -> dict[str, object]:
    """Validate the opt-in terminal contract without interpreting native history."""

    if not isinstance(value, Mapping) or set(value) != {
        "version",
        "answer_id",
        "text",
        "summary",
        "outcome",
    }:
        raise ValueError("accepted answer has an invalid envelope")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("accepted answer version is unsupported")
    answer_id, text, summary, outcome = (
        value["answer_id"],
        value["text"],
        value["summary"],
        value["outcome"],
    )
    if not isinstance(answer_id, str) or _ANSWER_ID.fullmatch(answer_id) is None:
        raise ValueError("accepted answer identity is invalid")
    if not isinstance(text, str) or not text.strip() or not isinstance(summary, str):
        raise ValueError("accepted answer requires nonblank text and a summary")
    if len(text) > MAX_ANSWER_CHARACTERS:
        raise ValueError("accepted answer exceeds the answer character limit")
    if len(summary) > MAX_TASK_SUMMARY_CHARACTERS:
        raise ValueError("accepted answer summary exceeds the metadata character limit")
    if not isinstance(outcome, str) or outcome not in {
        "completed",
        "blocked",
        "partial",
    }:
        raise ValueError("accepted answer outcome is invalid")
    tool_usage = checkpoint.get("tool_usage", {})
    if not isinstance(tool_usage, Mapping):
        raise ValueError("accepted answer tool usage is invalid")
    calls = tool_usage.get("calls", 0)
    finished = tool_usage.get("finished", False)
    usage = checkpoint.get("usage_total", {})
    if (
        type(calls) is not int
        or calls < 0
        or not isinstance(finished, bool)
        or not isinstance(usage, Mapping)
        or any(
            not isinstance(key, str) or type(count) is not int or count < 0
            for key, count in usage.items()
        )
    ):
        raise ValueError("accepted answer usage is invalid")
    return {
        "version": 1,
        "answer_id": answer_id,
        "message_id": answer_id,
        "answer": text,
        "summary": summary,
        "outcome": outcome,
        "finished_cleanly": finished,
        "tool_calls": calls,
        "usage": dict(usage),
    }


def _decoded_usage(raw: object) -> dict[str, int]:
    if raw is None:
        return {}
    decoded = json.loads(str(raw))
    if not isinstance(decoded, dict):
        return {}
    return {str(key): int(value) for key, value in decoded.items()}


def session_secret_hash(secret: str) -> str:
    """Return the only representation of a resume secret the daemon keeps."""

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_identifier(prefix: str) -> str:
    """Return an opaque local identifier without host or process information."""

    return f"{prefix}_{uuid.uuid4().hex}"


class ControlStore:
    """Connection factory and small transactional persistence facade.

    The store deliberately opens a fresh connection per operation.  That keeps
    SQLite connection ownership local to a thread while WAL and
    ``BEGIN IMMEDIATE`` provide cross-thread/process coordination.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: float = 30.0,
        clock: object = time.time,
    ) -> None:
        self.path = Path(path).expanduser()
        self.timeout_seconds = timeout_seconds
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.clock = clock

    def connect(self) -> sqlite3.Connection:
        return connect_control(self.path, timeout_seconds=self.timeout_seconds)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield one configured connection and always close it."""

        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> int:
        with self.connection() as connection:
            return apply_migrations(connection)

    def register_repository(
        self,
        *,
        repo_key: str,
        display_name: str,
        git_common_dir: str,
        main_worktree_path: str,
        target_ref: str,
        repository_id: str | None = None,
        profile_id: str = "default",
        remote_identity: str | None = None,
        object_format: str = "sha1",
        integration_adapter: str = "local",
        coordination_mode: str = "enforce",
        path_case_insensitive: bool = True,
        coordinate_by_remote: bool = True,
        now: int | None = None,
    ) -> RepositoryRecord:
        """Register or refresh one already-canonicalized repository target."""

        if _REPO_KEY.fullmatch(repo_key) is None:
            raise ValueError("repo_key must be a lowercase SHA-256 digest")
        required = {
            "display_name": display_name,
            "git_common_dir": git_common_dir,
            "main_worktree_path": main_worktree_path,
            "target_ref": target_ref,
            "profile_id": profile_id,
        }
        if any(not value for value in required.values()):
            raise ValueError("repository identity fields must not be empty")
        timestamp = self._now(now)
        requested_id = repository_id or new_identifier("repo")
        with self.connection() as connection, immediate_transaction(connection):
            existing = connection.execute(
                "SELECT repository_id FROM repositories WHERE repo_key = ?",
                (repo_key,),
            ).fetchone()
            effective_id = (
                str(existing["repository_id"]) if existing is not None else requested_id
            )
            connection.execute(
                """
                INSERT INTO repositories(
                    repository_id, repo_key, profile_id, display_name,
                    git_common_dir, main_worktree_path, remote_identity,
                    target_ref, object_format, integration_adapter,
                    coordination_mode, path_case_insensitive, coordinate_by_remote,
                    created_at, updated_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_key) DO UPDATE SET
                    profile_id = excluded.profile_id,
                    display_name = excluded.display_name,
                    git_common_dir = excluded.git_common_dir,
                    main_worktree_path = excluded.main_worktree_path,
                    remote_identity = excluded.remote_identity,
                    target_ref = excluded.target_ref,
                    object_format = excluded.object_format,
                    integration_adapter = excluded.integration_adapter,
                    coordination_mode = excluded.coordination_mode,
                    path_case_insensitive = excluded.path_case_insensitive,
                    coordinate_by_remote = excluded.coordinate_by_remote,
                    updated_at = excluded.updated_at,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    effective_id,
                    repo_key,
                    profile_id,
                    display_name,
                    git_common_dir,
                    main_worktree_path,
                    remote_identity,
                    target_ref,
                    object_format,
                    integration_adapter,
                    coordination_mode,
                    int(path_case_insensitive),
                    int(coordinate_by_remote),
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO repository_heads(
                    repo_key, next_queue_sequence, next_fencing_token, revision,
                    last_effective_time, created_at, updated_at
                ) VALUES (?, 0, 0, 0, ?, ?, ?)
                ON CONFLICT(repo_key) DO UPDATE SET
                    last_effective_time = MAX(
                        last_effective_time, excluded.last_effective_time
                    ),
                    updated_at = excluded.updated_at
                """,
                (repo_key, timestamp, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM repositories WHERE repo_key = ?", (repo_key,)
            ).fetchone()
            assert row is not None
            return self.repository_from_row(row)

    def create_task(
        self,
        *,
        repository_id: str,
        task_id: str | None = None,
        title: str = "",
        coordination_mode: str | None = None,
        session_id: str | None = None,
        attempt: int = 1,
        parent_task_id: str | None = None,
        group_id: str | None = None,
        now: int | None = None,
    ) -> TaskRecord:
        """Create a root task idempotently by task ID."""

        if attempt <= 0:
            raise ValueError("attempt must be positive")
        timestamp = self._now(now)
        effective_task_id = task_id or new_identifier("task")
        with self.connection() as connection, immediate_transaction(connection):
            repository = connection.execute(
                "SELECT * FROM repositories WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
            if repository is None:
                raise KeyError(f"repository {repository_id!r} is not registered")
            mode = coordination_mode or str(repository["coordination_mode"])
            if session_id is not None:
                session = connection.execute(
                    "SELECT checkout_id, state FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise KeyError(f"session {session_id!r} does not exist")
                if str(session["state"]) != "active":
                    raise ValueError("a task requires an active session")
                checkout = connection.execute(
                    "SELECT repository_id FROM checkouts WHERE checkout_id = ?",
                    (session["checkout_id"],),
                ).fetchone()
                if checkout is None or str(checkout["repository_id"]) != repository_id:
                    raise ValueError("session belongs to another repository")
            existing = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (effective_task_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["repository_id"] != repository_id
                    or int(existing["attempt"]) != attempt
                    or existing["session_id"] != session_id
                ):
                    raise ValueError(
                        "task ID is already bound to another task generation"
                    )
                return self.task_from_row(existing)
            if session_id is not None:
                live_task = connection.execute(
                    """
                    SELECT task_id FROM tasks
                    WHERE session_id = ? AND state NOT IN (
                        'completed', 'failed', 'cancelled', 'ready_for_integration',
                        'operator_attention', 'reviewing'
                    ) LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if live_task is not None:
                    raise ValueError("this session already has an active task")
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, repository_id, repo_key, parent_task_id, group_id,
                    title, state, coordination_mode, coordination_state,
                    attempt, session_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'created', ?, 'unclaimed', ?, ?, ?, ?)
                """,
                (
                    effective_task_id,
                    repository_id,
                    repository["repo_key"],
                    parent_task_id,
                    group_id,
                    title[:500],
                    mode,
                    attempt,
                    session_id,
                    timestamp,
                    timestamp,
                ),
            )
            self.append_task_event(
                connection,
                task_id=effective_task_id,
                claim_id=None,
                event_type="task.created",
                payload={"task_id": effective_task_id},
                now=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (effective_task_id,)
            ).fetchone()
            assert row is not None
            return self.task_from_row(row)

    def begin_new_attempt(
        self,
        task_id: str,
        *,
        now: int | None = None,
    ) -> TaskRecord:
        """Advance a terminal/unclaimed task generation before reacquisition."""

        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(f"task {task_id!r} does not exist")
            live = connection.execute(
                """
                SELECT 1 FROM claims
                WHERE task_id = ? AND state IN (
                    'queued', 'active_work', 'publishing', 'active_integration'
                ) LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if live is not None:
                raise ValueError("cannot advance an attempt while a claim is live")
            connection.execute(
                """
                UPDATE tasks SET
                    attempt = attempt + 1,
                    state = 'queued',
                    coordination_state = 'unclaimed',
                    current_claim_id = NULL,
                    current_fencing_token = NULL,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (timestamp, task_id),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            assert row is not None
            return self.task_from_row(row)

    def get_repository(self, repository_id: str) -> RepositoryRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM repositories WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
            return self.repository_from_row(row) if row is not None else None

    def get_repository_by_key(self, repo_key: str) -> RepositoryRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM repositories WHERE repo_key = ?", (repo_key,)
            ).fetchone()
            return self.repository_from_row(row) if row is not None else None

    def list_repositories(self) -> tuple[RepositoryRecord, ...]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM repositories ORDER BY display_name, repository_id"
            ).fetchall()
            return tuple(self.repository_from_row(row) for row in rows)

    def ensure_checkout(
        self,
        *,
        repository: RepositoryRecord,
        canonical_path: str,
        git_common_dir: str,
        now: int | None = None,
    ) -> CheckoutRecord:
        """Return one exact physical checkout without collapsing sibling clones.

        The current repository row may still be keyed by a remote-derived
        legacy identity.  This additive record is what keeps the new session
        stream local to the checkout the user actually opened.
        """

        if not canonical_path or not git_common_dir:
            raise ValueError("checkout paths must not be empty")
        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            row = connection.execute(
                """
                SELECT * FROM checkouts
                WHERE repository_id = ? AND canonical_path = ?
                """,
                (repository.repository_id, canonical_path),
            ).fetchone()
            if row is None:
                checkout_id = new_identifier("checkout")
                connection.execute(
                    """
                    INSERT INTO checkouts(
                        checkout_id, repository_id, repo_key, canonical_path,
                        git_common_dir, path_case_insensitive, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkout_id,
                        repository.repository_id,
                        repository.repo_key,
                        canonical_path,
                        git_common_dir,
                        int(repository.path_case_insensitive),
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO checkout_heads(checkout_id, created_at, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (checkout_id, timestamp, timestamp),
                )
                row = connection.execute(
                    "SELECT * FROM checkouts WHERE checkout_id = ?", (checkout_id,)
                ).fetchone()
                assert row is not None
            elif (
                str(row["repo_key"]) != repository.repo_key
                or str(row["git_common_dir"]) != git_common_dir
            ):
                raise ValueError(
                    "the registered checkout no longer matches its repository identity"
                )
            return self.checkout_from_row(row)

    def get_checkout(self, checkout_id: str) -> CheckoutRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM checkouts WHERE checkout_id = ?", (checkout_id,)
            ).fetchone()
            return self.checkout_from_row(row) if row is not None else None

    def list_checkouts(
        self, repository_id: str | None = None
    ) -> tuple[CheckoutRecord, ...]:
        with self.connection() as connection:
            if repository_id is None:
                rows = connection.execute(
                    "SELECT * FROM checkouts ORDER BY created_at, checkout_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM checkouts WHERE repository_id = ?
                    ORDER BY created_at, checkout_id
                    """,
                    (repository_id,),
                ).fetchall()
            return tuple(self.checkout_from_row(row) for row in rows)

    def ensure_shared_workspace(
        self, checkout: CheckoutRecord, *, now: int | None = None
    ) -> WorkspaceRecord:
        """Return the one shared workspace for a checkout, creating it once.

        The shared workspace *is* the user's checkout, so it is not something a
        session creates or owns: every session on that checkout coordinates
        through the same record, revision, and epoch.
        """

        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            row = connection.execute(
                """
                SELECT * FROM workspaces
                WHERE checkout_id = ? AND kind = 'shared_checkout'
                """,
                (checkout.checkout_id,),
            ).fetchone()
            if row is None:
                workspace_id = new_identifier("workspace")
                connection.execute(
                    """
                    INSERT INTO workspaces(
                        workspace_id, checkout_id, kind, mode, state,
                        canonical_path, git_dir, created_at, updated_at
                    ) VALUES (?, ?, 'shared_checkout', 'shared', 'active',
                              ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        checkout.checkout_id,
                        checkout.canonical_path,
                        checkout.git_common_dir,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM workspaces WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
            assert row is not None
            return self.workspace_from_row(row)

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            return self.workspace_from_row(row) if row is not None else None

    def record_workspace_read(
        self,
        *,
        session_id: str,
        relative_path: str,
        identity: FileIdentity,
        now: int | None = None,
    ) -> WorkspaceRecord:
        """Persist the base token a session received from the file broker."""

        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            session = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise KeyError(f"session {session_id!r} does not exist")
            if str(session["state"]) != "active" or session["workspace_id"] is None:
                raise ValueError("an active shared-workspace session is required")
            workspace = connection.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?",
                (session["workspace_id"],),
            ).fetchone()
            if workspace is None or str(workspace["mode"]) != "shared":
                raise ValueError("this session does not have a shared workspace")
            connection.execute(
                """
                INSERT INTO workspace_read_observations(
                    session_id, workspace_id, relative_path, observed_kind,
                    observed_mode, observed_digest, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, relative_path) DO UPDATE SET
                    workspace_id = excluded.workspace_id,
                    observed_kind = excluded.observed_kind,
                    observed_mode = excluded.observed_mode,
                    observed_digest = excluded.observed_digest,
                    observed_at = excluded.observed_at
                """,
                (
                    session_id,
                    workspace["workspace_id"],
                    relative_path,
                    str(identity.kind),
                    identity.mode,
                    identity.digest,
                    timestamp,
                ),
            )
            return self.workspace_from_row(workspace)

    def stage_workspace_candidate(
        self,
        *,
        candidate_id: str,
        session_id: str,
        relative_path: str,
        base: FileIdentity,
        result: FileIdentity,
        content_hash: str,
        byte_count: int,
        now: int | None = None,
    ) -> WorkspaceCandidateRecord:
        """Attach retained candidate content to a previously observed base."""

        if (
            not candidate_id
            or len(candidate_id) > 128
            or len(content_hash) != 64
            or _REPO_KEY.fullmatch(content_hash) is None
            or byte_count < 0
            or result.kind is not ObjectKind.REGULAR
        ):
            raise ValueError("candidate identity is invalid")
        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            existing = connection.execute(
                "SELECT * FROM workspace_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if existing is not None:
                expected = (
                    str(existing["session_id"]) == session_id
                    and str(existing["relative_path"]) == relative_path
                    and str(existing["base_kind"]) == str(base.kind)
                    and str(existing["base_mode"]) == base.mode
                    and str(existing["base_digest"]) == base.digest
                    and str(existing["result_kind"]) == str(result.kind)
                    and str(existing["result_mode"]) == result.mode
                    and str(existing["result_digest"]) == result.digest
                    and str(existing["content_hash"]) == content_hash
                    and int(existing["byte_count"]) == byte_count
                )
                if not expected:
                    raise ValueError(
                        "candidate ID is already bound to different content"
                    )
                return self.workspace_candidate_from_row(existing)

            session = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise KeyError(f"session {session_id!r} does not exist")
            workspace_id = session["workspace_id"]
            if str(session["state"]) != "active" or workspace_id is None:
                raise ValueError("an active shared-workspace session is required")
            observation = connection.execute(
                """
                SELECT * FROM workspace_read_observations
                WHERE session_id = ? AND relative_path = ?
                """,
                (session_id, relative_path),
            ).fetchone()
            if observation is None or (
                str(observation["workspace_id"]) != str(workspace_id)
                or str(observation["observed_kind"]) != str(base.kind)
                or str(observation["observed_mode"]) != base.mode
                or str(observation["observed_digest"]) != base.digest
            ):
                raise ValueError(
                    "candidate base must be the latest identity read through "
                    "this session"
                )
            workspace = connection.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            if workspace is None or str(workspace["mode"]) != "shared":
                raise ValueError("this session does not have a shared workspace")
            generation_row = connection.execute(
                """
                SELECT COALESCE(MAX(generation), 0) AS generation
                FROM workspace_candidates WHERE session_id = ? AND relative_path = ?
                """,
                (session_id, relative_path),
            ).fetchone()
            assert generation_row is not None
            generation = int(generation_row["generation"]) + 1
            connection.execute(
                """
                INSERT INTO workspace_candidates(
                    candidate_id, workspace_id, session_id, relative_path, generation,
                    base_kind, base_mode, base_digest,
                    result_kind, result_mode, result_digest,
                    content_hash, byte_count, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?, ?)
                """,
                (
                    candidate_id,
                    workspace_id,
                    session_id,
                    relative_path,
                    generation,
                    str(base.kind),
                    base.mode,
                    base.digest,
                    str(result.kind),
                    result.mode,
                    result.digest,
                    content_hash,
                    byte_count,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM workspace_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            assert row is not None
            return self.workspace_candidate_from_row(row)

    def get_workspace_candidate(
        self, candidate_id: str
    ) -> WorkspaceCandidateRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            return self.workspace_candidate_from_row(row) if row is not None else None

    def get_workspace_publication(
        self, candidate_id: str
    ) -> WorkspacePublicationRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_publications WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            return self.workspace_publication_from_row(row) if row is not None else None

    def get_workspace_divergence(
        self, candidate_id: str
    ) -> WorkspaceDivergenceRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_divergences WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            return self.workspace_divergence_from_row(row) if row is not None else None

    def get_checkout_event(
        self, checkout_id: str, sequence: int
    ) -> CheckoutEventRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM checkout_events
                WHERE checkout_id = ? AND sequence = ?
                """,
                (checkout_id, sequence),
            ).fetchone()
            return self.checkout_event_from_row(row) if row is not None else None

    def begin_workspace_publication(
        self, *, candidate_id: str, session_id: str, now: int | None = None
    ) -> tuple[WorkspaceCandidateRecord, WorkspacePublicationRecord, WorkspaceRecord]:
        """Write the pre-replace publication journal, idempotently."""

        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            candidate = connection.execute(
                "SELECT * FROM workspace_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate is None:
                raise KeyError(f"candidate {candidate_id!r} does not exist")
            if str(candidate["session_id"]) != session_id:
                raise ValueError("candidate belongs to another session")
            if str(candidate["state"]) in {"diverged", "abandoned"}:
                raise ValueError("a diverged or abandoned candidate cannot publish")
            workspace = connection.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?",
                (candidate["workspace_id"],),
            ).fetchone()
            if workspace is None or str(workspace["state"]) != "active":
                raise ValueError("workspace is not ready for publication")
            publication = connection.execute(
                "SELECT * FROM workspace_publications WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if publication is None:
                publication_id = new_identifier("workspace_publication")
                connection.execute(
                    """
                    INSERT INTO workspace_publications(
                        publication_id, candidate_id, workspace_id, checkout_id,
                        operation_state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'prepared', ?, ?)
                    """,
                    (
                        publication_id,
                        candidate_id,
                        candidate["workspace_id"],
                        workspace["checkout_id"],
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE workspace_candidates SET state = 'publishing', updated_at = ?
                    WHERE candidate_id = ? AND state = 'staged'
                    """,
                    (timestamp, candidate_id),
                )
            elif str(publication["operation_state"]) == "rolled_back":
                connection.execute(
                    """
                    UPDATE workspace_publications SET operation_state = 'prepared',
                        updated_at = ? WHERE publication_id = ?
                    """,
                    (timestamp, publication["publication_id"]),
                )
                connection.execute(
                    """
                    UPDATE workspace_candidates SET state = 'publishing', updated_at = ?
                    WHERE candidate_id = ? AND state = 'staged'
                    """,
                    (timestamp, candidate_id),
                )
            candidate_row = connection.execute(
                "SELECT * FROM workspace_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            publication_row = connection.execute(
                "SELECT * FROM workspace_publications WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            assert candidate_row is not None and publication_row is not None
            return (
                self.workspace_candidate_from_row(candidate_row),
                self.workspace_publication_from_row(publication_row),
                self.workspace_from_row(workspace),
            )

    def confirm_workspace_publication(
        self, *, candidate_id: str, now: int | None = None
    ) -> tuple[
        WorkspaceCandidateRecord,
        WorkspacePublicationRecord,
        WorkspaceRecord,
        CheckoutEventRecord,
    ]:
        """Commit the result identity, revision, and replay event together."""

        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            candidate = connection.execute(
                "SELECT * FROM workspace_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            publication = connection.execute(
                "SELECT * FROM workspace_publications WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate is None or publication is None:
                raise KeyError(
                    f"candidate {candidate_id!r} does not have a publication"
                )
            workspace = connection.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?",
                (candidate["workspace_id"],),
            ).fetchone()
            assert workspace is not None
            if str(publication["operation_state"]) == "confirmed":
                event = connection.execute(
                    """
                    SELECT * FROM checkout_events
                    WHERE checkout_id = ? AND sequence = ?
                    """,
                    (
                        publication["checkout_id"],
                        publication["checkout_event_sequence"],
                    ),
                ).fetchone()
                assert event is not None
                return (
                    self.workspace_candidate_from_row(candidate),
                    self.workspace_publication_from_row(publication),
                    self.workspace_from_row(workspace),
                    self.checkout_event_from_row(event),
                )
            if (
                str(publication["operation_state"]) != "prepared"
                or str(candidate["state"]) != "publishing"
            ):
                raise ValueError(
                    "publication cannot be confirmed from its current state"
                )
            revision = int(workspace["workspace_revision"]) + 1
            updated = connection.execute(
                """
                UPDATE workspaces SET workspace_revision = ?, updated_at = ?
                WHERE workspace_id = ? AND workspace_revision = ?
                """,
                (revision, timestamp, workspace["workspace_id"], revision - 1),
            )
            if updated.rowcount != 1:
                raise ValueError("workspace revision changed concurrently")
            event = self.append_checkout_event(
                connection,
                checkout_id=str(workspace["checkout_id"]),
                event_type="workspace.change_published",
                caused_by_session_id=str(candidate["session_id"]),
                payload={
                    "candidate_id": candidate_id,
                    "path": str(candidate["relative_path"]),
                    "workspace_revision": revision,
                    "base_digest": str(candidate["base_digest"]),
                    "result_digest": str(candidate["result_digest"]),
                },
                now=timestamp,
            )
            connection.execute(
                """
                UPDATE workspace_candidates SET state = 'published',
                    published_at = ?, updated_at = ? WHERE candidate_id = ?
                """,
                (timestamp, timestamp, candidate_id),
            )
            connection.execute(
                """
                UPDATE workspace_publications SET operation_state = 'confirmed',
                    workspace_revision = ?, checkout_event_sequence = ?,
                    confirmed_at = ?, updated_at = ? WHERE candidate_id = ?
                """,
                (revision, event.sequence, timestamp, timestamp, candidate_id),
            )
            candidate_row = connection.execute(
                "SELECT * FROM workspace_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            publication_row = connection.execute(
                "SELECT * FROM workspace_publications WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            workspace_row = connection.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?",
                (workspace["workspace_id"],),
            ).fetchone()
            assert candidate_row is not None and publication_row is not None
            assert workspace_row is not None
            return (
                self.workspace_candidate_from_row(candidate_row),
                self.workspace_publication_from_row(publication_row),
                self.workspace_from_row(workspace_row),
                event,
            )

    def rollback_workspace_publication(
        self, *, candidate_id: str, now: int | None = None
    ) -> None:
        """Return a prepared journal to a retryable, retained candidate."""

        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            publication = connection.execute(
                "SELECT * FROM workspace_publications WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if publication is None or str(publication["operation_state"]) != "prepared":
                return
            connection.execute(
                """
                UPDATE workspace_publications SET operation_state = 'rolled_back',
                    updated_at = ? WHERE candidate_id = ?
                """,
                (timestamp, candidate_id),
            )
            connection.execute(
                """
                UPDATE workspace_candidates SET state = 'staged', updated_at = ?
                WHERE candidate_id = ? AND state = 'publishing'
                """,
                (timestamp, candidate_id),
            )

    def record_workspace_divergence(
        self,
        *,
        candidate_id: str,
        current: FileIdentity,
        now: int | None = None,
    ) -> tuple[WorkspaceDivergenceRecord, CheckoutEventRecord | None]:
        """Preserve stale candidate evidence instead of reporting a transient error."""

        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            candidate = connection.execute(
                "SELECT * FROM workspace_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate is None:
                raise KeyError(f"candidate {candidate_id!r} does not exist")
            existing = connection.execute(
                "SELECT * FROM workspace_divergences WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if existing is not None:
                return self.workspace_divergence_from_row(existing), None
            if str(candidate["state"]) == "published":
                raise ValueError("a published candidate cannot diverge")
            divergence_id = new_identifier("workspace_divergence")
            connection.execute(
                """
                INSERT INTO workspace_divergences(
                    divergence_id, candidate_id, workspace_id, session_id,
                    relative_path,
                    base_kind, base_mode, base_digest,
                    current_kind, current_mode, current_digest,
                    candidate_kind, candidate_mode, candidate_digest,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    divergence_id,
                    candidate_id,
                    candidate["workspace_id"],
                    candidate["session_id"],
                    candidate["relative_path"],
                    candidate["base_kind"],
                    candidate["base_mode"],
                    candidate["base_digest"],
                    str(current.kind),
                    current.mode,
                    current.digest,
                    candidate["result_kind"],
                    candidate["result_mode"],
                    candidate["result_digest"],
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE workspace_candidates SET state = 'diverged', diverged_at = ?,
                    updated_at = ? WHERE candidate_id = ?
                """,
                (timestamp, timestamp, candidate_id),
            )
            connection.execute(
                """
                UPDATE workspace_publications SET operation_state = 'diverged',
                    updated_at = ? WHERE candidate_id = ?
                    AND operation_state = 'prepared'
                """,
                (timestamp, candidate_id),
            )
            workspace = connection.execute(
                "SELECT checkout_id FROM workspaces WHERE workspace_id = ?",
                (candidate["workspace_id"],),
            ).fetchone()
            assert workspace is not None
            event = self.append_checkout_event(
                connection,
                checkout_id=str(workspace["checkout_id"]),
                event_type="workspace.candidate_diverged",
                caused_by_session_id=str(candidate["session_id"]),
                payload={
                    "candidate_id": candidate_id,
                    "path": str(candidate["relative_path"]),
                    "base_digest": str(candidate["base_digest"]),
                    "current_digest": current.digest,
                    "candidate_digest": str(candidate["result_digest"]),
                },
                now=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM workspace_divergences WHERE divergence_id = ?",
                (divergence_id,),
            ).fetchone()
            assert row is not None
            return self.workspace_divergence_from_row(row), event

    def list_recoverable_workspace_publications(
        self,
    ) -> tuple[WorkspacePublicationRecord, ...]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workspace_publications
                WHERE operation_state = 'prepared' ORDER BY created_at, publication_id
                """
            ).fetchall()
            return tuple(self.workspace_publication_from_row(row) for row in rows)

    @staticmethod
    def workspace_from_row(row: sqlite3.Row) -> WorkspaceRecord:
        return WorkspaceRecord(
            workspace_id=str(row["workspace_id"]),
            checkout_id=str(row["checkout_id"]),
            kind=str(row["kind"]),
            mode=str(row["mode"]),
            state=str(row["state"]),
            state_version=int(row["state_version"]),
            canonical_path=str(row["canonical_path"]),
            git_dir=str(row["git_dir"]) if row["git_dir"] is not None else None,
            workspace_epoch=int(row["workspace_epoch"]),
            workspace_revision=int(row["workspace_revision"]),
            local_generation=int(row["local_generation"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def workspace_candidate_from_row(row: sqlite3.Row) -> WorkspaceCandidateRecord:
        return WorkspaceCandidateRecord(
            candidate_id=str(row["candidate_id"]),
            workspace_id=str(row["workspace_id"]),
            session_id=str(row["session_id"]),
            relative_path=str(row["relative_path"]),
            generation=int(row["generation"]),
            base_kind=str(row["base_kind"]),
            base_mode=str(row["base_mode"]),
            base_digest=str(row["base_digest"]),
            result_kind=str(row["result_kind"]),
            result_mode=str(row["result_mode"]),
            result_digest=str(row["result_digest"]),
            content_hash=str(row["content_hash"]),
            byte_count=int(row["byte_count"]),
            state=str(row["state"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            published_at=(
                int(row["published_at"]) if row["published_at"] is not None else None
            ),
            diverged_at=(
                int(row["diverged_at"]) if row["diverged_at"] is not None else None
            ),
        )

    @staticmethod
    def workspace_publication_from_row(
        row: sqlite3.Row,
    ) -> WorkspacePublicationRecord:
        return WorkspacePublicationRecord(
            publication_id=str(row["publication_id"]),
            candidate_id=str(row["candidate_id"]),
            workspace_id=str(row["workspace_id"]),
            checkout_id=str(row["checkout_id"]),
            operation_state=str(row["operation_state"]),
            workspace_revision=(
                int(row["workspace_revision"])
                if row["workspace_revision"] is not None
                else None
            ),
            checkout_event_sequence=(
                int(row["checkout_event_sequence"])
                if row["checkout_event_sequence"] is not None
                else None
            ),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            confirmed_at=(
                int(row["confirmed_at"]) if row["confirmed_at"] is not None else None
            ),
        )

    @staticmethod
    def workspace_divergence_from_row(row: sqlite3.Row) -> WorkspaceDivergenceRecord:
        return WorkspaceDivergenceRecord(
            divergence_id=str(row["divergence_id"]),
            candidate_id=str(row["candidate_id"]),
            workspace_id=str(row["workspace_id"]),
            session_id=str(row["session_id"]),
            relative_path=str(row["relative_path"]),
            base_kind=str(row["base_kind"]),
            base_mode=str(row["base_mode"]),
            base_digest=str(row["base_digest"]),
            current_kind=str(row["current_kind"]),
            current_mode=str(row["current_mode"]),
            current_digest=str(row["current_digest"]),
            candidate_kind=str(row["candidate_kind"]),
            candidate_mode=str(row["candidate_mode"]),
            candidate_digest=str(row["candidate_digest"]),
            state=str(row["state"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def open_session(
        self,
        *,
        session_id: str,
        checkout_id: str,
        resume_token_hash: str,
        provider: str,
        model: str,
        workspace_id: str,
        workspace_mode: str = "shared",
        effort: str | None = None,
        agent_mode: str = "auto",
        now: int | None = None,
    ) -> tuple[SessionRecord, CheckoutEventRecord, int]:
        """Create one opening session and issue its immutable bootstrap cursor."""

        if workspace_mode not in {"shared", "isolated"}:
            raise ValueError("workspace mode must be shared or isolated")
        agent_mode = validate_agent_mode(agent_mode)
        if effort is not None and (
            not isinstance(effort, str) or effort not in EFFORT_LEVELS
        ):
            raise ValueError("session effort is invalid")

        if not session_id or not provider or not model:
            raise ValueError("session identity, provider, and model are required")
        if (
            len(provider) > 64
            or len(model) > 256
            or _REPO_KEY.fullmatch(resume_token_hash) is None
        ):
            raise ValueError("session identity is invalid")
        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            checkout = connection.execute(
                "SELECT 1 FROM checkouts WHERE checkout_id = ?", (checkout_id,)
            ).fetchone()
            if checkout is None:
                raise KeyError(f"checkout {checkout_id!r} does not exist")
            existing = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["checkout_id"]) != checkout_id
                    or str(existing["resume_token_hash"]) != resume_token_hash
                    or str(existing["provider"]) != provider
                    or str(existing["model"]) != model
                    or existing["effort"] != effort
                    or str(existing["workspace_id"]) != workspace_id
                    or str(existing["workspace_mode"]) != workspace_mode
                ):
                    raise ValueError("session ID is already bound to another session")
                cursor = connection.execute(
                    """
                    SELECT last_delivered_sequence FROM session_cursors
                    WHERE session_id = ? AND checkout_id = ?
                    """,
                    (session_id, checkout_id),
                ).fetchone()
                assert cursor is not None
                event = connection.execute(
                    """
                    SELECT * FROM checkout_events
                    WHERE checkout_id = ? AND sequence = ?
                    """,
                    (checkout_id, int(cursor["last_delivered_sequence"])),
                ).fetchone()
                if event is None:
                    raise ValueError("session bootstrap event is no longer available")
                return (
                    self.session_from_row(existing),
                    self.checkout_event_from_row(event),
                    int(cursor["last_delivered_sequence"]),
                )

            connection.execute(
                """
                INSERT INTO sessions(
                    session_id, checkout_id, workspace_id, workspace_mode,
                    resume_token_hash, provider, model, effort, agent_mode,
                    state, opened_at, last_heartbeat_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'opening', ?, ?, ?)
                """,
                (
                    session_id,
                    checkout_id,
                    workspace_id,
                    workspace_mode,
                    resume_token_hash,
                    provider,
                    model,
                    effort,
                    agent_mode,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO session_publication_policy(session_id,mode) VALUES (?,?)",
                (session_id, publication_mode(agent_mode)),
            )
            connection.execute(
                """
                INSERT INTO session_cursors(session_id, checkout_id, updated_at)
                VALUES (?, ?, ?)
                """,
                (session_id, checkout_id, timestamp),
            )
            event = self.append_checkout_event(
                connection,
                checkout_id=checkout_id,
                event_type="session.opened",
                caused_by_session_id=session_id,
                payload={
                    "session_id": session_id,
                    "provider": provider,
                    "model": model,
                    "effort": effort,
                    "agent_mode": agent_mode,
                },
                now=timestamp,
            )
            connection.execute(
                """
                UPDATE session_cursors SET last_delivered_sequence = ?, updated_at = ?
                WHERE session_id = ? AND checkout_id = ?
                """,
                (event.sequence, timestamp, session_id, checkout_id),
            )
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            assert row is not None
            return self.session_from_row(row), event, event.sequence

    def set_session_mode(self, session_id: str, agent_mode: str) -> SessionRecord:
        """Change future task policy only while this session has no live work."""
        agent_mode = validate_agent_mode(agent_mode)
        with self.connection() as connection, immediate_transaction(connection):
            session = connection.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if session is None:
                raise KeyError("session")
            if session["state"] != "active":
                raise ValueError("mode changes require an active session")
            busy = connection.execute(
                """SELECT t.task_id FROM tasks t LEFT JOIN claims c
                    ON c.claim_id=t.current_claim_id WHERE t.session_id=? AND (
                        t.state NOT IN ('completed','failed','cancelled',
                            'ready_for_integration','operator_attention','reviewing')
                        OR c.state IN ('queued','active_work','publishing',
                            'active_integration')) LIMIT 1""",
                (session_id,),
            ).fetchone()
            if busy is not None:
                raise ClaimConflict(
                    "finish or stop the current task before changing mode"
                )
            now = self._now(None)
            connection.execute(
                "UPDATE sessions SET agent_mode=?,updated_at=? WHERE session_id=?",
                (agent_mode, now, session_id),
            )
            connection.execute(
                """INSERT INTO session_publication_policy(session_id,mode) VALUES (?,?)
                    ON CONFLICT(session_id) DO UPDATE SET mode=excluded.mode""",
                (session_id, publication_mode(agent_mode)),
            )
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            assert row is not None
            return self.session_from_row(row)

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return self.session_from_row(row) if row is not None else None

    def get_session_cursor(self, session_id: str) -> SessionCursorRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM session_cursors WHERE session_id = ?", (session_id,)
            ).fetchone()
            return self.session_cursor_from_row(row) if row is not None else None

    def list_sessions(
        self, *, checkout_id: str | None = None, state: str | None = None
    ) -> tuple[SessionRecord, ...]:
        with self.connection() as connection:
            clauses: list[str] = []
            parameters: list[str] = []
            if checkout_id is not None:
                clauses.append("checkout_id = ?")
                parameters.append(checkout_id)
            if state is not None:
                clauses.append("state = ?")
                parameters.append(state)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                f"SELECT * FROM sessions{where} ORDER BY opened_at, session_id",
                parameters,
            ).fetchall()
            return tuple(self.session_from_row(row) for row in rows)

    def authenticate_session(
        self, session_id: str, resume_secret: str
    ) -> SessionRecord:
        """Verify a raw wrapper-held secret without ever returning its digest."""

        if not resume_secret or len(resume_secret) > 512:
            raise ValueError("session resume secret is invalid")
        expected = session_secret_hash(resume_secret)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None or not hmac.compare_digest(
                str(row["resume_token_hash"]), expected
            ):
                raise ValueError("session resume secret was rejected")
            return self.session_from_row(row)

    def acknowledge_session(
        self, session_id: str, *, sequence: int, now: int | None = None
    ) -> tuple[SessionRecord, SessionCursorRecord, CheckoutEventRecord | None]:
        """Advance a delivered checkout cursor and activate an opening session."""

        if sequence < 0:
            raise ValueError("session event sequence must be non-negative")
        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"session {session_id!r} does not exist")
            state = str(row["state"])
            if state not in {"opening", "active"}:
                raise ValueError("only an opening or active session can acknowledge")
            cursor = connection.execute(
                """
                SELECT * FROM session_cursors WHERE session_id = ? AND checkout_id = ?
                """,
                (session_id, row["checkout_id"]),
            ).fetchone()
            assert cursor is not None
            delivered = int(cursor["last_delivered_sequence"])
            received = int(cursor["transport_received_sequence"])
            if sequence < received or sequence > delivered:
                raise ValueError("session acknowledgement has an undelivered gap")
            connection.execute(
                """
                UPDATE session_cursors SET
                    transport_received_sequence = ?, context_consumed_sequence = ?,
                    updated_at = ?
                WHERE session_id = ? AND checkout_id = ?
                """,
                (sequence, sequence, timestamp, session_id, row["checkout_id"]),
            )
            event: CheckoutEventRecord | None = None
            if state == "opening":
                if sequence != delivered:
                    raise ValueError("opening session must acknowledge its bootstrap")
                connection.execute(
                    """
                    UPDATE sessions SET state = 'active',
                        state_version = state_version + 1,
                        last_heartbeat_at = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (timestamp, timestamp, session_id),
                )
                event = self.append_checkout_event(
                    connection,
                    checkout_id=str(row["checkout_id"]),
                    event_type="session.activated",
                    caused_by_session_id=session_id,
                    payload={"session_id": session_id},
                    now=timestamp,
                )
            updated = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            updated_cursor = connection.execute(
                "SELECT * FROM session_cursors WHERE session_id = ?", (session_id,)
            ).fetchone()
            assert updated is not None and updated_cursor is not None
            return (
                self.session_from_row(updated),
                self.session_cursor_from_row(updated_cursor),
                event,
            )

    def resume_session(
        self, session_id: str, *, now: int | None = None
    ) -> tuple[SessionRecord, CheckoutEventRecord | None]:
        """Restore a disconnected session; a closed session is intentionally final."""

        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"session {session_id!r} does not exist")
            state = str(row["state"])
            if state == "closed":
                raise ValueError("a closed session cannot be resumed")
            event: CheckoutEventRecord | None = None
            if state in {"disconnected", "stale"}:
                connection.execute(
                    """
                    UPDATE sessions SET state = 'active',
                        state_version = state_version + 1,
                        disconnected_at = NULL, last_heartbeat_at = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (timestamp, timestamp, session_id),
                )
                event = self.append_checkout_event(
                    connection,
                    checkout_id=str(row["checkout_id"]),
                    event_type="session.resumed",
                    caused_by_session_id=session_id,
                    payload={"session_id": session_id},
                    now=timestamp,
                )
            updated = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            assert updated is not None
            return self.session_from_row(updated), event

    def heartbeat_session(
        self, session_id: str, *, now: int | None = None
    ) -> SessionRecord:
        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            updated = connection.execute(
                """
                UPDATE sessions SET last_heartbeat_at = ?, updated_at = ?
                WHERE session_id = ? AND state = 'active'
                """,
                (timestamp, timestamp, session_id),
            )
            if updated.rowcount != 1:
                raise ValueError("only an active session can heartbeat")
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            assert row is not None
            return self.session_from_row(row)

    def close_session(
        self,
        session_id: str,
        *,
        reason: str = "operator_exit",
        now: int | None = None,
    ) -> tuple[SessionRecord, CheckoutEventRecord | None]:
        if not reason or len(reason) > 128:
            raise ValueError("session close reason is invalid")
        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"session {session_id!r} does not exist")
            if str(row["state"]) == "closed":
                return self.session_from_row(row), None
            cleared = connection.execute(
                """
                UPDATE session_intents SET state = 'cleared',
                    cleared_at = ?, updated_at = ?
                WHERE session_id = ? AND state = 'active'
                """,
                (timestamp, timestamp, session_id),
            ).rowcount
            connection.execute(
                """
                UPDATE sessions SET state = 'closed', state_version = state_version + 1,
                    closed_at = ?, close_reason = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (timestamp, reason, timestamp, session_id),
            )
            event = self.append_checkout_event(
                connection,
                checkout_id=str(row["checkout_id"]),
                event_type="session.closed",
                caused_by_session_id=session_id,
                payload={"session_id": session_id, "cleared_intents": cleared},
                now=timestamp,
            )
            updated = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            assert updated is not None
            return self.session_from_row(updated), event

    def disconnect_active_sessions(self, *, now: int | None = None) -> tuple[str, ...]:
        """Mark clients from a prior daemon boot disconnected before serving."""

        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            rows = connection.execute(
                "SELECT session_id, checkout_id FROM sessions WHERE state = 'active'"
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE sessions SET state = 'disconnected',
                        state_version = state_version + 1,
                        disconnected_at = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (timestamp, timestamp, row["session_id"]),
                )
                self.append_checkout_event(
                    connection,
                    checkout_id=str(row["checkout_id"]),
                    event_type="session.disconnected",
                    caused_by_session_id=str(row["session_id"]),
                    payload={
                        "session_id": str(row["session_id"]),
                        "reason": "daemon_restart",
                    },
                    now=timestamp,
                )
            return tuple(str(row["session_id"]) for row in rows)

    def list_session_events(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        now: int | None = None,
    ) -> tuple[CheckoutEventRecord, ...]:
        """Replay a durable checkout stream and remember the issued high-water."""

        if after_sequence < 0 or not 1 <= limit <= 500:
            raise ValueError("session event cursor or limit is invalid")
        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            session = connection.execute(
                "SELECT checkout_id FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise KeyError(f"session {session_id!r} does not exist")
            rows = connection.execute(
                """
                SELECT * FROM checkout_events
                WHERE checkout_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (session["checkout_id"], after_sequence, limit),
            ).fetchall()
            if rows:
                high_water = int(rows[-1]["sequence"])
                connection.execute(
                    """
                    UPDATE session_cursors SET last_delivered_sequence = MAX(
                        last_delivered_sequence, ?
                    ), updated_at = ? WHERE session_id = ? AND checkout_id = ?
                    """,
                    (high_water, timestamp, session_id, session["checkout_id"]),
                )
            return tuple(self.checkout_event_from_row(row) for row in rows)

    def set_session_intent(
        self,
        session_id: str,
        *,
        paths: tuple[str, ...],
        summary: str = "",
        now: int | None = None,
    ) -> tuple[SessionIntentRecord, CheckoutEventRecord, tuple[str, ...]]:
        """Replace one session's advisory intent and report overlapping sessions."""

        normalized = normalize_scopes(paths)
        if len(summary) > 1_000:
            raise ValueError("intent summary exceeds 1000 characters")
        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            session = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise KeyError(f"session {session_id!r} does not exist")
            if str(session["state"]) != "active":
                raise ValueError("only an active session can set an intent")
            checkout = connection.execute(
                "SELECT path_case_insensitive FROM checkouts WHERE checkout_id = ?",
                (session["checkout_id"],),
            ).fetchone()
            assert checkout is not None
            latest = connection.execute(
                """
                SELECT COALESCE(MAX(generation), 0) AS generation
                FROM session_intents WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            assert latest is not None
            connection.execute(
                """
                UPDATE session_intents SET state = 'cleared',
                    cleared_at = ?, updated_at = ?
                WHERE session_id = ? AND state = 'active'
                """,
                (timestamp, timestamp, session_id),
            )
            intent_id = new_identifier("intent")
            generation = int(latest["generation"]) + 1
            connection.execute(
                """
                INSERT INTO session_intents(
                    intent_id, session_id, generation, summary, state, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (intent_id, session_id, generation, summary, timestamp, timestamp),
            )
            connection.executemany(
                """
                INSERT INTO session_intent_paths(intent_id, ordinal, path)
                VALUES (?, ?, ?)
                """,
                [(intent_id, ordinal, path) for ordinal, path in enumerate(normalized)],
            )
            overlapping: set[str] = set()
            others = connection.execute(
                """
                SELECT intents.intent_id, intents.session_id
                FROM session_intents AS intents
                JOIN sessions AS other_session
                    ON other_session.session_id = intents.session_id
                WHERE intents.state = 'active' AND intents.session_id <> ?
                    AND other_session.checkout_id = ?
                """,
                (session_id, session["checkout_id"]),
            ).fetchall()
            for other in others:
                other_paths = connection.execute(
                    "SELECT path FROM session_intent_paths "
                    "WHERE intent_id = ? ORDER BY ordinal",
                    (other["intent_id"],),
                ).fetchall()
                if scope_sets_overlap(
                    normalized,
                    tuple(str(item["path"]) for item in other_paths),
                    case_insensitive_filesystem=bool(checkout["path_case_insensitive"]),
                ):
                    overlapping.add(str(other["session_id"]))
            event = self.append_checkout_event(
                connection,
                checkout_id=str(session["checkout_id"]),
                event_type="intent.set",
                caused_by_session_id=session_id,
                payload={
                    "intent_id": intent_id,
                    "session_id": session_id,
                    "paths": list(normalized),
                    "overlapping_session_ids": sorted(overlapping),
                },
                now=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM session_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            assert row is not None
            return (
                self.session_intent_from_row(connection, row),
                event,
                tuple(sorted(overlapping)),
            )

    def clear_session_intent(
        self, session_id: str, *, now: int | None = None
    ) -> tuple[SessionIntentRecord | None, CheckoutEventRecord | None]:
        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            session = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise KeyError(f"session {session_id!r} does not exist")
            row = connection.execute(
                """
                SELECT * FROM session_intents
                WHERE session_id = ? AND state = 'active'
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None, None
            connection.execute(
                """
                UPDATE session_intents SET state = 'cleared',
                    cleared_at = ?, updated_at = ?
                WHERE intent_id = ?
                """,
                (timestamp, timestamp, row["intent_id"]),
            )
            event = self.append_checkout_event(
                connection,
                checkout_id=str(session["checkout_id"]),
                event_type="intent.cleared",
                caused_by_session_id=session_id,
                payload={"intent_id": str(row["intent_id"]), "session_id": session_id},
                now=timestamp,
            )
            updated = connection.execute(
                "SELECT * FROM session_intents WHERE intent_id = ?", (row["intent_id"],)
            ).fetchone()
            assert updated is not None
            return self.session_intent_from_row(connection, updated), event

    def list_session_intents(
        self, *, checkout_id: str, active_only: bool = True
    ) -> tuple[SessionIntentRecord, ...]:
        with self.connection() as connection:
            state_clause = "AND intents.state = 'active'" if active_only else ""
            rows = connection.execute(
                f"""
                SELECT intents.* FROM session_intents AS intents
                JOIN sessions ON sessions.session_id = intents.session_id
                WHERE sessions.checkout_id = ? {state_clause}
                ORDER BY intents.created_at, intents.intent_id
                """,
                (checkout_id,),
            ).fetchall()
            return tuple(self.session_intent_from_row(connection, row) for row in rows)

    def session_conversation(
        self, session_id: str
    ) -> tuple[str, str, dict[str, object]] | None:
        """Return provider-bound prior conversation state for a new task turn."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT provider, model, conversation_json FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"session {session_id!r} does not exist")
            raw = row["conversation_json"]
            if raw is None:
                return None
            decoded = json.loads(str(raw))
            if not isinstance(decoded, dict) or not all(
                isinstance(key, str) for key in decoded
            ):
                raise ValueError("stored session conversation is malformed")
            return str(row["provider"]), str(row["model"]), decoded

    def save_session_conversation(
        self,
        session_id: str,
        *,
        provider: str,
        model: str,
        conversation: Mapping[str, object],
        now: int | None = None,
    ) -> SessionRecord:
        """Replace the last completed provider-native conversation snapshot."""

        try:
            encoded = json.dumps(
                dict(conversation), sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("session conversation must be JSON-safe") from exc
        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"session {session_id!r} does not exist")
            if str(row["provider"]) != provider or str(row["model"]) != model:
                raise ValueError(
                    "conversation provider/model does not match its session"
                )
            connection.execute(
                """
                UPDATE sessions SET conversation_json = ?,
                    conversation_revision = conversation_revision + 1,
                    updated_at = ? WHERE session_id = ?
                """,
                (encoded, timestamp, session_id),
            )
            updated = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            assert updated is not None
            return self.session_from_row(updated)

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            return self.task_from_row(row) if row is not None else None

    def list_tasks(self, repo_key: str | None = None) -> tuple[TaskRecord, ...]:
        with self.connection() as connection:
            if repo_key is None:
                rows = connection.execute(
                    "SELECT * FROM tasks ORDER BY created_at, task_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM tasks WHERE repo_key = ?
                    ORDER BY created_at, task_id
                    """,
                    (repo_key,),
                ).fetchall()
            return tuple(self.task_from_row(row) for row in rows)

    def list_task_events(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[TaskEventRecord, ...]:
        """Return a bounded durable event stream for one task."""

        if after_sequence < 0:
            raise ValueError("event sequence must be non-negative")
        if not 1 <= limit <= 500:
            raise ValueError("event limit must be between 1 and 500")
        with self.connection() as connection:
            task = connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise KeyError(f"task {task_id!r} does not exist")
            rows = connection.execute(
                """
                SELECT * FROM task_events
                WHERE task_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (task_id, after_sequence, limit),
            ).fetchall()
            return tuple(self.task_event_from_row(row) for row in rows)

    def record_task_event(
        self,
        *,
        task_id: str,
        claim_id: str | None,
        event_type: str,
        payload: Mapping[str, object],
        now: int | None = None,
    ) -> TaskEventRecord:
        """Append one session-visible lifecycle event in its own transaction."""

        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            exists = connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"task {task_id!r} does not exist")
            event_id = self.append_task_event(
                connection,
                task_id=task_id,
                claim_id=claim_id,
                event_type=event_type,
                payload=payload,
                now=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM task_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            assert row is not None
            return self.task_event_from_row(row)

    def get_claim(self, claim_id: str) -> ClaimRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM claims WHERE claim_id = ?", (claim_id,)
            ).fetchone()
            return self.claim_from_row(connection, row) if row is not None else None

    def list_claims(self, repo_key: str) -> tuple[ClaimRecord, ...]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM claims WHERE repo_key = ? ORDER BY queue_sequence",
                (repo_key,),
            ).fetchall()
            return tuple(self.claim_from_row(connection, row) for row in rows)

    def get_publication_intent(self, intent_id: str) -> PublicationIntentRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM publication_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                return None
            return self.publication_from_row(connection, row)

    def get_execution(self, task_id: str, attempt: int) -> ExecutionRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM task_executions
                WHERE task_id = ? AND task_attempt = ?
                """,
                (task_id, attempt),
            ).fetchone()
            return self.execution_from_row(row) if row is not None else None

    def save_execution_launch(
        self,
        *,
        task_id: str,
        attempt: int,
        driver: str,
        instructions: str,
        interactive: bool,
        parameters: Mapping[str, object],
        now: int | None = None,
    ) -> ExecutionLaunchRecord:
        """Store the immutable recipe that starts a task once it owns a claim.

        The row is written before the claim request. A daemon crash between the
        two operations can therefore leave at most an unused recipe, never an
        active claim with no way for the scheduler to start it.
        """

        if attempt <= 0 or not driver or len(driver) > 64:
            raise ValueError("execution launch identity is invalid")
        if not instructions.strip():
            raise ValueError("execution launch instructions must not be empty")
        try:
            encoded = json.dumps(
                dict(parameters), sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("execution launch parameters must be JSON-safe") from exc
        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            task = connection.execute(
                "SELECT attempt FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None or int(task["attempt"]) != attempt:
                raise ValueError("execution launch does not match the current task")
            existing = connection.execute(
                """
                SELECT * FROM task_execution_launches
                WHERE task_id = ? AND task_attempt = ?
                """,
                (task_id, attempt),
            ).fetchone()
            if existing is not None:
                stored = self.execution_launch_from_row(existing)
                if (
                    stored.driver != driver
                    or stored.instructions != instructions
                    or stored.interactive != interactive
                    or stored.parameters != dict(parameters)
                ):
                    raise ValueError(
                        "task attempt already has a different durable execution recipe"
                    )
                return stored
            connection.execute(
                """
                INSERT INTO task_execution_launches(
                    task_id, task_attempt, driver, instructions, interactive,
                    parameters_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    attempt,
                    driver,
                    instructions,
                    int(interactive),
                    encoded,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM task_execution_launches
                WHERE task_id = ? AND task_attempt = ?
                """,
                (task_id, attempt),
            ).fetchone()
            assert row is not None
            return self.execution_launch_from_row(row)

    def get_execution_launch(
        self, task_id: str, attempt: int
    ) -> ExecutionLaunchRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM task_execution_launches
                WHERE task_id = ? AND task_attempt = ?
                """,
                (task_id, attempt),
            ).fetchone()
            return self.execution_launch_from_row(row) if row is not None else None

    def list_execution_launches(self) -> tuple[ExecutionLaunchRecord, ...]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_execution_launches
                ORDER BY created_at, task_id, task_attempt
                """
            ).fetchall()
            return tuple(self.execution_launch_from_row(row) for row in rows)

    def get_execution_checkpoint(
        self, execution_id: str
    ) -> ExecutionCheckpointRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT c.*, a.execution_id IS NOT NULL AS terminal_event_persisted
                    FROM execution_checkpoints c LEFT JOIN execution_answers a
                    ON a.execution_id = c.execution_id WHERE c.execution_id = ?""",
                (execution_id,),
            ).fetchone()
            return self.execution_checkpoint_from_row(row) if row is not None else None

    def get_execution_answer(self, execution_id: str) -> ExecutionAnswerRecord | None:
        """Return the complete accepted answer, never a clipped task summary."""

        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_answers WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None:
                return None
            return ExecutionAnswerRecord(
                execution_id=str(row["execution_id"]),
                answer_id=str(row["answer_id"]),
                version=int(row["version"]),
                payload=json.loads(str(row["payload_json"])),
                content_sha256=str(row["content_sha256"]),
                first_event_sequence=int(row["first_event_sequence"]),
                last_event_sequence=int(row["last_event_sequence"]),
                created_at=int(row["created_at"]),
            )

    def save_execution_checkpoint(
        self,
        *,
        execution_id: str,
        driver: str,
        checkpoint: Mapping[str, object],
        now: int | None = None,
    ) -> ExecutionCheckpointRecord:
        """Save runtime state and any accepted terminal answer in one transaction.

        Legacy checkpoints remain opaque. A finished checkpoint may opt into
        durable answer delivery with an ``accepted_answer`` envelope. Its full
        answer and transcript projection are accepted exactly once, so a crash
        after this commit cannot lose or duplicate the final answer on replay.
        """

        if not driver or len(driver) > 64:
            raise ValueError("execution checkpoint driver name is invalid")
        try:
            encoded = json.dumps(
                dict(checkpoint), sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("execution checkpoint must be JSON-safe") from exc
        checkpoint_finalization(checkpoint)
        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            execution = connection.execute(
                "SELECT * FROM task_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if execution is None:
                raise KeyError(f"execution {execution_id!r} does not exist")
            if str(execution["driver"]) != driver:
                raise ValueError("checkpoint driver does not match its execution")
            prior_row = connection.execute(
                "SELECT checkpoint_json FROM execution_checkpoints "
                "WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            prior = (
                json.loads(str(prior_row["checkpoint_json"]))
                if prior_row is not None
                else None
            )
            if prior is not None and not isinstance(prior, dict):
                raise ValueError("stored execution checkpoint is malformed")
            validate_finalization_transition(prior, checkpoint)
            answer = checkpoint.get("accepted_answer")
            existing_answer = connection.execute(
                "SELECT 1 FROM execution_answers WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if answer is not None:
                if checkpoint.get("phase") != "finished":
                    raise ValueError(
                        "an accepted answer requires a finished checkpoint"
                    )
                self._persist_execution_answer(
                    connection,
                    execution=execution,
                    answer=answer,
                    checkpoint=checkpoint,
                    now=timestamp,
                )
            elif existing_answer is not None:
                raise ValueError("a checkpoint cannot discard its accepted answer")
            connection.execute(
                """
                INSERT INTO execution_checkpoints(
                    execution_id, driver, checkpoint_json, revision, created_at,
                    updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                    checkpoint_json = excluded.checkpoint_json,
                    revision = execution_checkpoints.revision + 1,
                    updated_at = excluded.updated_at
                """,
                (execution_id, driver, encoded, timestamp, timestamp),
            )
            row = connection.execute(
                """SELECT c.*, a.execution_id IS NOT NULL AS terminal_event_persisted
                    FROM execution_checkpoints c LEFT JOIN execution_answers a
                    ON a.execution_id = c.execution_id WHERE c.execution_id = ?""",
                (execution_id,),
            ).fetchone()
            assert row is not None
            return self.execution_checkpoint_from_row(row)

    def _persist_execution_answer(
        self,
        connection: sqlite3.Connection,
        *,
        execution: sqlite3.Row,
        answer: object,
        checkpoint: Mapping[str, object],
        now: int,
    ) -> None:
        payload = _accepted_answer(answer, checkpoint)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        execution_id = str(execution["execution_id"])
        existing = connection.execute(
            "SELECT payload_json FROM execution_answers WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        if existing is not None:
            if existing["payload_json"] != encoded:
                raise ValueError("an execution's accepted answer is immutable")
            return
        text = str(payload["answer"])
        chunks = max(1, (len(text) - 1) // _ANSWER_CHUNK_CHARACTERS + 1)
        sequences: list[int] = []
        for part in range(chunks):
            start = part * _ANSWER_CHUNK_CHARACTERS
            event_payload = {
                **payload,
                "answer": text[start : start + _ANSWER_CHUNK_CHARACTERS],
            }
            if chunks > 1:
                event_payload.update(part=part, final=part == chunks - 1)
            event_id = self.append_task_event(
                connection,
                task_id=str(execution["task_id"]),
                claim_id=str(execution["claim_id"]),
                event_type="model.finished",
                payload=event_payload,
                now=now,
            )
            row = connection.execute(
                "SELECT sequence FROM task_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            assert row is not None
            sequences.append(int(row["sequence"]))
        connection.execute(
            """INSERT INTO execution_answers(
                execution_id, answer_id, version, payload_json, content_sha256,
                first_event_sequence, last_event_sequence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                execution_id,
                payload["answer_id"],
                payload["version"],
                encoded,
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                sequences[0],
                sequences[-1],
                now,
            ),
        )

    @staticmethod
    def promote_shared_conversation(
        connection: sqlite3.Connection, execution_id: str, *, now: int
    ) -> None:
        """Promote only a finished conversation in the session's shared workspace."""

        ControlStore.promote_execution_conversation(
            connection, execution_id, now=now, shared_only=True
        )

    @staticmethod
    def promote_execution_conversation(
        connection: sqlite3.Connection,
        execution_id: str,
        *,
        now: int,
        shared_only: bool = False,
    ) -> None:
        """Save a finished session conversation in its settlement transaction.

        A new prompt can start as soon as the claim is released, so its prior
        conversation must be committed in that same transaction.
        """

        owner = connection.execute(
            """SELECT t.session_id FROM task_executions e
                JOIN tasks t ON t.task_id = e.task_id WHERE e.execution_id = ?""",
            (execution_id,),
        ).fetchone()
        if owner is not None and owner["session_id"] is None:
            return
        row = connection.execute(
            """SELECT c.checkpoint_json, s.session_id, s.provider, s.model
                FROM task_executions e JOIN tasks t ON t.task_id = e.task_id
                JOIN sessions s ON s.session_id = t.session_id
                JOIN execution_checkpoints c ON c.execution_id = e.execution_id
                WHERE e.execution_id = ?
                AND (? = 0 OR e.workspace_id = s.workspace_id)""",
            (execution_id, int(shared_only)),
        ).fetchone()
        state = json.loads(str(row["checkpoint_json"])) if row else {}
        if (
            row is None
            or not isinstance(state, dict)
            or state.get("phase") != "finished"
            or state.get("provider") != row["provider"]
            or state.get("model") != row["model"]
            or not isinstance(state.get("session"), dict)
        ):
            raise ValueError("completion has no matching finished conversation")
        conversation = {
            "version": 1,
            "provider": state["provider"],
            "model": state["model"],
            "session": state["session"],
            "coordination_sequence": state.get("coordination_sequence"),
        }
        connection.execute(
            """UPDATE sessions SET conversation_json = ?,
                conversation_revision = conversation_revision + 1, updated_at = ?
                WHERE session_id = ?""",
            (json.dumps(conversation, sort_keys=True), now, row["session_id"]),
        )

    def adopt_execution(
        self,
        execution_id: str,
        *,
        boot_id: str,
        now: int | None = None,
    ) -> ExecutionRecord:
        """Assign a recoverable running execution to this daemon boot."""

        if not boot_id:
            raise ValueError("execution owner boot must not be empty")
        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            updated = connection.execute(
                """
                UPDATE task_executions SET boot_id = ?, updated_at = ?
                WHERE execution_id = ? AND state = 'running'
                """,
                (boot_id, timestamp, execution_id),
            )
            if updated.rowcount != 1:
                raise ValueError("only a running execution can be adopted")
            row = connection.execute(
                "SELECT * FROM task_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            assert row is not None
            return self.execution_from_row(row)

    def list_orphaned_executions(self, boot_id: str) -> tuple[ExecutionRecord, ...]:
        """Return unsettled executions that no live daemon boot is advancing.

        An execution only ever moves forward inside the process that started
        it, so a row stamped with another boot -- or with none, from before the
        column existed -- is one whose worker is gone.  ``cleanup_failed`` is
        included because its publication is already confirmed and only the
        managed worktree still needs removing.
        """

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_executions
                WHERE state IN (
                    'preparing', 'worktree_ready', 'running', 'validated',
                    'publishing', 'cleanup_failed'
                ) AND (boot_id IS NULL OR boot_id <> ?)
                ORDER BY created_at, execution_id
                """,
                (boot_id,),
            ).fetchall()
            return tuple(self.execution_from_row(row) for row in rows)

    def latest_publication_intent(
        self, task_id: str, attempt: int
    ) -> PublicationIntentRecord | None:
        """Return the newest publication intent recorded for one task attempt."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM publication_intents
                WHERE task_id = ? AND task_attempt = ?
                ORDER BY started_at DESC, intent_id DESC LIMIT 1
                """,
                (task_id, attempt),
            ).fetchone()
            if row is None:
                return None
            return self.publication_from_row(connection, row)

    def list_open_publication_intents(self) -> tuple[PublicationIntentRecord, ...]:
        """Return intents whose Git side effect has no established outcome.

        ``operator_attention`` is included: it records a question recovery
        could not answer, not an answer, so a later pass with a readable
        repository may still be able to settle it.
        """

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM publication_intents
                WHERE operation_state IN (
                    'prepared', 'side_effect_unknown', 'operator_attention'
                )
                ORDER BY started_at, intent_id
                """
            ).fetchall()
            return tuple(self.publication_from_row(connection, row) for row in rows)

    def create_execution(
        self,
        *,
        task_id: str,
        attempt: int,
        claim_id: str,
        driver: str,
        worktree_path: str,
        base_oid: str,
        boot_id: str,
        workspace_id: str | None = None,
        now: int | None = None,
    ) -> ExecutionRecord:
        """Persist a planned local execution before creating its worktree.

        ``boot_id`` records which daemon process owns the row.  Recovery reads
        it to tell a live worker from one that died with an earlier process,
        so it is required rather than defaulted.
        """

        if attempt <= 0 or not worktree_path or not base_oid:
            raise ValueError("execution identity fields must not be empty")
        if not boot_id:
            raise ValueError("execution must record its owning daemon boot")
        # Which driver names exist is a registry question answered in the
        # execution layer; the store only refuses a shape the schema cannot
        # hold, so adding a driver never requires a migration.
        if not driver or len(driver) > 64:
            raise ValueError("execution driver name is not a valid identifier")
        timestamp = self._now(now)
        with self.connection() as connection, immediate_transaction(connection):
            task = connection.execute(
                "SELECT attempt FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None or int(task["attempt"]) != attempt:
                raise ValueError("execution does not match the current task attempt")
            existing = connection.execute(
                """
                SELECT * FROM task_executions
                WHERE task_id = ? AND task_attempt = ?
                """,
                (task_id, attempt),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["claim_id"]) != claim_id
                    or existing["workspace_id"] != workspace_id
                    or str(existing["driver"]) != driver
                ):
                    raise ValueError(
                        "task attempt already belongs to another execution"
                    )
                # Taking over a row left behind by an earlier boot transfers
                # ownership to this process, so recovery does not later treat a
                # running execution as an orphan of the dead one.
                connection.execute(
                    """
                    UPDATE task_executions SET boot_id = ?, updated_at = ?
                    WHERE execution_id = ? AND boot_id IS NOT ?
                    """,
                    (boot_id, timestamp, existing["execution_id"], boot_id),
                )
                refreshed = connection.execute(
                    "SELECT * FROM task_executions WHERE execution_id = ?",
                    (existing["execution_id"],),
                ).fetchone()
                assert refreshed is not None
                return self.execution_from_row(refreshed)
            execution_id = new_identifier("exec")
            connection.execute(
                """
                INSERT INTO task_executions(
                    execution_id, task_id, task_attempt, claim_id, driver, state,
                    boot_id, worktree_path, base_oid, created_at, updated_at,
                    workspace_id
                ) VALUES (?, ?, ?, ?, ?, 'preparing', ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    task_id,
                    attempt,
                    claim_id,
                    driver,
                    boot_id,
                    worktree_path,
                    base_oid,
                    timestamp,
                    timestamp,
                    workspace_id,
                ),
            )
            self.append_task_event(
                connection,
                task_id=task_id,
                claim_id=claim_id,
                event_type="execution.preparing",
                payload={"execution_id": execution_id, "driver": driver},
                now=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM task_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            assert row is not None
            return self.execution_from_row(row)

    def update_execution(
        self,
        execution_id: str,
        *,
        state: str,
        result_tree_id: str | None = None,
        result_commit_id: str | None = None,
        patch_hash: str | None = None,
        summary: str | None = None,
        tool_calls: int | None = None,
        usage: Mapping[str, int] | None = None,
        failure_code: str | None = None,
        now: int | None = None,
    ) -> ExecutionRecord:
        """Advance a persisted execution without making Git authority changes."""

        valid_states = {
            "preparing",
            "worktree_ready",
            "running",
            "validated",
            "publishing",
            "published",
            "cleanup_failed",
            "failed",
            "operator_attention",
        }
        if state not in valid_states:
            raise ValueError("unknown execution state")
        if patch_hash is not None and (
            len(patch_hash) != 64
            or any(character not in "0123456789abcdef" for character in patch_hash)
        ):
            raise ValueError("execution patch hash must be a lowercase SHA-256 digest")
        timestamp = self._now(now)
        finished_at = (
            timestamp if state in {"published", "cleanup_failed", "failed"} else None
        )
        with self.connection() as connection, immediate_transaction(connection):
            updated = connection.execute(
                """
                UPDATE task_executions SET
                    state = ?,
                    result_tree_id = COALESCE(?, result_tree_id),
                    result_commit_id = COALESCE(?, result_commit_id),
                    patch_hash = COALESCE(?, patch_hash),
                    summary = COALESCE(?, summary),
                    tool_calls = COALESCE(?, tool_calls),
                    usage_json = COALESCE(?, usage_json),
                    failure_code = COALESCE(?, failure_code),
                    started_at = CASE WHEN ? = 'running'
                        THEN COALESCE(started_at, ?) ELSE started_at END,
                    finished_at = COALESCE(?, finished_at),
                    updated_at = ?
                WHERE execution_id = ?
                """,
                (
                    state,
                    result_tree_id,
                    result_commit_id,
                    patch_hash,
                    summary,
                    tool_calls,
                    json.dumps(dict(usage), sort_keys=True) if usage else None,
                    failure_code,
                    state,
                    timestamp,
                    finished_at,
                    timestamp,
                    execution_id,
                ),
            )
            if updated.rowcount != 1:
                raise KeyError(f"execution {execution_id!r} does not exist")
            row = connection.execute(
                "SELECT * FROM task_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            assert row is not None
            self.append_task_event(
                connection,
                task_id=str(row["task_id"]),
                claim_id=str(row["claim_id"]),
                event_type=f"execution.{state}",
                payload={
                    "execution_id": execution_id,
                    "failure_code": failure_code,
                },
                now=timestamp,
            )
            return self.execution_from_row(row)

    @staticmethod
    def repository_from_row(row: sqlite3.Row) -> RepositoryRecord:
        return RepositoryRecord(
            repository_id=str(row["repository_id"]),
            repo_key=str(row["repo_key"]),
            profile_id=str(row["profile_id"]),
            display_name=str(row["display_name"]),
            git_common_dir=str(row["git_common_dir"]),
            main_worktree_path=str(row["main_worktree_path"]),
            remote_identity=(
                str(row["remote_identity"])
                if row["remote_identity"] is not None
                else None
            ),
            target_ref=str(row["target_ref"]),
            object_format=str(row["object_format"]),
            integration_adapter=str(row["integration_adapter"]),
            coordination_mode=str(row["coordination_mode"]),
            path_case_insensitive=bool(row["path_case_insensitive"]),
            coordinate_by_remote=bool(row["coordinate_by_remote"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def checkout_from_row(row: sqlite3.Row) -> CheckoutRecord:
        return CheckoutRecord(
            checkout_id=str(row["checkout_id"]),
            repository_id=str(row["repository_id"]),
            repo_key=str(row["repo_key"]),
            canonical_path=str(row["canonical_path"]),
            git_common_dir=str(row["git_common_dir"]),
            path_case_insensitive=bool(row["path_case_insensitive"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def session_from_row(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=str(row["session_id"]),
            checkout_id=str(row["checkout_id"]),
            workspace_id=(
                str(row["workspace_id"]) if row["workspace_id"] is not None else None
            ),
            workspace_mode=str(row["workspace_mode"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            effort=str(row["effort"]) if row["effort"] is not None else None,
            agent_mode=str(row["agent_mode"]),
            state=str(row["state"]),
            state_version=int(row["state_version"]),
            conversation_revision=int(row["conversation_revision"]),
            opened_at=int(row["opened_at"]),
            last_heartbeat_at=int(row["last_heartbeat_at"]),
            disconnected_at=(
                int(row["disconnected_at"])
                if row["disconnected_at"] is not None
                else None
            ),
            closed_at=(int(row["closed_at"]) if row["closed_at"] is not None else None),
            close_reason=(
                str(row["close_reason"]) if row["close_reason"] is not None else None
            ),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def session_cursor_from_row(row: sqlite3.Row) -> SessionCursorRecord:
        return SessionCursorRecord(
            session_id=str(row["session_id"]),
            checkout_id=str(row["checkout_id"]),
            transport_received_sequence=int(row["transport_received_sequence"]),
            context_consumed_sequence=int(row["context_consumed_sequence"]),
            last_delivered_sequence=int(row["last_delivered_sequence"]),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def checkout_event_from_row(row: sqlite3.Row) -> CheckoutEventRecord:
        decoded = json.loads(str(row["payload_json"]))
        if not isinstance(decoded, dict) or not all(
            isinstance(key, str) for key in decoded
        ):
            raise ValueError("stored checkout event payload is not an object")
        return CheckoutEventRecord(
            checkout_id=str(row["checkout_id"]),
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            event_type=str(row["event_type"]),
            caused_by_session_id=(
                str(row["caused_by_session_id"])
                if row["caused_by_session_id"] is not None
                else None
            ),
            payload=decoded,
            created_at=int(row["created_at"]),
        )

    @staticmethod
    def session_intent_from_row(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> SessionIntentRecord:
        paths = connection.execute(
            """
            SELECT path FROM session_intent_paths WHERE intent_id = ?
            ORDER BY ordinal
            """,
            (row["intent_id"],),
        ).fetchall()
        return SessionIntentRecord(
            intent_id=str(row["intent_id"]),
            session_id=str(row["session_id"]),
            generation=int(row["generation"]),
            summary=str(row["summary"]),
            state=str(row["state"]),
            paths=tuple(str(item["path"]) for item in paths),
            created_at=int(row["created_at"]),
            cleared_at=(
                int(row["cleared_at"]) if row["cleared_at"] is not None else None
            ),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def task_from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=str(row["task_id"]),
            repository_id=str(row["repository_id"]),
            repo_key=str(row["repo_key"]),
            title=str(row["title"]),
            state=str(row["state"]),
            coordination_mode=str(row["coordination_mode"]),
            coordination_state=str(row["coordination_state"]),
            attempt=int(row["attempt"]),
            current_claim_id=(
                str(row["current_claim_id"])
                if row["current_claim_id"] is not None
                else None
            ),
            current_fencing_token=(
                int(row["current_fencing_token"])
                if row["current_fencing_token"] is not None
                else None
            ),
            session_id=(
                str(row["session_id"]) if row["session_id"] is not None else None
            ),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def claim_from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> ClaimRecord:
        scope_rows = connection.execute(
            "SELECT value FROM claim_scopes WHERE claim_id = ? ORDER BY ordinal",
            (row["claim_id"],),
        ).fetchall()
        blocker_rows = connection.execute(
            """
            SELECT blocking_claim_id FROM claim_blockers
            WHERE waiting_claim_id = ? ORDER BY blocking_claim_id
            """,
            (row["claim_id"],),
        ).fetchall()
        return ClaimRecord(
            claim_id=str(row["claim_id"]),
            task_id=str(row["task_id"]),
            task_attempt=int(row["task_attempt"]),
            repo_key=str(row["repo_key"]),
            state=ClaimState(str(row["state"])),
            queue_sequence=int(row["queue_sequence"]),
            fencing_token=(
                int(row["fencing_token"]) if row["fencing_token"] is not None else None
            ),
            lease_expires_at=(
                int(row["lease_expires_at"])
                if row["lease_expires_at"] is not None
                else None
            ),
            scopes=tuple(str(item["value"]) for item in scope_rows),
            blocking_claim_ids=tuple(
                str(item["blocking_claim_id"]) for item in blocker_rows
            ),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            release_reason=(
                str(row["release_reason"])
                if row["release_reason"] is not None
                else None
            ),
            scheduling_mode=str(row["scheduling_mode"]),
            workspace_id=(
                str(row["workspace_id"]) if row["workspace_id"] is not None else None
            ),
        )

    @staticmethod
    def publication_from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> PublicationIntentRecord:
        paths = connection.execute(
            "SELECT path FROM publication_paths WHERE intent_id = ? ORDER BY ordinal",
            (row["intent_id"],),
        ).fetchall()
        return PublicationIntentRecord(
            intent_id=str(row["intent_id"]),
            idempotency_key=str(row["idempotency_key"]),
            claim_id=str(row["claim_id"]),
            task_id=str(row["task_id"]),
            task_attempt=int(row["task_attempt"]),
            repo_key=str(row["repo_key"]),
            fencing_token=int(row["fencing_token"]),
            patch_hash=str(row["patch_hash"]),
            result_tree_id=str(row["result_tree_id"]),
            result_commit_id=(
                str(row["result_commit_id"])
                if row["result_commit_id"] is not None
                else None
            ),
            expected_old_ref=(
                str(row["expected_old_ref"])
                if row["expected_old_ref"] is not None
                else None
            ),
            target_ref=str(row["target_ref"]),
            task_ref=str(row["task_ref"]) if row["task_ref"] is not None else None,
            operation_state=str(row["operation_state"]),
            changed_paths=tuple(str(item["path"]) for item in paths),
            started_at=int(row["started_at"]),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def execution_from_row(row: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord(
            execution_id=str(row["execution_id"]),
            task_id=str(row["task_id"]),
            task_attempt=int(row["task_attempt"]),
            claim_id=str(row["claim_id"]),
            driver=str(row["driver"]),
            state=str(row["state"]),
            boot_id=str(row["boot_id"]) if row["boot_id"] is not None else None,
            worktree_path=str(row["worktree_path"]),
            base_oid=str(row["base_oid"]),
            result_tree_id=(
                str(row["result_tree_id"])
                if row["result_tree_id"] is not None
                else None
            ),
            result_commit_id=(
                str(row["result_commit_id"])
                if row["result_commit_id"] is not None
                else None
            ),
            patch_hash=(
                str(row["patch_hash"]) if row["patch_hash"] is not None else None
            ),
            summary=str(row["summary"]) if row["summary"] is not None else None,
            tool_calls=int(row["tool_calls"]),
            usage=_decoded_usage(row["usage_json"]),
            failure_code=(
                str(row["failure_code"]) if row["failure_code"] is not None else None
            ),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            workspace_id=(
                str(row["workspace_id"]) if row["workspace_id"] is not None else None
            ),
        )

    @staticmethod
    def execution_launch_from_row(row: sqlite3.Row) -> ExecutionLaunchRecord:
        decoded = json.loads(str(row["parameters_json"]))
        if not isinstance(decoded, dict) or not all(
            isinstance(key, str) for key in decoded
        ):
            raise ValueError("stored execution launch parameters are not an object")
        return ExecutionLaunchRecord(
            task_id=str(row["task_id"]),
            task_attempt=int(row["task_attempt"]),
            driver=str(row["driver"]),
            instructions=str(row["instructions"]),
            interactive=bool(row["interactive"]),
            parameters=decoded,
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def execution_checkpoint_from_row(
        row: sqlite3.Row,
    ) -> ExecutionCheckpointRecord:
        decoded = json.loads(str(row["checkpoint_json"]))
        if not isinstance(decoded, dict) or not all(
            isinstance(key, str) for key in decoded
        ):
            raise ValueError("stored driver checkpoint is not an object")
        return ExecutionCheckpointRecord(
            execution_id=str(row["execution_id"]),
            driver=str(row["driver"]),
            checkpoint=decoded,
            revision=int(row["revision"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            terminal_event_persisted=bool(dict(row).get("terminal_event_persisted")),
        )

    @staticmethod
    def task_event_from_row(row: sqlite3.Row) -> TaskEventRecord:
        decoded = json.loads(str(row["payload_json"]))
        if not isinstance(decoded, dict) or not all(
            isinstance(key, str) for key in decoded
        ):
            raise ValueError("stored task event payload is not an object")
        return TaskEventRecord(
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            task_id=str(row["task_id"]),
            claim_id=str(row["claim_id"]) if row["claim_id"] is not None else None,
            event_type=str(row["event_type"]),
            payload=decoded,
            created_at=int(row["created_at"]),
        )

    @staticmethod
    def append_checkout_event(
        connection: sqlite3.Connection,
        *,
        checkout_id: str,
        event_type: str,
        caused_by_session_id: str | None,
        payload: Mapping[str, object],
        now: int,
    ) -> CheckoutEventRecord:
        """Allocate and append one gapless checkout-local event in this txn."""

        head = connection.execute(
            "SELECT next_event_sequence FROM checkout_heads WHERE checkout_id = ?",
            (checkout_id,),
        ).fetchone()
        if head is None:
            raise KeyError(f"checkout {checkout_id!r} does not exist")
        sequence = int(head["next_event_sequence"]) + 1
        updated = connection.execute(
            """
            UPDATE checkout_heads SET next_event_sequence = ?, updated_at = ?
            WHERE checkout_id = ? AND next_event_sequence = ?
            """,
            (sequence, now, checkout_id, sequence - 1),
        )
        if updated.rowcount != 1:
            raise ValueError("checkout event sequence changed concurrently")
        event_id = new_identifier("checkout_event")
        connection.execute(
            """
            INSERT INTO checkout_events(
                checkout_id, sequence, event_id, event_type, caused_by_session_id,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkout_id,
                sequence,
                event_id,
                event_type,
                caused_by_session_id,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM checkout_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        assert row is not None
        return ControlStore.checkout_event_from_row(row)

    @staticmethod
    def append_task_event(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        claim_id: str | None,
        event_type: str,
        payload: Mapping[str, object],
        now: int,
    ) -> str:
        event_id = new_identifier("event")
        connection.execute(
            """
            INSERT INTO task_events(
                event_id, task_id, claim_id, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                task_id,
                claim_id,
                event_type,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                now,
            ),
        )
        return event_id

    @staticmethod
    def enqueue_outbox(
        connection: sqlite3.Connection,
        *,
        idempotency_key: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, object],
        now: int,
    ) -> str:
        outbox_id = new_identifier("outbox")
        connection.execute(
            """
            INSERT INTO outbox(
                outbox_id, idempotency_key, event_type, aggregate_type,
                aggregate_id, payload_json, available_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                outbox_id,
                idempotency_key,
                event_type,
                aggregate_type,
                aggregate_id,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                now,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT outbox_id FROM outbox WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        assert row is not None
        return str(row["outbox_id"])

    def _now(self, supplied: int | None) -> int:
        if supplied is not None:
            if supplied < 0:
                raise ValueError("time must be non-negative Unix milliseconds")
            return int(supplied)
        return int(float(self.clock()) * 1_000)
