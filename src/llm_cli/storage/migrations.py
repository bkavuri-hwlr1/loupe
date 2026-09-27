"""Numbered, checksummed SQLite schema migrations."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from typing import Final

from llm_cli.storage.connection import immediate_transaction


class MigrationError(RuntimeError):
    """The on-disk schema cannot safely be migrated by this build."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


_MIGRATION_TABLE_SQL: Final = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL,
    checksum TEXT NOT NULL CHECK (length(checksum) = 64),
    applied_at INTEGER NOT NULL CHECK (applied_at >= 0)
)
"""


CONTROL_MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        1,
        "repositories_tasks_and_heads",
        """
CREATE TABLE repositories (
    repository_id TEXT PRIMARY KEY,
    repo_key TEXT NOT NULL UNIQUE CHECK (length(repo_key) = 64),
    profile_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    git_common_dir TEXT NOT NULL,
    main_worktree_path TEXT NOT NULL,
    remote_identity TEXT,
    target_ref TEXT NOT NULL,
    object_format TEXT NOT NULL DEFAULT 'sha1'
        CHECK (object_format IN ('sha1', 'sha256')),
    integration_adapter TEXT NOT NULL DEFAULT 'local',
    coordination_mode TEXT NOT NULL DEFAULT 'enforce'
        CHECK (coordination_mode IN ('off', 'observe', 'enforce')),
    index_mode TEXT NOT NULL DEFAULT 'manual',
    trust_mode TEXT NOT NULL DEFAULT 'local',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL
);

CREATE INDEX repositories_common_dir_idx
    ON repositories(git_common_dir);
CREATE INDEX repositories_remote_target_idx
    ON repositories(remote_identity, target_ref);

CREATE TABLE repository_heads (
    repo_key TEXT PRIMARY KEY
        REFERENCES repositories(repo_key) ON DELETE RESTRICT,
    next_queue_sequence INTEGER NOT NULL DEFAULT 0
        CHECK (next_queue_sequence >= 0),
    next_fencing_token INTEGER NOT NULL DEFAULT 0
        CHECK (next_fencing_token >= 0),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    last_effective_time INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL
        REFERENCES repositories(repository_id) ON DELETE RESTRICT,
    repo_key TEXT NOT NULL
        REFERENCES repository_heads(repo_key) ON DELETE RESTRICT,
    parent_task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    group_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'created' CHECK (state IN (
        'created', 'planning', 'waiting_for_repository', 'queued',
        'preparing', 'running', 'awaiting_clarification', 'reviewing',
        'ready_for_integration', 'integrating', 'integration_pending',
        'operator_attention', 'completed', 'failed', 'cancelled'
    )),
    coordination_mode TEXT NOT NULL DEFAULT 'enforce'
        CHECK (coordination_mode IN ('off', 'observe', 'enforce')),
    coordination_state TEXT NOT NULL DEFAULT 'unclaimed' CHECK (
        coordination_state IN (
            'unclaimed', 'queued', 'active_work', 'publishing',
            'active_integration', 'released', 'expired', 'cancelled'
        )
    ),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
    current_claim_id TEXT,
    current_fencing_token INTEGER,
    base_revision TEXT,
    result_revision TEXT,
    failure_code TEXT,
    created_at INTEGER NOT NULL,
    queued_at INTEGER,
    started_at INTEGER,
    finished_at INTEGER,
    updated_at INTEGER NOT NULL
);

CREATE INDEX tasks_state_created_idx ON tasks(state, created_at);
CREATE INDEX tasks_repo_state_idx ON tasks(repo_key, state);
CREATE INDEX tasks_parent_idx ON tasks(parent_task_id, created_at);
CREATE INDEX tasks_group_idx ON tasks(group_id, created_at);
""",
    ),
    Migration(
        2,
        "root_claims_scopes_and_blockers",
        """
