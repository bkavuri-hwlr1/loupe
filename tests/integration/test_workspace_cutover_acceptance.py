"""Real service RPC acceptance with scripted models and disposable authority."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from test_shared_agent_execution import ScriptedProvider, _call, _finish, _tools

from llm_cli.config.models import Settings
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.capabilities import WORKSPACE_CAPABILITIES
from llm_cli.protocol.envelopes import Request
from llm_cli.providers.base import ModelTurn, ToolCallResult


def _request(method: str, **params: Any) -> Request:
    return Request.create(
        request_id=f"request-{method}",
        method=method,
        params=params,
        profile_id="test",
    )


def _service(tmp_path: Path, policy: str = "minimal") -> DaemonService:
    paths = AppPaths.resolve(
        "test",
        environ={
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home=tmp_path,
    )
    return DaemonService(
        paths,
        Settings(
            profile_id="test",
            agent_provider="scripted",
            agent_model="workspace-model",
            agent_commands="off",
            agent_command_environment=policy,
        ),
        asyncio.Event(),
        boot_id="boot-cutover-acceptance",
        authority_root=tmp_path / "physical-authority" / "v1",
    )


async def _cutover(service: DaemonService, repository: Path) -> dict[str, Any]:
    plan = await service.handle(
        _request("workspace.cutover.plan", path=str(repository))
    )
    assert plan["regime"] == "legacy" and plan["ready"] is True, plan
    assert plan["mappings"] and plan["checkpoint"]
    await service.handle(
        _request(
            "workspace.cutover.apply",
            path=str(repository),
            checkpoint=plan["checkpoint"],
            intent_id="cutover-acceptance",
            dispositions={},
        )
    )
    status = await service.handle(
        _request("workspace.cutover.status", path=str(repository))
    )
    assert status["regime"] == "workspace", status
    return status


@pytest.mark.parametrize("policy", ["minimal", "session"])
def test_sessionless_workspace_rpc_has_no_claim_and_closes_after_actual_worker(
    tmp_path: Path,
    policy: str,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    git_run: Callable[..., str],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/a.md": "base\n"})
        service = _service(tmp_path, policy)
        environment_seen: list[str] = []
        canary = "one-shot-project-variable-in-memory-only"

        def after_read(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert all(not result.is_error for result in results), results
            task = service.store.get_task("workspace-task")
            assert task is not None and task.session_id is not None
            if policy == "session":
                environment = service.session_environments.get(task.session_id)
                assert environment.values["PROJECT_FLAG"] == canary
                assert "OPENAI_API_KEY" not in environment.values
                assert "CUSTOM_PROVIDER_SECRET" not in environment.values
                environment_seen.append(task.session_id)
            return _tools(
                _call("write_file", path="docs/a.md", content="workspace result\n")
            )

        def after_write(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert all(not result.is_error for result in results), results
            return _finish("workspace result published")

        provider = ScriptedProvider(
            "workspace-model",
            [
                _tools(_call("read_file", path="docs/a.md")),
                after_read,
                after_write,
            ],
        )
        service.providers.register(
            "scripted",
            lambda _model: provider,
            credential_environment_names=("CUSTOM_PROVIDER_SECRET",),
        )
        service.initialize()
        try:
            await service.handle(_request("repo.add", path=str(repository)))
            original_git = (
                git_run(repository, "rev-parse", "HEAD"),
                git_run(repository, "write-tree"),
            )
            await _cutover(service, repository)
            ping = await service.handle(_request("system.ping"))
            assert set(WORKSPACE_CAPABILITIES) <= set(ping["capabilities"])
            params: dict[str, Any] = {
                "path": str(repository),
                "task_id": "workspace-task",
                "title": "update the guide",
                "scopes": ["docs/"],
                "provider": "scripted",
                "model": "workspace-model",
            }
            if policy == "session":
                params["command_environment"] = {
                    "PROJECT_FLAG": canary,
                    "OPENAI_API_KEY": "fixture-provider-value",
                    "CUSTOM_PROVIDER_SECRET": "fixture-custom-provider-value",
                }
            accepted = await service.handle(_request("task.run", **params))
            assert accepted["claim"] is None
            worker = service._background_tasks[("workspace-task", 1)]
            await asyncio.wait_for(asyncio.shield(worker), timeout=30)
            assert worker.done()
            task = service.store.get_task("workspace-task")
            assert task is not None and task.state == "completed", task
            assert task.current_claim_id is None and task.current_fencing_token is None
            assert task.session_id is not None
            session = service.store.get_session(task.session_id)
            assert session is not None and session.workspace_mode == "shared"
            assert (
                session.state == "closed" and session.close_reason == "one_shot_settled"
            )
            execution = service.store.get_execution(task.task_id, 1)
            assert execution is not None
            assert execution.execution_contract == "workspace_v1"
            assert execution.claim_id is None and execution.state == "published"
            launch = service.store.get_execution_launch(task.task_id, 1)
            assert launch is not None and launch.parameters["one_shot_session"] is True
            with service.store.connection() as connection:
                assert (
                    connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
                )
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM task_executions"
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM workspace_executions"
                    ).fetchone()[0]
                    == 1
                )
            assert (repository / "docs/a.md").read_text() == "workspace result\n"
            assert (
                git_run(repository, "rev-parse", "HEAD"),
                git_run(repository, "write-tree"),
            ) == original_git
            assert not provider.steps
            assert bool(environment_seen) == (policy == "session")
            assert (
                service.session_environments.plan(task.session_id)["state"]
                == "awaiting_session_environment"
            )
            for suffix in ("", "-wal"):
                database = Path(str(service.paths.control_db) + suffix)
                if database.exists():
                    assert canary.encode() not in database.read_bytes()
            with pytest.raises(LlmCoordError) as refused:
                await service.handle(
                    _request(
                        "task.run",
                        path=str(repository),
                        task_id="refused-claim",
                        title="legacy claim",
                        scopes=["docs/"],
                        claim_only=True,
                    )
                )
            assert refused.value.code is ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED
            assert service.store.get_task("refused-claim") is None
            exported = await service.handle(
                _request("workspace.cutover.export", path=str(repository))
            )
            assert exported["format"] == "loupe.legacy-metadata.v1"
            assert "checkpoint_json" not in str(exported)
            assert canary not in str(exported)
        finally:
            service.close()

    asyncio.run(scenario())


def test_preview_and_stale_checkpoint_leave_the_legacy_regime_intact(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"README.md": "base\n"})
        service = _service(tmp_path)
        service.initialize()
        try:
            await service.handle(_request("repo.add", path=str(repository)))
            first = await service.handle(
                _request("workspace.cutover.plan", path=str(repository))
            )
            second = await service.handle(
                _request("workspace.cutover.plan", path=str(repository))
            )
            assert first["checkpoint"] == second["checkpoint"]
            assert first["regime"] == second["regime"] == "legacy"
            with pytest.raises(LlmCoordError) as stale:
                await service.handle(
                    _request(
                        "workspace.cutover.apply",
                        path=str(repository),
                        checkpoint="stale-reviewed-checkpoint",
                        intent_id="must-not-start",
                    )
                )
            assert stale.value.code is ErrorCode.CUTOVER_CHECKPOINT_STALE
            status = await service.handle(
                _request("workspace.cutover.status", path=str(repository))
            )
            assert status["regime"] == "legacy"
            assert status.get("intent_id") is None
        finally:
            service.close()

    asyncio.run(scenario())


def test_retained_legacy_history_stays_readable_but_claim_mutation_is_refused(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"README.md": "base\n"})
        service = _service(tmp_path)
        service.initialize()
        try:
            await service.handle(_request("repo.add", path=str(repository)))
            private_prompt = "retained-prompt-is-not-part-of-metadata-export"
            accepted = await service.handle(
                _request(
                    "task.run",
                    path=str(repository),
                    task_id="legacy-history",
                    title=private_prompt,
                    scopes=["README.md"],
                    claim_only=True,
                )
            )
            claim = accepted["claim"]
            await service.handle(
                _request(
                    "claim.release",
                    claim_id=claim["claim_id"],
                    reason="settled before cutover",
                )
            )
            before = await service.handle(
                _request("claim.show", claim_id=claim["claim_id"])
            )
            assert before["state"] == "released"
            await _cutover(service, repository)
            assert (
                await service.handle(
                    _request(
                        "claim.show",
                        claim_id=claim["claim_id"],
                    )
                )
                == before
            )
            shown = await service.handle(
                _request("task.show", task_id="legacy-history")
            )
            assert shown["title"] == private_prompt
            events = await service.handle(
                _request("task.events", task_id="legacy-history")
            )
            assert events
            exported = await service.handle(
                _request("workspace.cutover.export", path=str(repository))
            )
            assert any(row["task_id"] == "legacy-history" for row in exported["tasks"])
            assert any(
                row["claim_id"] == claim["claim_id"] for row in exported["claims"]
            )
            assert private_prompt not in str(exported)
            for method, params in (
                (
                    "claim.release",
                    {"claim_id": claim["claim_id"], "reason": "forbidden"},
                ),
                (
                    "claim.renew",
                    {
                        "claim_id": claim["claim_id"],
                        "task_id": "legacy-history",
                        "fencing_token": claim["fencing_token"],
                        "attempt": 1,
                    },
                ),
            ):
                with pytest.raises(LlmCoordError) as refused:
                    await service.handle(_request(method, **params))
                assert refused.value.code is ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED
            assert service.store.get_task("legacy-history").state == "cancelled"
        finally:
            service.close()

    asyncio.run(scenario())
