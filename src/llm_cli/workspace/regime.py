"""Explicit, journaled conversion of one physical checkout's execution contract."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.workspace.authority import Mutation, PhysicalAuthority, PhysicalCheckout

if TYPE_CHECKING:
    from llm_cli.storage.control import ControlStore

# Relevant retained legacy work. The reviewed checkpoint additionally binds
# source, index, HEAD and mappings, but only at apply, before anything is durable.
_HISTORY_KEYS = (
    "claims",
    "tasks",
    "executions",
    "publications",
    "workflows",
    "batches",
    "workspace_publications",
    "authority_receipts",
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _error(code: ErrorCode, message: str) -> LlmCoordError:
    return LlmCoordError(code, message)


def _source_checkpoint(physical: PhysicalCheckout) -> str:
    """One bounded stable snapshot; no source or individual digests are exported."""
    from llm_cli.agent.source_policy import contains_secret_material, excluded_paths
    from llm_cli.workspace.external import metadata
    from llm_cli.workspace.isolated import _passive, _read, _source, _source_paths

    deadline = time.monotonic() + 30.0

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise _error(
                ErrorCode.CUTOVER_BLOCKED,
                "cutover source checkpoint exceeded its time budget",
            )
        return value

    root = physical.root
    try:
        paths = _source_paths(root)
        excluded = excluded_paths(root, list(paths), remaining=remaining)
        index = _passive(root, ["ls-files", "--stage", "-v", "-z"])
        before = {path: metadata(root, path) for path in paths if path not in excluded}
        if any(stamp == "unavailable" for stamp in before.values()):
            raise ValueError("source observation unavailable")
        records: list[tuple[str, str, str | None]] = []
        total = 0
        for path, stamp in before.items():
            remaining()
            digest: str | None = None
            if stamp != "absent":
                mode, size = json.loads(stamp)[2:4]
                if stat.S_ISREG(mode):
                    if size > 16 * 1024 * 1024 or total + size > 64 * 1024 * 1024:
                        raise ValueError("source checkpoint exceeds byte budget")
                    with _source(root, path) as descriptor:
                        body = _read(descriptor, 16 * 1024 * 1024)
                    total += len(body)
                    if not contains_secret_material(body):
                        digest = hashlib.sha256(body).hexdigest()
            records.append((path, stamp, digest))
        remaining()
        if (
            paths != _source_paths(root)
            or excluded != excluded_paths(root, list(paths), remaining=remaining)
            or index != _passive(root, ["ls-files", "--stage", "-v", "-z"])
            or any(metadata(root, path) != stamp for path, stamp in before.items())
        ):
            raise ValueError("source or index changed during checkpoint")
        remaining()
        physical.validate()
        return _digest({"paths": records, "index": hashlib.sha256(index).hexdigest()})
    except (OSError, ValueError) as exc:
        raise _error(
            ErrorCode.CUTOVER_BLOCKED,
            "cutover source or index is unstable or exceeds its checkpoint budget",
        ) from exc


def legacy_activation_allowed(
    connection: sqlite3.Connection,
    repo_key: str,
    *,
    physical_key: str | None = None,
) -> bool:
    """Transaction-aware local gate; callers also verify the physical marker."""
    return (
        connection.execute(
            "SELECT 1 FROM checkout_regime_mappings m JOIN checkout_regimes r "
            "ON r.physical_key=m.physical_key WHERE m.repo_key=? AND "
            "r.regime<>'legacy' AND (? IS NULL OR r.physical_key=?) LIMIT 1",
            (repo_key, physical_key, physical_key),
        ).fetchone()
        is None
    )


def assert_legacy_claim(
    connection: sqlite3.Connection,
    repo_key: str,
    *,
    physical_key: str | None = None,
) -> None:
    row = connection.execute(
        "SELECT r.regime FROM checkout_regime_mappings m JOIN checkout_regimes r "
        "ON r.physical_key=m.physical_key WHERE m.repo_key=? AND r.regime<>'legacy' "
        "AND (? IS NULL OR r.physical_key=?) ORDER BY r.generation DESC LIMIT 1",
        (repo_key, physical_key, physical_key),
    ).fetchone()
    if row is not None:
        raise _error(
            ErrorCode.CUTOVER_BLOCKED
            if row[0] == "transition"
            else ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED,
            "legacy claims are disabled for this checkout regime",
        )


class CutoverManager:
    """No automatic migration: preview, explicit checkpoint, durable resume."""

    assert_legacy_claim = staticmethod(assert_legacy_claim)
    legacy_activation_allowed = staticmethod(legacy_activation_allowed)

    def __init__(
        self,
        store: ControlStore,
        authority: PhysicalAuthority,
        *,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.store, self.authority = store, authority
        self._fault = fault or (lambda stage: None)

    def preview(self, path: Path | str) -> dict[str, Any]:
        return self._inspect(path, observe_source=True)

    def inventory(self, path: Path | str) -> dict[str, Any]:
        """Inspect retained metadata without source observation or a new checkpoint."""
        return self._inspect(path, observe_source=False)

    def _inspect(self, path: Path | str, *, observe_source: bool) -> dict[str, Any]:
        physical = PhysicalCheckout.inspect(Path(path))
        try:
            regime = self.authority.regime(physical.root)
            with self.store.connection() as connection:
                connection.execute("BEGIN")
                return self._preview(
                    connection, physical, regime, observe_source=observe_source
                )
        finally:
            physical.close()

    def _preview(
        self,
        connection: sqlite3.Connection,
        physical: PhysicalCheckout,
        regime: dict[str, Any],
        *,
        observe_source: bool = True,
        source_checkpoint: str | None = None,
    ) -> dict[str, Any]:
        root = str(physical.root)
        repositories = [
            dict(row)
            for row in connection.execute(
                "SELECT repository_id,repo_key,main_worktree_path,git_common_dir,"
                "target_ref,"
                "integration_adapter FROM repositories ORDER BY repo_key"
            )
        ]
        provenance = list(
            {
                _json(dict(row)): dict(row)
                for row in connection.execute(
                    "SELECT repository_id,repo_key,canonical_path,git_common_dir,"
                    "target_ref,integration_adapter,provenance "
                    "FROM repository_registration_provenance ORDER BY registration_id"
                )
            }.values()
        )
        checkouts = [
            dict(row)
            for row in connection.execute(
                "SELECT checkout_id,repository_id,repo_key,canonical_path,"
                "git_common_dir "
                "FROM checkouts ORDER BY checkout_id"
            )
        ]

        def matches(value: str) -> bool:
            return str(Path(value).resolve()) == root

        keys = {
            row["repo_key"]
            for row in repositories
            if matches(row["main_worktree_path"])
        } | {
            row["repo_key"]
            for row in provenance + checkouts
            if matches(row["canonical_path"])
        }
        mappings: list[dict[str, Any]] = []
        blockers: list[dict[str, str]] = []
        for repo in repositories:
            if repo["repo_key"] not in keys:
                continue
            records = [
                p for p in provenance if p["repository_id"] == repo["repository_id"]
            ]
            attached = [
                c for c in checkouts if c["repository_id"] == repo["repository_id"]
            ]
            paths = {str(Path(repo["main_worktree_path"]).resolve())}
            paths.update(
                str(Path(p["canonical_path"]).resolve()) for p in records + attached
            )
            ambiguous_paths = paths != {root}
            known_physical_keys = {physical.key}
            for retained_path in paths:
                try:
                    retained = PhysicalCheckout.inspect(Path(retained_path))
                    try:
                        known_physical_keys.add(retained.key)
                    finally:
                        retained.close()
                except (LlmCoordError, OSError):
                    pass
            if any(
                p["provenance"] == "legacy_snapshot" for p in records
            ) and not self._proven_legacy_history(
                connection, repo["repo_key"], known_physical_keys
            ):
                blockers.append(
                    {
                        "kind": "mapping",
                        "id": repo["repo_key"],
                        "reason": (
                            "ambiguous_historical_mapping"
                            if ambiguous_paths
                            else "unproven_legacy_clone_history"
                        ),
                    }
                )
            attachments: list[dict[str, Any] | None] = list(attached) or [None]
            facts = {
                (p["canonical_path"], p["target_ref"], p["integration_adapter"])
                for p in records
            }
            facts.add(
                (
                    repo["main_worktree_path"],
                    repo["target_ref"],
                    repo["integration_adapter"],
                )
            )
            for checkout in attachments:
                if checkout is not None:
                    facts.add(
                        (
                            checkout["canonical_path"],
                            repo["target_ref"],
                            repo["integration_adapter"],
                        )
                    )
            for canonical_path, target_ref, adapter in sorted(facts):
                checkout = next(
                    (c for c in attached if c["canonical_path"] == canonical_path), None
                )
                observed_key: str | None = None
                try:
                    attachment = PhysicalCheckout.inspect(Path(canonical_path))
                    try:
                        observed_key = attachment.key
                    finally:
                        attachment.close()
                except (LlmCoordError, OSError):
                    pass  # Historical registrations remain visible after removal.
                mapping = {
                    "repo_key": repo["repo_key"],
                    "repository_id": repo["repository_id"],
                    "checkout_id": checkout["checkout_id"] if checkout else None,
                    "target_ref": target_ref,
                    "integration_adapter": adapter,
                    "canonical_path": canonical_path,
                    "physical_key": observed_key,
                    "provenance": records,
                    "registered_path": repo["main_worktree_path"],
                }
                mapping["mapping_id"] = _digest(
                    {"cutover_physical_key": physical.key, **mapping}
                )
                mappings.append(mapping)
        if not mappings:
            blockers.append(
                {
                    "kind": "mapping",
                    "id": physical.key,
                    "reason": "unregistered_checkout",
                }
            )
        placeholders = ",".join("?" for _ in keys) or "NULL"
        parameters = tuple(sorted(keys))

        def rows(sql: str) -> list[dict[str, Any]]:
            return [dict(row) for row in connection.execute(sql, parameters)]

        claims = rows(
            f"SELECT claim_id,task_id,task_attempt,repo_key,state,fencing_token,"
            f"lease_expires_at,updated_at FROM claims WHERE repo_key IN "
            f"({placeholders}) ORDER BY claim_id"
        )
        tasks = rows(
            f"SELECT task_id,repo_key,attempt,state,coordination_state,"
            f"current_claim_id,"
            f"session_id,result_revision,updated_at FROM tasks WHERE repo_key IN "
            f"({placeholders}) ORDER BY task_id"
        )
        executions = rows(
            f"SELECT e.execution_id,e.task_id,e.task_attempt,e.state,e.claim_id,"
            f"e.base_oid,"
            f"e.result_tree_id,e.result_commit_id,e.updated_at,"
            f"e.execution_contract,e.physical_key "
            f"FROM all_task_executions e JOIN tasks t ON t.task_id=e.task_id "
            f"WHERE t.repo_key IN ({placeholders}) ORDER BY e.execution_id"
        )
        publications = rows(
            f"SELECT intent_id,claim_id,task_id,task_attempt,operation_state,"
            f"target_ref,task_ref,"
            f"result_commit_id,updated_at FROM publication_intents WHERE repo_key "
            f"IN ({placeholders}) ORDER BY intent_id"
        )
        workflows = rows(
            f"SELECT w.task_id,w.attempt,w.execution_id,w.status,w.publication_id,"
            f"(w.proposal_json IS NOT NULL) AS has_proposal,w.expires_at FROM "
            f"task_workflows w "
            f"JOIN tasks t ON t.task_id=w.task_id WHERE t.repo_key IN "
            f"({placeholders}) ORDER BY w.task_id,w.attempt"
        )
        batches = rows(
            f"SELECT b.batch_id,b.checkout_id,b.state,b.updated_at FROM "
            f"workspace_batches b "
            f"JOIN checkouts c ON c.checkout_id=b.checkout_id WHERE c.repo_key IN "
            f"({placeholders}) ORDER BY b.batch_id"
        )
        manual = rows(
            f"SELECT p.publication_id,p.checkout_id,p.operation_state,p.updated_at "
            f"FROM workspace_publications p "
            f"JOIN checkouts c ON c.checkout_id=p.checkout_id WHERE c.repo_key IN "
            f"({placeholders}) ORDER BY p.publication_id"
        )
        # Only immutable bindings can prove work belongs to another checkout.
        # Unbound or conflicting evidence remains relevant and blocks conservatively.
        by_checkout = {c["checkout_id"]: c["canonical_path"] for c in checkouts}
        path_keys = {root: physical.key}

        def checkout_key(checkout_id: str) -> set[str]:
            path = by_checkout.get(checkout_id)
            if path is None:
                return set()
            if path not in path_keys:
                try:
                    observed = PhysicalCheckout.inspect(Path(path))
                    try:
                        path_keys[path] = observed.key
                    finally:
                        observed.close()
                except (LlmCoordError, OSError):
                    return set()
            return {path_keys[path]}

        claim_keys: dict[str, set[str]] = {c["claim_id"]: set() for c in claims}
        task_keys: dict[str, set[str]] = {t["task_id"]: set() for t in tasks}
        for row in connection.execute(
            "SELECT b.claim_id,b.physical_key FROM legacy_claim_bindings b "
            f"JOIN claims c ON c.claim_id=b.claim_id WHERE c.repo_key IN "
            f"({placeholders})",
            parameters,
        ):
            claim_keys[row["claim_id"]].add(row["physical_key"])
        for row in connection.execute(
            "SELECT c.claim_id,j.physical_key FROM claims c JOIN authority_journals j "
            "ON (j.kind='legacy_claim' AND j.reference_id=c.claim_id) OR "
            "(j.kind='legacy_execution' AND j.reference_id IN "
            "(SELECT execution_id FROM task_executions e WHERE e.claim_id=c.claim_id)) "
            "JOIN authority_checkouts a ON a.physical_key=j.physical_key "
            f"WHERE c.repo_key IN ({placeholders}) AND j.state='confirmed'",
            parameters,
        ):
            claim_keys[row["claim_id"]].add(row["physical_key"])
        for row in connection.execute(
            "SELECT t.task_id,s.checkout_id FROM tasks t "
            "JOIN sessions s ON s.session_id=t.session_id "
            f"WHERE t.repo_key IN ({placeholders})",
            parameters,
        ):
            task_keys[row["task_id"]].update(checkout_key(row["checkout_id"]))
        session_keys = {key: set(value) for key, value in task_keys.items()}
        attempts = {task["task_id"]: task["attempt"] for task in tasks}
        for claim in claims:
            claim_keys[claim["claim_id"]].update(session_keys[claim["task_id"]])
            if claim["task_attempt"] == attempts[claim["task_id"]]:
                task_keys[claim["task_id"]].update(claim_keys[claim["claim_id"]])
            claim["physical_keys"] = sorted(claim_keys[claim["claim_id"]])
        for execution in executions:
            proof = (
                {execution["physical_key"]}
                if execution["physical_key"]
                else claim_keys.get(execution["claim_id"], set())
            )
            if execution["task_attempt"] == attempts[execution["task_id"]]:
                task_keys[execution["task_id"]].update(proof)
            execution["physical_keys"] = sorted(proof)

        def relevant(proof: set[str] | list[str]) -> bool:
            return len(proof) != 1 or physical.key in proof

        for task in tasks:
            task["physical_keys"] = sorted(task_keys[task["task_id"]])

        def task_relevant(task_id: str) -> bool:
            return relevant(task_keys.get(task_id, set()))

        authority_receipts = rows(
            "SELECT DISTINCT j.journal_id,j.physical_key,j.kind,j.reference_id,j.state,"
            "a.checkout_id,c.canonical_path FROM authority_journals j "
            "LEFT JOIN authority_checkouts a ON a.physical_key=j.physical_key "
            "LEFT JOIN checkouts c ON c.checkout_id=a.checkout_id "
            "JOIN claims q ON (j.kind='legacy_claim' AND j.reference_id=q.claim_id) OR "
            "(j.kind='legacy_execution' AND j.reference_id IN "
            "(SELECT execution_id FROM task_executions e WHERE e.claim_id=q.claim_id)) "
            f"WHERE q.repo_key IN ({placeholders}) ORDER BY j.journal_id"
        )
        dispositions: list[dict[str, Any]] = []
        for claim in claims:
            state = claim["state"]
            if not relevant(claim["physical_keys"]):
                dispositions.append(
                    {
                        "kind": "claim",
                        "id": claim["claim_id"],
                        "state": state,
                        "default": "retain_other_checkout",
                        "allowed": [],
                        "physical_keys": claim["physical_keys"],
                        "retained_evidence": [
                            "task",
                            "execution",
                            "checkpoint",
                            "publication_refs",
                        ],
                    }
                )
                continue
            default = "retain_history"
            allowed: list[str] = []
            if state == "queued":
                default, allowed = "retain_legacy", ["cancel"]
            elif state in {"active_work", "publishing", "active_integration"}:
                default = "drain_or_recover"
            elif state == "expired" and (
                any(
                    e["claim_id"] == claim["claim_id"]
                    and e["state"] not in {"failed", "cleaned", "published"}
                    for e in executions
                )
                or any(
                    t["task_id"] == claim["task_id"]
                    and t["attempt"] == claim["task_attempt"]
                    and t["state"] not in {"completed", "failed", "cancelled"}
                    for t in tasks
                )
            ):
                default = "recover"
            dispositions.append(
                {
                    "kind": "claim",
                    "id": claim["claim_id"],
                    "state": state,
                    "default": default,
                    "allowed": allowed,
                    "retained_evidence": [
                        "task",
                        "execution",
                        "checkpoint",
                        "publication_refs",
                    ],
                }
            )
            if default in {"drain_or_recover", "recover"}:
                blockers.append(
                    {"kind": "claim", "id": claim["claim_id"], "reason": default}
                )
        for execution in executions:
            if relevant(execution["physical_keys"]) and execution["state"] not in {
                "failed",
                "cleaned",
                "published",
            }:
                blockers.append(
                    {
                        "kind": "execution",
                        "id": execution["execution_id"],
                        "reason": "drain_or_recover",
                    }
                )
        for task in tasks:
            if (
                task_relevant(task["task_id"])
                and task["state"] not in {"completed", "failed", "cancelled"}
                and not task["current_claim_id"]
            ):
                blockers.append(
                    {"kind": "task", "id": task["task_id"], "reason": "unsettled_task"}
                )
        for publication in publications:
            if task_relevant(publication["task_id"]) and publication[
                "operation_state"
            ] not in {"confirmed", "failed_safe"}:
                blockers.append(
                    {
                        "kind": "publication",
                        "id": publication["intent_id"],
                        "reason": "ambiguous_publication",
                    }
                )
        for batch in batches:
            if relevant(checkout_key(batch["checkout_id"])) and batch["state"] in {
                "applying",
                "operator_attention",
            }:
                blockers.append(
                    {
                        "kind": "batch",
                        "id": batch["batch_id"],
                        "reason": "unfinished_publication",
                    }
                )
        for publication in manual:
            if relevant(checkout_key(publication["checkout_id"])) and publication[
                "operation_state"
            ] in {"prepared", "operator_attention"}:
                blockers.append(
                    {
                        "kind": "publication",
                        "id": publication["publication_id"],
                        "reason": "unfinished_publication",
                    }
                )
        for workflow in workflows:
            dispositions.append(
                {
                    "kind": "workflow",
                    "id": workflow["task_id"],
                    "state": workflow["status"],
                    "default": "retain_history_and_proposal",
                    "allowed": [],
                    "retained_evidence": [
                        "proposal",
                        "checks",
                        "checkpoint",
                        "publication_refs",
                    ],
                }
            )
        for kind, items, identity_key, state_key, terminal in (
            ("task", tasks, "task_id", "state", {"completed", "failed", "cancelled"}),
            (
                "execution",
                executions,
                "execution_id",
                "state",
                {"failed", "cleaned", "published"},
            ),
            (
                "publication",
                publications,
                "intent_id",
                "operation_state",
                {"confirmed", "failed_safe"},
            ),
            ("batch", batches, "batch_id", "state", {"published", "diverged"}),
            (
                "workspace_publication",
                manual,
                "publication_id",
                "operation_state",
                {"confirmed", "rolled_back", "diverged"},
            ),
        ):
            for item in items:
                related = (
                    relevant(item["physical_keys"])
                    if "physical_keys" in item
                    else task_relevant(item["task_id"])
                    if "task_id" in item
                    else relevant(checkout_key(item["checkout_id"]))
                )
                dispositions.append(
                    {
                        "kind": kind,
                        "id": item[identity_key],
                        "state": item[state_key],
                        "default": (
                            "retain_other_checkout"
                            if not related
                            else "retain_history"
                            if item[state_key] in terminal
                            else "drain_or_recover"
                        ),
                        "allowed": [],
                        "retained_evidence": [
                            "history",
                            "checkpoint",
                            "publication_refs",
                        ],
                    }
                )
        head = run_git(physical.root, ["rev-parse", "--verify", "HEAD"], check=False)
        evidence = {
            "physical_key": physical.key,
            "binding": physical.binding,
            "head": head.stdout.strip() if head.returncode == 0 else None,
            "mappings": mappings,
            "claims": claims,
            "tasks": tasks,
            "executions": executions,
            "publications": publications,
            "workflows": workflows,
            "batches": batches,
            "workspace_publications": manual,
            "authority_receipts": authority_receipts,
        }
        checkpoint_evidence = dict(evidence)
        checkpoint_evidence["claims"] = [
            c for c in claims if relevant(c["physical_keys"])
        ]
        checkpoint_evidence["tasks"] = [t for t in tasks if task_relevant(t["task_id"])]
        checkpoint_evidence["executions"] = [
            e for e in executions if relevant(e["physical_keys"])
        ]
        checkpoint_evidence["publications"] = [
            p for p in publications if task_relevant(p["task_id"])
        ]
        checkpoint_evidence["workflows"] = [
            w for w in workflows if task_relevant(w["task_id"])
        ]
        checkpoint_evidence["batches"] = [
            b for b in batches if relevant(checkout_key(b["checkout_id"]))
        ]
        checkpoint_evidence["workspace_publications"] = [
            p for p in manual if relevant(checkout_key(p["checkout_id"]))
        ]
        checkpoint_evidence["authority_receipts"] = [
            receipt
            for receipt in authority_receipts
            if receipt["physical_key"] == physical.key
        ]
        checkpoint_evidence["blockers"] = blockers
        # Once an intent closes legacy admission, only this retained history must
        # stay fixed. Source, index and HEAD can keep changing under an editor.
        history = {
            "physical_key": physical.key,
            **{key: checkpoint_evidence[key] for key in _HISTORY_KEYS},
        }
        return {
            "physical_key": physical.key,
            "path": root,
            "regime": regime["regime"],
            "generation": regime["generation"],
            "checkpoint": (
                _digest(
                    {
                        "manifest": checkpoint_evidence,
                        "source": source_checkpoint or _source_checkpoint(physical),
                    }
                )
                if observe_source
                else None
            ),
            "checkpoint_observed": observe_source,
            "history_checkpoint": _digest(history),
            **{
                key: value
                for key, value in evidence.items()
                if key not in {"physical_key", "binding"}
            },
            "dispositions": dispositions,
            "blockers": blockers,
            "ready": not blockers,
        }

    @staticmethod
    def _proven_legacy_history(
        connection: sqlite3.Connection,
        repo_key: str,
        known_physical_keys: set[str],
    ) -> bool:
        """A pre-upgrade latest-path row is insufficient; require G2 receipts."""
        tasks = connection.execute(
            "SELECT task_id FROM tasks WHERE repo_key=? AND session_id IS NULL",
            (repo_key,),
        ).fetchall()
        for task in tasks:
            objects = connection.execute(
                "SELECT 'legacy_execution' AS kind,execution_id AS id "
                "FROM task_executions "
                "WHERE task_id=? UNION ALL SELECT 'legacy_claim',claim_id "
                "FROM claims WHERE task_id=?",
                (task["task_id"], task["task_id"]),
            ).fetchall()
            if not objects:
                return False
            for item in objects:
                bound = connection.execute(
                    "SELECT 1 FROM legacy_claim_bindings WHERE claim_id=? "
                    "UNION ALL SELECT 1 FROM legacy_claim_bindings b "
                    "JOIN task_executions e ON e.claim_id=b.claim_id "
                    "WHERE e.execution_id=? LIMIT 1",
                    (item["id"], item["id"]),
                ).fetchone()
                if bound:
                    continue
                receipts = connection.execute(
                    "SELECT j.physical_key,j.state,a.binding_json "
                    "FROM authority_journals j "
                    "LEFT JOIN authority_checkouts a "
                    "ON a.physical_key=j.physical_key "
                    "WHERE j.kind=? AND j.reference_id=?",
                    (item["kind"], item["id"]),
                ).fetchall()
                if not receipts and item["kind"] == "legacy_claim":
                    # The confirmed execution also proves its mandatory claim binding.
                    receipts = connection.execute(
                        "SELECT j.physical_key,j.state,a.binding_json "
                        "FROM task_executions e "
                        "JOIN authority_journals j ON j.kind='legacy_execution' "
                        "AND j.reference_id=e.execution_id "
                        "LEFT JOIN authority_checkouts a "
                        "ON a.physical_key=j.physical_key "
                        "WHERE e.claim_id=?",
                        (item["id"],),
                    ).fetchall()
                if not receipts or len({r["physical_key"] for r in receipts}) != 1:
                    return False
                for receipt in receipts:
                    if receipt["physical_key"] not in known_physical_keys:
                        return False
                    if receipt["state"] != "confirmed" or not receipt["binding_json"]:
                        return False
                    try:
                        binding = json.loads(receipt["binding_json"])
                    except (ValueError, TypeError):
                        return False
                    if not isinstance(binding, dict) or not binding:
                        return False
        return True

    def begin(
        self,
        path: Path | str,
        checkpoint: str,
        dispositions: dict[str, str] | None = None,
        intent_id: str | None = None,
    ) -> dict[str, Any]:
        requested = dict(dispositions or {})
        with (
            self.authority.pin(Path(path), recovery=True) as pin,
            pin.owner.mutation_lock,
        ):
            if pin.owner.pins != 1:
                raise _error(
                    ErrorCode.CUTOVER_BLOCKED,
                    "live checkout workers must drain before cutover",
                )
            regime = self.authority.regime(pin.owner.physical.root)
            with self.store.connection() as connection:
                existing = (
                    connection.execute(
                        "SELECT * FROM cutover_intents WHERE intent_id=?",
                        (intent_id,),
                    ).fetchone()
                    if intent_id is not None
                    else None
                )
                pending = connection.execute(
                    "SELECT intent_id FROM cutover_intents WHERE physical_key=? "
                    "AND stage<>'complete'",
                    (pin.owner.physical.key,),
                ).fetchone()
            if existing is not None:
                if (
                    existing["physical_key"] != pin.owner.physical.key
                    or existing["checkpoint"] != checkpoint
                    or existing["dispositions_json"] != _json(requested)
                ):
                    raise _error(
                        ErrorCode.CUTOVER_CHECKPOINT_STALE,
                        "cutover intent request changed",
                    )
                return self._resume_owned(pin, str(existing["intent_id"]), checkpoint)
            if pending is not None:
                raise _error(
                    ErrorCode.CUTOVER_BLOCKED, "resume the existing cutover intent"
                )
            if pin.owner.marker["state"] != "clean":
                raise _error(
                    ErrorCode.CUTOVER_BLOCKED,
                    "unfinished authority journal blocks cutover",
                )
            if regime["regime"] != "legacy":
                raise _error(
                    ErrorCode.CUTOVER_BLOCKED,
                    "checkout is already transitioning or workspace managed",
                )
            # Hash source before taking the write lock; this scan can take
            # seconds, and every other daemon writer would wait behind it.
            source = _source_checkpoint(pin.owner.physical)
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    plan = self._preview(
                        connection,
                        pin.owner.physical,
                        regime,
                        source_checkpoint=source,
                    )
                    if checkpoint != plan["checkpoint"]:
                        raise _error(
                            ErrorCode.CUTOVER_CHECKPOINT_STALE,
                            "cutover preview changed; preview again",
                        )
                    cancellable = {
                        d["id"]
                        for d in plan["dispositions"]
                        if d["kind"] == "claim" and "cancel" in d["allowed"]
                    }
                    if any(
                        key not in cancellable or value != "cancel"
                        for key, value in requested.items()
                    ):
                        raise _error(
                            ErrorCode.CUTOVER_BLOCKED,
                            "unsupported or unsafe cutover disposition",
                        )
                    blockers = [
                        b
                        for b in plan["blockers"]
                        if not (b["kind"] == "claim" and b["id"] in requested)
                    ]
                    if blockers:
                        raise _error(
                            ErrorCode.CUTOVER_BLOCKED,
                            "legacy work or ambiguous mappings block cutover",
                        )
                    now = self.store._now(None)
                    identity = intent_id or "cutover_" + secrets.token_hex(16)
                    connection.execute(
                        "INSERT INTO checkout_regimes VALUES (?,?,'transition',1,?,"
                        "?,?)",
                        (plan["physical_key"], plan["path"], identity, checkpoint, now),
                    )
                    for mapping in plan["mappings"]:
                        connection.execute(
                            "INSERT INTO checkout_regime_mappings VALUES (?,?,?,?,"
                            "?,?,?,?,?)",
                            (
                                mapping["mapping_id"],
                                plan["physical_key"],
                                mapping["repo_key"],
                                mapping["repository_id"],
                                mapping["checkout_id"],
                                mapping["target_ref"],
                                mapping["integration_adapter"],
                                mapping["canonical_path"],
                                _json(mapping["provenance"]),
                            ),
                        )
                    # Later ownership handoffs are ordered after these event
                    # sequences, independent of wall-clock time.
                    owned = sorted(
                        {
                            m["checkout_id"]
                            for m in plan["mappings"]
                            if m["checkout_id"] is not None
                            and m["physical_key"] == plan["physical_key"]
                        }
                    )
                    plan["checkout_event_sequences"] = {
                        str(row["checkout_id"]): int(row["next_event_sequence"])
                        for row in connection.execute(
                            "SELECT checkout_id,next_event_sequence FROM "
                            "checkout_heads WHERE checkout_id IN "
                            f"({','.join('?' for _ in owned) or 'NULL'})",
                            owned,
                        )
                    }
                    connection.execute(
                        (
                            "INSERT INTO cutover_intents VALUES "
                            "(?,?,?,?,?,'prepared',1,2,NULL,?,?)"
                        ),
                        (
                            identity,
                            plan["physical_key"],
                            checkpoint,
                            _json(plan),
                            _json(requested),
                            now,
                            now,
                        ),
                    )
                    for item in plan["dispositions"]:
                        connection.execute(
                            "INSERT INTO cutover_dispositions VALUES (?,?,?,?,?,?,?)",
                            (
                                identity,
                                item["kind"],
                                item["id"],
                                item["state"],
                                requested.get(item["id"], item["default"]),
                                "explicit checkout regime conversion",
                                _json(item["retained_evidence"]),
                            ),
                        )
                    connection.execute(
                        (
                            "INSERT INTO authority_journals VALUES "
                            "(?,?,'regime_cutover',?,'applying',?)"
                        ),
                        (identity, plan["physical_key"], identity, now),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            self._fault("intent_saved")
            return self._resume_owned(pin, identity, checkpoint)

    def resume(
        self,
        path: Path | str,
        intent_id: str,
        *,
        checkpoint: str | None = None,
    ) -> dict[str, Any]:
        with (
            self.authority.pin(Path(path), recovery=True) as pin,
            pin.owner.mutation_lock,
        ):
            if pin.owner.pins != 1:
                raise _error(
                    ErrorCode.CUTOVER_BLOCKED,
                    "live checkout workers must drain before cutover",
                )
            return self._resume_owned(pin, intent_id, checkpoint)

    def _resume_owned(
        self, pin: Any, identity: str, checkpoint: str | None
    ) -> dict[str, Any]:
        physical = pin.owner.physical
        with self.store.connection() as connection:
            intent = connection.execute(
                "SELECT * FROM cutover_intents WHERE intent_id=? AND physical_key=?",
                (identity, physical.key),
            ).fetchone()
        if intent is None:
            raise _error(
                ErrorCode.CUTOVER_BLOCKED, "cutover intent does not match this checkout"
            )
        if checkpoint is not None and checkpoint != intent["checkpoint"]:
            raise _error(
                ErrorCode.CUTOVER_CHECKPOINT_STALE,
                "resume requires the original preview checkpoint",
            )
        if pin.owner.marker["state"] == "dirty" and (
            pin.owner.marker.get("journal_id") != identity
            or pin.owner.marker.get("journal_kind") != "regime_cutover"
        ):
            raise _error(
                ErrorCode.CUTOVER_BLOCKED,
                "another unfinished journal requires recovery",
            )
        # From here on only retained legacy history must stay fixed: the saved
        # intent already closed legacy admission, while source, index and HEAD
        # may keep changing. Intents saved before history digests existed keep
        # their original source-inclusive checks.
        history = json.loads(intent["plan_json"]).get("history_checkpoint")

        def history_unchanged(expected: str) -> bool:
            if history is None:
                return bool(self.preview(physical.root)["checkpoint"] == expected)
            return bool(self.inventory(physical.root)["history_checkpoint"] == expected)

        stage = intent["stage"]
        if stage == "prepared":
            # Before the transition marker, other profiles still see a legacy
            # checkout. Their work is not in this database, so refuse if one
            # owned the checkout after the intent was saved.
            if history is not None and self._foreign_owner_since(intent):
                raise _error(
                    ErrorCode.CUTOVER_CHECKPOINT_STALE,
                    "another profile owned this checkout after the cutover intent "
                    "was saved; cutover requires attention",
                )
            if not history_unchanged(history or intent["checkpoint"]):
                raise _error(
                    ErrorCode.CUTOVER_CHECKPOINT_STALE,
                    "retained legacy history changed before transition; cutover "
                    "requires attention",
                )
            self.authority.advance_regime(
                pin,
                regime="transition",
                generation=intent["transition_generation"],
                checkpoint=intent["checkpoint"],
                intent_id=identity,
            )
            self._stage(identity, "transition")
            stage = "transition"
            self._fault("transition_marked")
        if stage == "transition":
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    observed = self._preview(
                        connection,
                        physical,
                        self.authority.regime(physical.root),
                        observe_source=history is None,
                    )
                    if (
                        observed["checkpoint"] != intent["checkpoint"]
                        if history is None
                        else observed["history_checkpoint"] != history
                    ):
                        raise _error(
                            ErrorCode.CUTOVER_CHECKPOINT_STALE,
                            "legacy history changed during transition",
                        )
                    now = self.store._now(None)
                    for claim_id in json.loads(intent["dispositions_json"]):
                        claim = connection.execute(
                            "SELECT * FROM claims WHERE claim_id=?", (claim_id,)
                        ).fetchone()
                        if claim is None or claim["state"] != "queued":
                            raise _error(
                                ErrorCode.CUTOVER_BLOCKED,
                                "queued cancellation no longer matches the preview",
                            )
                        connection.execute(
                            (
                                "UPDATE claims SET "
                                "state='cancelled',lease_expires_at=NULL,"
                                "terminal_expires_at=?,"
                                "released_at=?,release_reason='checkout regime "
                                "conversion',updated_at=? WHERE claim_id=?"
                            ),
                            (now + 86_400_000, now, now, claim_id),
                        )
                        connection.execute(
                            (
                                "UPDATE tasks SET "
                                "state='cancelled',coordination_state='cancelled',"
                                "current_claim_id=NULL,"
                                "current_fencing_token=NULL,finished_at=?,"
                                "updated_at=? WHERE "
                                "task_id=? AND current_claim_id=?"
                            ),
                            (now, now, claim["task_id"], claim_id),
                        )
                    frozen = json.loads(intent["plan_json"])
                    selected = json.loads(intent["dispositions_json"])
                    retained = [
                        d["id"]
                        for d in frozen["dispositions"]
                        if d["kind"] == "claim"
                        and d["default"] == "retain_legacy"
                        and d["id"] not in selected
                    ]
                    for checkout_id in sorted(
                        {
                            m["checkout_id"]
                            for m in frozen["mappings"]
                            if m["checkout_id"] is not None
                            and m["physical_key"] == physical.key
                        }
                    ):
                        self.store.append_checkout_event(
                            connection,
                            checkout_id=checkout_id,
                            event_type="checkout.regime_cutover",
                            caused_by_session_id=None,
                            payload={
                                "intent_id": identity,
                                "retained_legacy_claim_ids": retained,
                                "cancelled_claim_ids": sorted(selected),
                            },
                            now=now,
                        )
                    settled_plan = self._preview(
                        connection,
                        physical,
                        self.authority.regime(physical.root),
                        observe_source=history is None,
                    )
                    settled = (
                        settled_plan["checkpoint"]
                        if history is None
                        else settled_plan["history_checkpoint"]
                    )
                    connection.execute(
                        (
                            "UPDATE checkout_regimes SET "
                            "regime='workspace',generation=?,checkpoint=?,updated_at=? "
                            "WHERE physical_key=?"
                        ),
                        (intent["workspace_generation"], settled, now, physical.key),
                    )
                    connection.execute(
                        (
                            "UPDATE cutover_intents SET "
                            "stage='checkpointed',settled_checkpoint=?,"
                            "updated_at=? WHERE "
                            "intent_id=?"
                        ),
                        (settled, now, identity),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            stage = "checkpointed"
            self._fault("checkpoint_saved")
        with self.store.connection() as connection:
            intent = connection.execute(
                "SELECT * FROM cutover_intents WHERE intent_id=?", (identity,)
            ).fetchone()
        if stage == "checkpointed":
            if not history_unchanged(intent["settled_checkpoint"]):
                raise _error(
                    ErrorCode.CUTOVER_CHECKPOINT_STALE,
                    "retained legacy history changed after disposition checkpoint",
                )
            self.authority.advance_regime(
                pin,
                regime="workspace",
                generation=intent["workspace_generation"],
                checkpoint=intent["settled_checkpoint"],
                intent_id=identity,
            )
            self._stage(identity, "workspace")
            stage = "workspace"
            self._fault("workspace_marked")
        if stage == "workspace":
            if not history_unchanged(intent["settled_checkpoint"]):
                raise _error(
                    ErrorCode.CUTOVER_CHECKPOINT_STALE,
                    "retained legacy history changed before final cutover confirmation",
                )
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    (
                        "UPDATE cutover_intents SET stage='complete',updated_at=? "
                        "WHERE intent_id=?"
                    ),
                    (self.store._now(None), identity),
                )
                connection.execute(
                    (
                        "UPDATE authority_journals SET state='confirmed' WHERE "
                        "journal_id=?"
                    ),
                    (identity,),
                )
                connection.commit()
            self._fault("intent_completed")
        current = self.authority.regime(physical.root)
        if (
            current["regime"] != "workspace"
            or current["generation"] != intent["workspace_generation"]
            or current["checkpoint"] != intent["settled_checkpoint"]
            or current["intent_id"] != identity
        ):
            raise _error(
                ErrorCode.CUTOVER_BLOCKED, "completed cutover marker proof changed"
            )
        Mutation(self.authority, pin.owner, "regime_cutover", identity).confirm()
        self._fault("marker_clean")
        return self.status(physical.root)

    def _foreign_owner_since(self, intent: sqlite3.Row) -> bool:
        """Whether a clean handoff from another profile followed this intent."""
        sequences = json.loads(intent["plan_json"])["checkout_event_sequences"]
        with self.store.connection() as connection:
            for checkout_id, sequence in sequences.items():
                for row in connection.execute(
                    "SELECT payload_json FROM checkout_events WHERE checkout_id=? "
                    "AND sequence>? AND event_type='checkout.authority_handoff'",
                    (checkout_id, sequence),
                ):
                    previous = json.loads(row["payload_json"]).get("previous_profile")
                    if previous != self.authority.profile:
                        return True
        return False

    def _stage(self, identity: str, stage: str) -> None:
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE cutover_intents SET stage=?,updated_at=? WHERE intent_id=?",
                (stage, self.store._now(None), identity),
            )

    def status(self, path: Path | str) -> dict[str, Any]:
        result = self.inventory(path)
        with self.store.connection() as connection:
            row = connection.execute(
                (
                    "SELECT intent_id,checkpoint,stage FROM cutover_intents WHERE "
                    "physical_key=? ORDER BY created_at DESC,intent_id DESC LIMIT 1"
                ),
                (result["physical_key"],),
            ).fetchone()
        result["current_intent"] = dict(row) if row else None
        result["intent_id"] = row["intent_id"] if row else None
        if row:
            result["current_checkpoint"] = result["checkpoint"]
            result["checkpoint"] = row["checkpoint"]
        return result