CREATE TABLE claims (
    claim_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    task_attempt INTEGER NOT NULL CHECK (task_attempt > 0),
    repo_key TEXT NOT NULL
        REFERENCES repository_heads(repo_key) ON DELETE RESTRICT,
    kind TEXT NOT NULL DEFAULT 'root' CHECK (kind = 'root'),
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'active_work', 'publishing', 'active_integration',
        'released', 'expired', 'cancelled'
    )),
    queue_sequence INTEGER NOT NULL CHECK (queue_sequence > 0),
    fencing_token INTEGER CHECK (fencing_token > 0),
    lease_expires_at INTEGER,
    scope_version INTEGER NOT NULL DEFAULT 1 CHECK (scope_version > 0),
    base_revision TEXT,
    publication_patch_hash TEXT,
    publication_tree_id TEXT,
    result_revision TEXT,
    integration_adapter TEXT,
    integration_identity TEXT,
    previous_task_id TEXT,
    release_reason TEXT,
    terminal_expires_at INTEGER,
    created_at INTEGER NOT NULL,
    activated_at INTEGER,
    publication_started_at INTEGER,
    integration_started_at INTEGER,
    released_at INTEGER,
    expired_at INTEGER,
    updated_at INTEGER NOT NULL,
    UNIQUE(repo_key, task_id, task_attempt),
    CHECK (
        (state = 'queued' AND fencing_token IS NULL
            AND lease_expires_at IS NULL AND terminal_expires_at IS NULL)
        OR
        (state = 'active_work' AND fencing_token IS NOT NULL
            AND lease_expires_at IS NOT NULL AND terminal_expires_at IS NULL)
        OR
        (state IN ('publishing', 'active_integration')
            AND fencing_token IS NOT NULL AND lease_expires_at IS NULL
            AND terminal_expires_at IS NULL)
        OR
        (state IN ('released', 'expired', 'cancelled')
            AND lease_expires_at IS NULL AND terminal_expires_at IS NOT NULL)
    )
);

CREATE INDEX claims_repo_queue_idx
    ON claims(repo_key, state, queue_sequence);
CREATE INDEX claims_expiry_idx ON claims(state, lease_expires_at);
CREATE INDEX claims_task_state_idx ON claims(task_id, state);
CREATE INDEX claims_integration_idx
    ON claims(repo_key, integration_adapter, integration_identity, state);
CREATE INDEX claims_terminal_retention_idx
    ON claims(terminal_expires_at);

CREATE TABLE claim_scopes (
    claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    scope_type TEXT NOT NULL DEFAULT 'path' CHECK (scope_type = 'path'),
    value TEXT NOT NULL,
    is_directory INTEGER NOT NULL CHECK (is_directory IN (0, 1)),
    source TEXT NOT NULL DEFAULT 'user'
        CHECK (source IN ('user', 'planner', 'fallback', 'policy')),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    PRIMARY KEY (claim_id, ordinal),
    UNIQUE (claim_id, scope_type, value)
);

CREATE INDEX claim_scopes_value_idx
    ON claim_scopes(scope_type, value, claim_id);

CREATE TABLE claim_blockers (
    waiting_claim_id TEXT NOT NULL
        REFERENCES claims(claim_id) ON DELETE CASCADE,
    blocking_claim_id TEXT NOT NULL
        REFERENCES claims(claim_id) ON DELETE CASCADE,
    observed_revision INTEGER NOT NULL CHECK (observed_revision >= 0),
    created_at INTEGER NOT NULL,
    PRIMARY KEY (waiting_claim_id, blocking_claim_id),
    CHECK (waiting_claim_id <> blocking_claim_id)
);

CREATE INDEX claim_blockers_blocking_idx
    ON claim_blockers(blocking_claim_id, waiting_claim_id);
""",
    ),
    Migration(
        3,
        "task_events_and_outbox",
        """
CREATE TABLE task_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    claim_id TEXT REFERENCES claims(claim_id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

CREATE INDEX task_events_task_sequence_idx
    ON task_events(task_id, sequence);

CREATE TABLE outbox (
    outbox_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'leased', 'processed', 'failed')),
    available_at INTEGER NOT NULL,
    lease_id TEXT,
    lease_expires_at INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at INTEGER NOT NULL,
    processed_at INTEGER,
    updated_at INTEGER NOT NULL,
    CHECK (
        (state = 'leased' AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR state <> 'leased'
    )
);

CREATE INDEX outbox_ready_idx ON outbox(state, available_at, outbox_id);
CREATE INDEX outbox_lease_idx ON outbox(state, lease_expires_at);
""",
    ),
    Migration(
        4,
        "publication_intents",
        """
