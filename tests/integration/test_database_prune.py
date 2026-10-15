"""Pruning preserves task inspection and interrupted execution recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from llm_cli.cli.app import build_parser, dispatch
from llm_cli.coordination.models import ClaimState
from llm_cli.daemon.service import DaemonService
from llm_cli.execution.recovery import ExecutionReconciler
from llm_cli.storage.retention import prune_task_output


def test_pruned_database_remains_inspectable_and_recovery_works(
    tmp_path: Path,
    service_factory: Any,
    request_factory: Any,
    repository_factory: Any,
    git_run: Any,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"README.md": "base\n"})
        service: DaemonService = service_factory(tmp_path)
        service.initialize()
        try:
            registered = await service.handle(
                request_factory("repo.add", {"path": str(repository)})
            )
            old = service.store.create_task(
                repository_id=registered["repository_id"],
                task_id="old",
                title="saved task",
            )
            with service.store.connection() as connection:
                connection.execute(
                    "UPDATE tasks SET state='completed',finished_at=1,updated_at=1 "
                    "WHERE task_id=?",
                    (old.task_id,),
                )
            for kind in ("model.text.delta", "model.finished"):
                service.store.record_task_event(
                    task_id=old.task_id,
                    claim_id=None,
                    event_type=kind,
                    payload={"answer": "full answer", "summary": "saved summary"},
                    now=1,
                )
            interrupted = service.store.create_task(
                repository_id=registered["repository_id"],
                task_id="interrupted",
                title="recover",
            )
            claim = service.coordinator.request_claim(
                interrupted.task_id, ["README.md"]
            )
            execution = service.store.create_execution(
                task_id=interrupted.task_id,
                attempt=1,
                claim_id=claim.claim_id,
                driver="fixture-write",
                worktree_path=str(service.paths.data_dir / "worktrees" / "missing"),
                base_oid=git_run(repository, "rev-parse", "HEAD"),
                boot_id="dead",
            )
            service.store.record_task_event(
                task_id=interrupted.task_id,
                claim_id=claim.claim_id,
                event_type="command.output",
                payload={"text": "recovery evidence"},
                now=1,
            )
            before = service.store.list_task_events(interrupted.task_id)
            arguments = build_parser().parse_args(
                ["db", "prune", "--older-than", "30d"]
            )

            class Client:
                def call(
                    self, method: str, params: dict[str, Any]
                ) -> dict[str, object]:
                    assert method == "db.prune"
                    return prune_task_output(service.store, **params)

            preview = dispatch(arguments, Client())  # type: ignore[arg-type]
            assert preview["dry_run"] is True
            applied = await service.handle(request_factory("db.prune", {"apply": True}))
            assert applied["event_chunks"] == 1
            shown = await service.handle(
                request_factory("task.show", {"task_id": old.task_id})
            )
            assert shown["title"] == "saved task"
            answer = next(
                e
                for e in service.store.list_task_events(old.task_id)
                if e.event_type == "model.finished"
            )
            assert answer.payload == {
                "answer": "full answer",
                "summary": "saved summary",
            }
            assert service.store.list_task_events(interrupted.task_id) == before
            reconciler = ExecutionReconciler(
                service.store,
                service.coordinator,
                managed_worktree_root=service.paths.data_dir / "worktrees",
                boot_id="new",
            )
            report = reconciler.reconcile()
            assert report.summary == {"released": 1}
            recovered = service.store.get_execution(interrupted.task_id, 1)
            assert (
                recovered is not None
                and recovered.execution_id == execution.execution_id
            )
            assert recovered.state == "failed"
            assert service.store.get_claim(claim.claim_id).state is ClaimState.RELEASED  # type: ignore[union-attr]
            assert reconciler.reconcile().outcomes == ()
        finally:
            service.close()

    asyncio.run(scenario())
