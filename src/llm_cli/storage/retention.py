"""Daemon-owned retention of disposable output, preserving recovery evidence."""

from __future__ import annotations

import json
import re

from llm_cli.agent.finalization import checkpoint_finalization
from llm_cli.storage.connection import immediate_transaction
from llm_cli.storage.control import ControlStore

_DAY_MS = 24 * 60 * 60 * 1_000
_AGE = re.compile(r"([1-9][0-9]{0,5})d")
_OUTPUT_EVENTS = (
    "model.text.delta",
    "model.answer.preview",
    "model.reasoning.delta",
    "model.tool.delta",
    "check.output",
    "command.output",
)

# Require affirmative terminal facts, across all attempts. A terminal-looking
# task is insufficient if a claim, check, proposal or publication is undecided.
_ELIGIBLE = """
SELECT t.task_id FROM tasks t
WHERE t.state IN ('completed','failed','cancelled')
  AND t.coordination_state IN ('unclaimed','released','expired','cancelled')
  AND t.finished_at IS NOT NULL AND t.finished_at < ? AND t.updated_at < ?
  AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.task_id=t.task_id
      AND c.state NOT IN ('released','expired','cancelled'))
  AND NOT EXISTS (SELECT 1 FROM task_executions e WHERE e.task_id=t.task_id
      AND e.state NOT IN ('published','failed'))
  AND NOT EXISTS (SELECT 1 FROM publication_intents p WHERE p.task_id=t.task_id
      AND p.operation_state NOT IN ('confirmed','failed_safe'))
  AND NOT EXISTS (SELECT 1 FROM task_workflows w WHERE w.task_id=t.task_id
      AND (w.status NOT IN ('published','undone','discarded')
          OR w.proposal_json IS NOT NULL))
  AND NOT EXISTS (SELECT 1 FROM check_runs c WHERE c.task_id=t.task_id
      AND c.state NOT IN ('passed','failed','cancelled','error','source_mutated'))
  AND NOT EXISTS (SELECT 1 FROM task_executions e JOIN workspace_batches b
      ON b.batch_id=e.execution_id WHERE e.task_id=t.task_id
      AND b.state <> 'published')
ORDER BY t.task_id
"""


def retention_age_ms(value: str) -> int:
    """Parse a bounded positive day count, such as 30d."""

    match = _AGE.fullmatch(value)
    if match is None:
        raise ValueError("older-than must be a positive number of days, such as 30d")
    return int(match[1]) * _DAY_MS


def prune_task_output(
    store: ControlStore, *, older_than: str, apply: bool = False
) -> dict[str, object]:
    """Count or atomically prune disposable old output, then compact SQLite.

    The authority transaction rechecks eligibility while blocking concurrent
    writers. VACUUM runs after commit, outside any transaction; SQLite provides
    its own locking and crash-safe replacement. A crash between those steps
    leaves committed pruning with compaction still safe to repeat.
    """

    now = store._now(None)
    cutoff = now - retention_age_ms(older_than)
    placeholders = ",".join("?" for _ in _OUTPUT_EVENTS)
    predicate = (
        "task_id IN (SELECT task_id FROM prune_tasks) AND created_at < ? "
        f"AND event_type IN ({placeholders})"
    )
    with store.connection() as connection:
        with immediate_transaction(connection):
            connection.execute(
                "CREATE TEMP TABLE prune_tasks(task_id TEXT PRIMARY KEY)"
            )
            for row in connection.execute(_ELIGIBLE, (cutoff, cutoff)).fetchall():
                task_id = str(row[0])
                checkpoints = connection.execute(
                    "SELECT c.checkpoint_json FROM execution_checkpoints c "
                    "JOIN task_executions e ON e.execution_id=c.execution_id "
                    "WHERE e.task_id=?",
                    (task_id,),
                )
                if all(_checkpoint_finished(str(item[0])) for item in checkpoints):
                    connection.execute("INSERT INTO prune_tasks VALUES (?)", (task_id,))
            eligible = connection.execute(
                "SELECT COUNT(*) FROM prune_tasks"
            ).fetchone()[0]
            events, event_bytes = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(CAST(payload_json AS BLOB))),0) "
                f"FROM task_events WHERE {predicate}",
                (cutoff, *_OUTPUT_EVENTS),
            ).fetchone()
            checks, check_bytes = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(CAST(output AS BLOB))),0) "
                "FROM check_runs WHERE task_id IN (SELECT task_id FROM prune_tasks) "
                "AND created_at < ? AND output <> ''",
                (cutoff,),
            ).fetchone()
            if apply:
                # An audit marker records the removal without advancing task
                # timestamps or changing event cursors / AUTOINCREMENT identity.
                for row in connection.execute(
                    "SELECT task_id FROM prune_tasks"
                ).fetchall():
                    task_id = str(row[0])
                    event_count = connection.execute(
                        f"SELECT COUNT(*) FROM task_events WHERE {predicate} "
                        "AND task_id=?",
                        (cutoff, *_OUTPUT_EVENTS, task_id),
                    ).fetchone()[0]
                    check_count = connection.execute(
                        "SELECT COUNT(*) FROM check_runs WHERE task_id=? "
                        "AND created_at < ? AND output <> ''",
                        (task_id, cutoff),
                    ).fetchone()[0]
                    if event_count or check_count:
                        store.append_task_event(
                            connection,
                            task_id=task_id,
                            claim_id=None,
                            event_type="task.output_pruned",
                            payload={
                                "cutoff": cutoff,
                                "event_chunks": event_count,
                                "check_outputs": check_count,
                            },
                            now=now,
                        )
                connection.execute(
                    f"DELETE FROM task_events WHERE {predicate}",
                    (cutoff, *_OUTPUT_EVENTS),
                )
                connection.execute(
                    "UPDATE check_runs SET output='',truncated=1 "
                    "WHERE task_id IN (SELECT task_id FROM prune_tasks) "
                    "AND created_at < ? AND output <> ''",
                    (cutoff,),
                )
        if apply:
            connection.execute("VACUUM")
    return {
        "dry_run": not apply,
        "cutoff": cutoff,
        "eligible_tasks": eligible,
        "event_chunks": events,
        "event_payload_bytes": event_bytes,
        "check_outputs": checks,
        "check_output_bytes": check_bytes,
        "vacuumed": apply,
    }


def _checkpoint_finished(raw: str) -> bool:
    try:
        checkpoint = json.loads(raw)
    except (ValueError, TypeError):
        return False
    if not isinstance(checkpoint, dict) or checkpoint.get("phase") != "finished":
        return False
    try:
        finalization = checkpoint_finalization(checkpoint)
    except ValueError:
        return False
    return finalization is None or finalization.status in {"completed", "interrupted"}