CREATE TABLE publication_intents (
    intent_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) = 64),
    claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    task_attempt INTEGER NOT NULL CHECK (task_attempt > 0),
    repo_key TEXT NOT NULL
        REFERENCES repository_heads(repo_key) ON DELETE RESTRICT,
    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
    patch_hash TEXT NOT NULL CHECK (length(patch_hash) = 64),
    result_tree_id TEXT NOT NULL,
    result_commit_id TEXT,
    strategy TEXT NOT NULL DEFAULT 'branch',
    adapter TEXT NOT NULL DEFAULT 'local',
    expected_old_ref TEXT,
    target_ref TEXT NOT NULL,
    task_ref TEXT,
    operation_state TEXT NOT NULL DEFAULT 'prepared' CHECK (
        operation_state IN (
            'prepared', 'side_effect_unknown', 'confirmed',
            'failed_safe', 'operator_attention'
        )
    ),
    result_hash TEXT,
    started_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    confirmed_at INTEGER,
    UNIQUE (claim_id, result_tree_id)
);

CREATE INDEX publication_intents_state_idx
    ON publication_intents(operation_state, updated_at);
CREATE INDEX publication_intents_repo_idx
    ON publication_intents(repo_key, started_at);

CREATE TABLE publication_paths (
    intent_id TEXT NOT NULL
        REFERENCES publication_intents(intent_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    path TEXT NOT NULL,
    PRIMARY KEY (intent_id, ordinal),
    UNIQUE (intent_id, path)
);
""",
    ),
    Migration(
        5,
        "durable_task_executions",
        """
CREATE TABLE task_executions (
    execution_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    task_attempt INTEGER NOT NULL CHECK (task_attempt > 0),
    claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    driver TEXT NOT NULL CHECK (driver IN ('fixture_write')),
    state TEXT NOT NULL CHECK (state IN (
        'preparing', 'worktree_ready', 'running', 'validated', 'publishing',
        'published', 'cleanup_failed', 'failed', 'operator_attention'
    )),
    worktree_path TEXT NOT NULL,
    base_oid TEXT NOT NULL,
    result_tree_id TEXT,
    result_commit_id TEXT,
    patch_hash TEXT CHECK (patch_hash IS NULL OR length(patch_hash) = 64),
    failure_code TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    updated_at INTEGER NOT NULL,
    UNIQUE(task_id, task_attempt)
);

CREATE INDEX task_executions_claim_idx ON task_executions(claim_id);
CREATE INDEX task_executions_state_idx ON task_executions(state, updated_at);
""",
    ),
    Migration(
        6,
        "repository_identity_facts",
        """
-- Rows carried over from an earlier schema were never probed, so they default
-- to case-insensitive.  Over-contending only serializes work that could have
-- run in parallel; under-contending hands two claims the same physical path.
-- A subsequent 'repo add' replaces this with Git's actual answer.
ALTER TABLE repositories ADD COLUMN path_case_insensitive INTEGER NOT NULL
    DEFAULT 1 CHECK (path_case_insensitive IN (0, 1));

ALTER TABLE repositories ADD COLUMN coordinate_by_remote INTEGER NOT NULL
    DEFAULT 1 CHECK (coordinate_by_remote IN (0, 1));
""",
    ),
    Migration(
        7,
        "execution_boot_ownership",
        """
-- Only the daemon boot that started an execution ever advances it, so a row
-- carrying a different boot's identity has no owner: its worker died with the
-- process.  Rows written before this column existed are indistinguishable from
-- that case and are recovered the same way, which is correct because they too
-- predate the current boot.
ALTER TABLE task_executions ADD COLUMN boot_id TEXT;

CREATE INDEX task_executions_boot_idx ON task_executions(boot_id, state);
""",
    ),
    Migration(
        8,
        "pluggable_execution_drivers",
        """
-- SQLite cannot alter a CHECK constraint, so widening the driver column means
-- rebuilding the table.  The replacement constrains the column's *shape* only:
-- which driver names are real is a registry question, and answering it in the
-- schema would mean a migration every time a driver is added.
CREATE TABLE task_executions_rebuilt (
    execution_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    task_attempt INTEGER NOT NULL CHECK (task_attempt > 0),
    claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE RESTRICT,
    driver TEXT NOT NULL CHECK (length(driver) BETWEEN 1 AND 64),
    state TEXT NOT NULL CHECK (state IN (
        'preparing', 'worktree_ready', 'running', 'validated', 'publishing',
        'published', 'cleanup_failed', 'failed', 'operator_attention'
    )),
    boot_id TEXT,
    worktree_path TEXT NOT NULL,
    base_oid TEXT NOT NULL,
    result_tree_id TEXT,
    result_commit_id TEXT,
    patch_hash TEXT CHECK (patch_hash IS NULL OR length(patch_hash) = 64),
    summary TEXT,
    tool_calls INTEGER NOT NULL DEFAULT 0 CHECK (tool_calls >= 0),
    usage_json TEXT,
    failure_code TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    updated_at INTEGER NOT NULL,
    UNIQUE(task_id, task_attempt)
);

INSERT INTO task_executions_rebuilt(
    execution_id, task_id, task_attempt, claim_id, driver, state, boot_id,
    worktree_path, base_oid, result_tree_id, result_commit_id, patch_hash,
    failure_code, created_at, started_at, finished_at, updated_at
) SELECT
    execution_id, task_id, task_attempt, claim_id, driver, state, boot_id,
    worktree_path, base_oid, result_tree_id, result_commit_id, patch_hash,
    failure_code, created_at, started_at, finished_at, updated_at
FROM task_executions;

DROP TABLE task_executions;

ALTER TABLE task_executions_rebuilt RENAME TO task_executions;

CREATE INDEX task_executions_claim_idx ON task_executions(claim_id);
CREATE INDEX task_executions_state_idx ON task_executions(state, updated_at);
CREATE INDEX task_executions_boot_idx ON task_executions(boot_id, state);
""",
    ),
    Migration(
        9,
        "durable_launches_and_execution_checkpoints",
        """
-- A queued claim has to carry enough immutable intent for the daemon to start
-- it later without asking the CLI session to submit the task again.  The
-- parameters are driver-owned, JSON-safe data; the task title remains the
-- user-visible summary, not the execution protocol.
CREATE TABLE task_execution_launches (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    task_attempt INTEGER NOT NULL CHECK (task_attempt > 0),
    driver TEXT NOT NULL CHECK (length(driver) BETWEEN 1 AND 64),
    instructions TEXT NOT NULL,
    interactive INTEGER NOT NULL DEFAULT 0 CHECK (interactive IN (0, 1)),
    parameters_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (task_id, task_attempt)
);

CREATE INDEX task_execution_launches_driver_idx
    ON task_execution_launches(driver, created_at);

-- Provider-native session history and neutral harness progress stay opaque to
-- the coordination layer. The driver checkpoints after every response and
-- every tool result, allowing a new daemon boot to continue the exact turn
-- without replaying completed tools. The control database is private local
-- state, so this deliberately retains prompt and tool-result content needed
-- for replay.
CREATE TABLE execution_checkpoints (
    execution_id TEXT PRIMARY KEY
        REFERENCES task_executions(execution_id) ON DELETE CASCADE,
    driver TEXT NOT NULL CHECK (length(driver) BETWEEN 1 AND 64),
    checkpoint_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX execution_checkpoints_driver_idx
    ON execution_checkpoints(driver, updated_at);
""",
    ),
    Migration(
        10,
        "durable_sessions_checkout_events_and_intents",
        """
-- ``repositories`` remains the legacy coordination identity for now.  A
-- checkout is deliberately an additive physical attachment to that identity:
-- two local clones may share a logical repository row but must never share a
-- session event stream merely because they have the same remote.
CREATE TABLE checkouts (
    checkout_id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL
        REFERENCES repositories(repository_id) ON DELETE RESTRICT,
    repo_key TEXT NOT NULL
        REFERENCES repository_heads(repo_key) ON DELETE RESTRICT,
    canonical_path TEXT NOT NULL,
    git_common_dir TEXT NOT NULL,
    path_case_insensitive INTEGER NOT NULL CHECK (path_case_insensitive IN (0, 1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(repository_id, canonical_path)
);

CREATE INDEX checkouts_repo_path_idx
    ON checkouts(repo_key, canonical_path);

CREATE TABLE checkout_heads (
    checkout_id TEXT PRIMARY KEY
        REFERENCES checkouts(checkout_id) ON DELETE RESTRICT,
    next_event_sequence INTEGER NOT NULL DEFAULT 0
        CHECK (next_event_sequence >= 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE checkout_events (
    checkout_id TEXT NOT NULL
        REFERENCES checkouts(checkout_id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 96),
    caused_by_session_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    PRIMARY KEY (checkout_id, sequence)
);

CREATE INDEX checkout_events_session_idx
    ON checkout_events(checkout_id, caused_by_session_id, sequence);

-- The raw resume secret is created and retained only by the CLI wrapper in an
-- owner-only profile file.  The daemon keeps its SHA-256 digest solely to
-- authenticate an explicit resume or an editing task submitted by that
-- session.
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    checkout_id TEXT NOT NULL
        REFERENCES checkouts(checkout_id) ON DELETE RESTRICT,
    resume_token_hash TEXT NOT NULL UNIQUE CHECK (length(resume_token_hash) = 64),
    provider TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 256),
    state TEXT NOT NULL CHECK (state IN (
        'opening', 'active', 'disconnected', 'stale', 'closed'
    )),
    state_version INTEGER NOT NULL DEFAULT 1 CHECK (state_version > 0),
    conversation_json TEXT,
    conversation_revision INTEGER NOT NULL DEFAULT 0
        CHECK (conversation_revision >= 0),
    opened_at INTEGER NOT NULL,
    last_heartbeat_at INTEGER NOT NULL,
    disconnected_at INTEGER,
    closed_at INTEGER,
    close_reason TEXT,
    updated_at INTEGER NOT NULL
);

CREATE INDEX sessions_checkout_state_idx
    ON sessions(checkout_id, state, opened_at);

CREATE TABLE session_cursors (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    checkout_id TEXT NOT NULL REFERENCES checkouts(checkout_id) ON DELETE RESTRICT,
    transport_received_sequence INTEGER NOT NULL DEFAULT 0
        CHECK (transport_received_sequence >= 0),
    context_consumed_sequence INTEGER NOT NULL DEFAULT 0
        CHECK (context_consumed_sequence >= 0),
    last_delivered_sequence INTEGER NOT NULL DEFAULT 0
        CHECK (last_delivered_sequence >= 0),
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, checkout_id),
    CHECK (context_consumed_sequence <= transport_received_sequence),
    CHECK (transport_received_sequence <= last_delivered_sequence)
);

-- Intents are advisory, never a replacement for the legacy claim authority.
-- Keeping their paths normalized through the same algebra still lets sessions
-- receive useful, correct overlap warnings while the optimistic change-batch
-- publication path is built.
CREATE TABLE session_intents (
    intent_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation > 0),
    summary TEXT NOT NULL DEFAULT '' CHECK (length(summary) <= 1000),
    state TEXT NOT NULL CHECK (state IN ('active', 'cleared')),
    created_at INTEGER NOT NULL,
    cleared_at INTEGER,
    updated_at INTEGER NOT NULL,
    UNIQUE(session_id, generation)
);

CREATE UNIQUE INDEX session_intents_one_active_idx
    ON session_intents(session_id) WHERE state = 'active';
CREATE INDEX session_intents_active_idx
    ON session_intents(state, updated_at);

CREATE TABLE session_intent_paths (
    intent_id TEXT NOT NULL
        REFERENCES session_intents(intent_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    path TEXT NOT NULL,
    PRIMARY KEY (intent_id, ordinal),
    UNIQUE(intent_id, path)
);

CREATE INDEX session_intent_paths_path_idx
    ON session_intent_paths(path, intent_id);

-- Existing tasks remain valid legacy work. New chat-submitted tasks can carry
-- their durable conversation owner without changing the claim lifecycle.
ALTER TABLE tasks ADD COLUMN session_id TEXT REFERENCES sessions(session_id);
CREATE INDEX tasks_session_created_idx ON tasks(session_id, created_at);
""",
    ),
    Migration(
        11,
        "workspace_records_and_modes",
        """
-- A workspace is the physical place a session's edits are prepared.  Shared
-- mode prepares them outside the visible checkout and publishes into it;
-- isolated mode uses a linked worktree.  The mode is durable on the session as
-- well as the workspace because changing a default must never retroactively
-- move a session that is already running under the old one.
CREATE TABLE workspaces (
    workspace_id TEXT PRIMARY KEY,
    checkout_id TEXT NOT NULL
        REFERENCES checkouts(checkout_id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (kind IN ('shared_checkout', 'linked_worktree')),
    mode TEXT NOT NULL CHECK (mode IN ('shared', 'isolated')),
    state TEXT NOT NULL CHECK (state IN (
        'creating', 'active', 'command_running', 'syncing', 'switching',
        'cleaning', 'quarantined', 'removed'
    )),
    state_version INTEGER NOT NULL DEFAULT 1 CHECK (state_version > 0),
    canonical_path TEXT NOT NULL,
    git_dir TEXT,
    source_workspace_id TEXT
        REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
    -- The epoch changes on a structural operation that invalidates incremental
    -- assumptions; the revision orders committed batches within an epoch. The
    -- counters are independent and neither may be inferred from the other.
    workspace_epoch INTEGER NOT NULL DEFAULT 1 CHECK (workspace_epoch > 0),
    workspace_revision INTEGER NOT NULL DEFAULT 0
        CHECK (workspace_revision >= 0),
    local_generation INTEGER NOT NULL DEFAULT 0 CHECK (local_generation >= 0),
    incoming_sequence INTEGER NOT NULL DEFAULT 0 CHECK (incoming_sequence >= 0),
    quarantine_reason TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_verified_at INTEGER
);

-- One shared workspace per checkout, and no two live workspaces at one path.
CREATE UNIQUE INDEX workspaces_shared_unique_idx
    ON workspaces(checkout_id) WHERE kind = 'shared_checkout';
CREATE UNIQUE INDEX workspaces_live_path_idx
    ON workspaces(canonical_path) WHERE state <> 'removed';

ALTER TABLE sessions ADD COLUMN workspace_id TEXT
    REFERENCES workspaces(workspace_id) ON DELETE RESTRICT;
ALTER TABLE sessions ADD COLUMN workspace_mode TEXT NOT NULL DEFAULT 'shared'
    CHECK (workspace_mode IN ('shared', 'isolated'));
""",
    ),
    Migration(
        12,
        "shared_workspace_candidates_and_publication_journal",
        """
-- The first shared-workspace writer deliberately handles one complete regular
-- file at a time.  Candidate bytes live in daemon-private, content-addressed
-- storage; this ledger contains only identities and ownership metadata.
CREATE TABLE workspace_read_observations (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
    relative_path TEXT NOT NULL,
    observed_kind TEXT NOT NULL CHECK (observed_kind IN (
        'absent', 'regular', 'symlink', 'directory_marker', 'gitlink'
    )),
    observed_mode TEXT NOT NULL,
    observed_digest TEXT NOT NULL CHECK (length(observed_digest) = 64),
    observed_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, relative_path)
);

CREATE INDEX workspace_read_observations_workspace_idx
    ON workspace_read_observations(workspace_id, relative_path);

CREATE TABLE workspace_candidates (
    candidate_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    relative_path TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation > 0),
    base_kind TEXT NOT NULL CHECK (base_kind IN (
        'absent', 'regular', 'symlink', 'directory_marker', 'gitlink'
    )),
    base_mode TEXT NOT NULL,
    base_digest TEXT NOT NULL CHECK (length(base_digest) = 64),
    result_kind TEXT NOT NULL CHECK (result_kind = 'regular'),
    result_mode TEXT NOT NULL CHECK (result_mode IN ('100644', '100755')),
    result_digest TEXT NOT NULL CHECK (length(result_digest) = 64),
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
    state TEXT NOT NULL CHECK (state IN (
        'staged', 'publishing', 'published', 'diverged', 'abandoned'
    )),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    published_at INTEGER,
    diverged_at INTEGER,
    UNIQUE(session_id, relative_path, generation)
);

CREATE INDEX workspace_candidates_workspace_state_idx
    ON workspace_candidates(workspace_id, state, created_at);
CREATE INDEX workspace_candidates_session_path_idx
    ON workspace_candidates(session_id, relative_path, generation);

-- A prepared record is the write-ahead boundary between durable candidate
-- metadata and the atomic filesystem replacement.  Recovery compares the
-- live path with the recorded base/result identities; it never guesses from
-- elapsed time or deletes a candidate to make a journal disappear.
CREATE TABLE workspace_publications (
    publication_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE
        REFERENCES workspace_candidates(candidate_id) ON DELETE RESTRICT,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
    checkout_id TEXT NOT NULL REFERENCES checkouts(checkout_id) ON DELETE RESTRICT,
    operation_state TEXT NOT NULL CHECK (operation_state IN (
        'prepared', 'confirmed', 'rolled_back', 'diverged', 'operator_attention'
    )),
    workspace_revision INTEGER CHECK (workspace_revision >= 0),
    checkout_event_sequence INTEGER CHECK (checkout_event_sequence > 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    confirmed_at INTEGER,
    CHECK (
        (operation_state = 'confirmed' AND workspace_revision IS NOT NULL
            AND checkout_event_sequence IS NOT NULL AND confirmed_at IS NOT NULL)
        OR operation_state <> 'confirmed'
    )
);

CREATE INDEX workspace_publications_recovery_idx
    ON workspace_publications(operation_state, updated_at);

CREATE TABLE workspace_divergences (
    divergence_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE
        REFERENCES workspace_candidates(candidate_id) ON DELETE RESTRICT,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    relative_path TEXT NOT NULL,
    base_kind TEXT NOT NULL,
    base_mode TEXT NOT NULL,
    base_digest TEXT NOT NULL CHECK (length(base_digest) = 64),
    current_kind TEXT NOT NULL,
    current_mode TEXT NOT NULL,
    current_digest TEXT NOT NULL CHECK (length(current_digest) = 64),
    candidate_kind TEXT NOT NULL,
    candidate_mode TEXT NOT NULL,
    candidate_digest TEXT NOT NULL CHECK (length(candidate_digest) = 64),
    state TEXT NOT NULL DEFAULT 'open' CHECK (state = 'open'),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX workspace_divergences_workspace_state_idx
    ON workspace_divergences(workspace_id, state, created_at);
""",
    ),
    Migration(
        13,
        "durable_shared_batches_and_execution_workspaces",
        """
ALTER TABLE task_executions ADD COLUMN workspace_id TEXT
    REFERENCES workspaces(workspace_id) ON DELETE RESTRICT;

-- Candidate bodies are fsynced in private content-addressed storage before
-- this journal becomes visible. request_json retains every base/result
-- identity and the immutable request; observations_json retains divergences.
CREATE TABLE workspace_batches (
    batch_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
    checkout_id TEXT NOT NULL REFERENCES checkouts(checkout_id) ON DELETE RESTRICT,
    canonical_path TEXT NOT NULL,
    git_common_dir TEXT NOT NULL,
    request_json TEXT NOT NULL,
    observations_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL CHECK (state IN (
        'applying', 'published', 'diverged', 'operator_attention'
    )),
    workspace_revision INTEGER CHECK (workspace_revision >= 0),
    checkout_event_sequence INTEGER CHECK (checkout_event_sequence > 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    CHECK ((state = 'published' AND workspace_revision IS NOT NULL
        AND checkout_event_sequence IS NOT NULL) OR state <> 'published')
);

CREATE UNIQUE INDEX workspace_batches_one_applying_idx
    ON workspace_batches(workspace_id)
    WHERE state IN ('applying', 'operator_attention');
CREATE INDEX workspace_batches_recovery_idx
    ON workspace_batches(state, created_at);
""",
    ),
    Migration(
        14,
        "optimistic_shared_claim_scheduling",
        """
-- Existing claims keep their original exclusive reservation, even if their
-- session now supports shared execution. Only new claims can opt into private
-- preparation with checked shared-batch publication, bound to one workspace.
ALTER TABLE claims ADD COLUMN workspace_id TEXT
    REFERENCES workspaces(workspace_id) ON DELETE RESTRICT;
ALTER TABLE claims ADD COLUMN scheduling_mode TEXT NOT NULL DEFAULT 'exclusive'
    CHECK (scheduling_mode IN ('exclusive', 'optimistic')
        AND (scheduling_mode = 'exclusive' OR workspace_id IS NOT NULL));
""",
    ),
    Migration(
        15,
        "durable_session_effort",
        """
-- Omission preserves the provider's default for existing conversations.
ALTER TABLE sessions ADD COLUMN effort TEXT
    CHECK (effort IS NULL OR effort IN (
        'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'
    ));
""",
    ),
    Migration(
        16,
        "verified_task_workflows",
        """
CREATE TABLE check_configurations (
    checkout_id TEXT PRIMARY KEY REFERENCES checkouts(checkout_id),
    config_json TEXT NOT NULL
);
CREATE TABLE session_publication_policy (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    mode TEXT NOT NULL CHECK(mode IN ('auto','review'))
);
CREATE TABLE task_workflows (
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    attempt INTEGER NOT NULL,
    execution_id TEXT,
    publish_mode TEXT NOT NULL DEFAULT 'auto',
    config_json TEXT NOT NULL DEFAULT '{}',
    proposal_json TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stop_requested INTEGER NOT NULL DEFAULT 0,
    publication_id TEXT,
    action_task_id TEXT,
    undo_task_id TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    PRIMARY KEY(task_id, attempt)
);
CREATE TABLE check_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    candidate_digest TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    exit_code INTEGER,
    duration REAL NOT NULL DEFAULT 0,
    output TEXT NOT NULL DEFAULT '',
    truncated INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(task_id, attempt) REFERENCES task_workflows(task_id, attempt)
);
CREATE INDEX check_runs_task_idx ON check_runs(task_id, attempt, created_at);
""",
    ),
    Migration(
        17,
        "session_agent_modes",
        """
ALTER TABLE sessions ADD COLUMN agent_mode TEXT NOT NULL DEFAULT 'auto'
    CHECK(agent_mode IN ('plan','normal','auto'));
UPDATE sessions SET agent_mode='normal' WHERE session_id IN (
    SELECT session_id FROM session_publication_policy WHERE mode='review'
);
ALTER TABLE task_workflows ADD COLUMN agent_mode TEXT NOT NULL DEFAULT 'auto'
    CHECK(agent_mode IN ('plan','normal','auto'));
UPDATE task_workflows SET agent_mode='normal' WHERE publish_mode='review';
""",
    ),
    Migration(
        18,
        "durable_execution_answers",
        """
-- The answer and its replay events are committed with the finished checkpoint.
-- One attempt accepts one immutable answer, independently of later publication.
CREATE TABLE execution_answers (
    execution_id TEXT PRIMARY KEY
        REFERENCES task_executions(execution_id) ON DELETE CASCADE,
    answer_id TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL CHECK (version = 1),
    payload_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    first_event_sequence INTEGER NOT NULL CHECK (first_event_sequence > 0),
    last_event_sequence INTEGER NOT NULL
        CHECK (last_event_sequence >= first_event_sequence),
    created_at INTEGER NOT NULL
);
""",
    ),
)


KNOWLEDGE_MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        1,
        "knowledge_store_bootstrap",
        """
CREATE TABLE store_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
""",
    ),
)


VECTOR_MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        1,
        "vector_store_bootstrap",
        """
CREATE TABLE store_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
""",
    ),
)


def _utc_timestamp_ms() -> int:
    return time.time_ns() // 1_000_000


def _iter_statements(sql: str) -> tuple[str, ...]:
    statements: list[str] = []
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                statements.append(statement)
            pending = ""
    if pending.strip():
        raise MigrationError("migration ends with an incomplete SQL statement")
    return tuple(statements)


def _validate_migration_set(migrations: tuple[Migration, ...]) -> None:
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(migrations) + 1)):
        raise MigrationError("migrations must be contiguous and start at version 1")
    if len({migration.name for migration in migrations}) != len(migrations):
        raise MigrationError("migration names must be unique")


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: tuple[Migration, ...] = CONTROL_MIGRATIONS,
) -> int:
    """Verify and transactionally apply all pending migrations.

    Applied SQL is immutable: changing the name or checksum of a numbered
    migration is rejected before any newer migration runs.
    """

    _validate_migration_set(migrations)
    with immediate_transaction(connection):
        connection.execute(_MIGRATION_TABLE_SQL)

    applied_rows = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    known = {migration.version: migration for migration in migrations}
    for row in applied_rows:
        version = int(row["version"])
        migration = known.get(version)
        if migration is None:
            raise MigrationError(
                f"database schema version {version} is newer than this build"
            )
        if row["name"] != migration.name or row["checksum"] != migration.checksum:
            raise MigrationError(f"migration {version} checksum does not match")

    applied_versions = {int(row["version"]) for row in applied_rows}
    for migration in migrations:
        if migration.version in applied_versions:
            continue
        with immediate_transaction(connection):
            for statement in _iter_statements(migration.sql):
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    _utc_timestamp_ms(),
                ),
            )
    return migrations[-1].version if migrations else 0


def current_schema_version(connection: sqlite3.Connection) -> int:
    """Return the applied version, or zero for an uninitialized database."""

    exists = connection.execute(
        """
        SELECT 1 FROM sqlite_schema
        WHERE type = 'table' AND name = 'schema_migrations'
        """
    ).fetchone()
    if exists is None:
        return 0
    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
    ).fetchone()
    return int(row["version"])
