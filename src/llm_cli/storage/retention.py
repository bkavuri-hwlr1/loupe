"""Daemon-owned retention of disposable output, preserving recovery evidence.

Old settled tasks keep their records, answers, summaries and check results.
Pruning removes streamed and tool output, and slims execution checkpoints to
what is still read once a task has settled: the provider conversation was
already saved to its session, and a published batch's file contents live in
the checkout.
"""

from __future__ import annotations

import json
import re
from collections import Counter

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
    "model.tool_result",
    "check.output",
    "command.output",
)

# Require affirmative terminal facts, across all attempts. A terminal-looking
# task is insufficient if a claim, check, held proposal or publication is
# undecided. A published proposal kept for undo needs neither output nor
# checkpoints, and a diverged batch is a settled refusal.
_ELIGIBLE = """
SELECT t.task_id FROM tasks t
WHERE t.state IN ('completed','failed','cancelled')
  AND t.coordination_state IN ('unclaimed','released','expired','cancelled')
  AND t.finished_at IS NOT NULL AND t.finished_at < ? AND t.updated_at < ?
  AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.task_id=t.task_id
      AND c.state NOT IN ('released','expired','cancelled'))
  AND NOT EXISTS (SELECT 1 FROM all_task_executions e WHERE e.task_id=t.task_id
      AND e.state NOT IN ('published','failed'))
  AND NOT EXISTS (SELECT 1 FROM publication_intents p WHERE p.task_id=t.task_id
      AND p.operation_state NOT IN ('confirmed','failed_safe'))
  AND NOT EXISTS (SELECT 1 FROM task_workflows w WHERE w.task_id=t.task_id
      AND (w.status='awaiting_review' OR (w.proposal_json IS NOT NULL
          AND w.status NOT IN ('published','undone','discarded'))))
  AND NOT EXISTS (SELECT 1 FROM check_runs c WHERE c.task_id=t.task_id
      AND c.state NOT IN ('passed','failed','cancelled','error','source_mutated'))
  AND NOT EXISTS (SELECT 1 FROM all_task_executions e JOIN workspace_batches b
      ON b.batch_id=e.execution_id WHERE e.task_id=t.task_id
      AND b.state NOT IN ('published','diverged'))
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
            slims: list[tuple[str, str, str, str]] = []
            checkpoint_bytes = 0
            for row in connection.execute(_ELIGIBLE, (cutoff, cutoff)).fetchall():
                task_id = str(row[0])
                checkpoints = connection.execute(
                    "SELECT e.execution_id,c.checkpoint_json,e.state,"
                    "e.execution_contract FROM all_task_executions e "
                    "LEFT JOIN all_execution_checkpoints c "
                    "ON c.execution_id=e.execution_id WHERE e.task_id=?",
                    (task_id,),
                ).fetchall()
                if any(
                    raw is None or contract not in {"legacy", "workspace_v1"}
                    for _, raw, _, contract in checkpoints
                ):
                    continue
                parsed = [
                    (str(execution_id), str(raw), str(state), str(contract))
                    for execution_id, raw, state, contract in checkpoints
                ]
                settled = [_settled(raw, state) for _, raw, state, _ in parsed]
                if any(checkpoint is None for checkpoint in settled):
                    continue
                connection.execute("INSERT INTO prune_tasks VALUES (?)", (task_id,))
                for (execution_id, raw, state, contract), checkpoint in zip(
                    parsed, settled, strict=True
                ):
                    assert checkpoint is not None
                    slim = _slim(checkpoint, state)
                    if slim is not None:
                        checkpoint_bytes += len(raw.encode()) - len(slim.encode())
                        table = (
                            "workspace_execution_checkpoints"
                            if contract == "workspace_v1"
                            else "execution_checkpoints"
                        )
                        slims.append((task_id, execution_id, slim, table))
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
                slimmed = Counter(task_id for task_id, _, _, _ in slims)
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
                    checkpoint_count = slimmed[task_id]
                    if event_count or check_count or checkpoint_count:
                        store.append_task_event(
                            connection,
                            task_id=task_id,
                            claim_id=None,
                            event_type="task.output_pruned",
                            payload={
                                "cutoff": cutoff,
                                "event_chunks": event_count,
                                "check_outputs": check_count,
                                "checkpoints": checkpoint_count,
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
                for table in (
                    "execution_checkpoints",
                    "workspace_execution_checkpoints",
                ):
                    connection.executemany(
                        f"UPDATE {table} SET checkpoint_json=? WHERE execution_id=?",
                        [
                            (slim, execution_id)
                            for _, execution_id, slim, target in slims
                            if target == table
                        ],
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
        "checkpoints": len(slims),
        "checkpoint_bytes": checkpoint_bytes,
        "vacuumed": apply,
    }


_UNSETTLED_PHASES = {"awaiting_settlement", "finalization_pending", "finalizing"}


def _settled(raw: str, execution_state: str) -> dict[str, object] | None:
    """A checkpoint nothing can resume or finalize again, or None."""

    try:
        checkpoint = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(checkpoint, dict):
        return None
    phase = checkpoint.get("phase")
    if not isinstance(phase, str) or phase in _UNSETTLED_PHASES:
        return None
    try:
        finalization = checkpoint_finalization(checkpoint)
    except ValueError:
        return None
    if finalization is not None and finalization.status not in {
        "completed",
        "interrupted",
    }:
        return None
    # A turn interrupted mid-way cannot resume once its execution has failed.
    if phase != "finished" and execution_state != "failed":
        return None
    return checkpoint


def _slim(checkpoint: dict[str, object], execution_state: str) -> str | None:
    """The settled checkpoint without data nothing reads again, or None."""

    if checkpoint.get("phase") == "finished":
        finalization = checkpoint_finalization(checkpoint)
        if finalization is not None and finalization.status == "interrupted":
            return None  # /retry-answer rewrites it from this conversation.
    slim = dict(checkpoint)
    # Settlement saved a finished conversation to its session; an interrupted
    # turn's conversation is never resumed after its execution failed.
    changed = slim.pop("session", None) is not None
    usage = slim.get("tool_usage")
    if (
        execution_state == "published"
        and isinstance(usage, dict)
        and "shared_workspace_state" in usage
    ):
        # Published file contents are in the checkout. A failed execution
        # keeps them: task diff shows what it had pending.
        slim["tool_usage"] = {
            key: value
            for key, value in usage.items()
            if key != "shared_workspace_state"
        }
        changed = True
    return json.dumps(slim, separators=(",", ":")) if changed else None
