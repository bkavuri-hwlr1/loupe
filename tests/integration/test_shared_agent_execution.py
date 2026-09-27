"""Scripted models exercise the shared coding harness through daemon sessions.

These are coordination checks, not measurements of real-model quality: the
same provider seam drives private edits, publication, follow-ups, and recovery
without credentials or network access.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from llm_cli.agent.harness import CodingAgentHarness, _answer_message_id
from llm_cli.agent.limits import ExecutionLimits
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import ToolBudgetExhausted
from llm_cli.coordination.models import ClaimState
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.protocol.envelopes import Request
from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult
from llm_cli.workspace.broker import WorkspaceBrokerError, load_candidate_content

type ScriptStep = ModelTurn | Callable[[Sequence[ToolCallResult]], ModelTurn]


class ScriptedProvider:
    name = "scripted"

    def __init__(self, model: str, steps: Sequence[ScriptStep]) -> None:
        self.model = model
        self.steps = list(steps)
        self.history: list[dict[str, object]] = []
        self.restored_state: Mapping[str, object] | None = None
        self.results: list[tuple[ToolCallResult, ...]] = []
        self.recorded_results: list[tuple[ToolCallResult, ...]] = []
        self.offered_tools: tuple[str, ...] = ()

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> ScriptedProvider:
        del system
        self.offered_tools = tuple(str(tool["name"]) for tool in tools)
        self.restored_state = state
        if state is not None:
            history = state.get("messages", [])
            assert isinstance(history, list)
            self.history = copy.deepcopy(history)
        return self

    def snapshot(self) -> Mapping[str, object]:
        return {"messages": copy.deepcopy(self.history)}

    def _next(self, results: Sequence[ToolCallResult] = ()) -> ModelTurn:
        assert self.steps, "the harness requested an unexpected model turn"
        step = self.steps.pop(0)
        return step(results) if callable(step) else step

    def send_user(self, text: str) -> ModelTurn:
        self.history.append({"role": "user", "content": text})
        return self._next()

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        self.results.append(tuple(results))
        self.history.append({"role": "tool", "results": len(results)})
        return self._next(results)

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        self.recorded_results.append(tuple(results))
        self.history.append({"role": "tool", "results": len(results)})


def _call(name: str, **arguments: object) -> ToolCallRequest:
    return ToolCallRequest(
        call_id=f"{name}:{arguments.get('path', '')}", name=name, arguments=arguments
    )


def _tools(*calls: ToolCallRequest) -> ModelTurn:
    return ModelTurn(text="", tool_calls=calls, stop_reason="tool_use")


def _finish(summary: str = "done") -> ModelTurn:
    return _tools(_call("finish_task", answer=summary, summary=summary))


def _request(method: str, params: dict[str, Any]) -> Request:
    return Request.create(
        request_id=f"request_{method.replace('.', '_')}",
        method=method,
        params=params,
        profile_id="test",
    )


def _register(service: DaemonService, *providers: ScriptedProvider) -> None:
    by_model = {provider.model: provider for provider in providers}
    service.providers.register("scripted", lambda model: by_model[str(model)])


async def _open_session(
    service: DaemonService, repository: Path, provider: ScriptedProvider
) -> dict[str, str]:
    secret = f"resume-secret-for-{provider.model}"
    credentials = {"session_id": provider.model, "resume_secret": secret}
    opened = await service.handle(
        _request(
            "session.open",
            {
                "session_id": credentials["session_id"],
                "path": str(repository),
                "resume_token_hash": hashlib.sha256(secret.encode()).hexdigest(),
                "provider": provider.name,
                "model": provider.model,
                "workspace": "shared",
            },
        )
    )
    await service.handle(
        _request(
            "session.ack",
            {**credentials, "sequence": opened["bootstrap_sequence"]},
        )
    )
    return credentials


async def _start(
    service: DaemonService,
    repository: Path,
    credentials: dict[str, str],
    task_id: str,
    scopes: tuple[str, ...],
    *,
    title: str | None = None,
) -> asyncio.Task[None]:
    accepted = await service.handle(
        _request(
            "task.run",
            {
                **credentials,
                "title": title or f"complete {task_id}",
                "path": str(repository),
                "scopes": list(scopes),
                "task_id": task_id,
            },
        )
    )
    assert accepted["execution"] == "scheduled"
    assert accepted["driver"] == "coding_agent"
    return service._background_tasks[(task_id, 1)]


def _assert_completed(service: DaemonService, task_id: str) -> None:
    task = service.store.get_task(task_id)
    assert task is not None
    assert task.state == "completed", service.store.list_task_events(task_id)
    claim = service.store.get_claim(str(task.current_claim_id))
    assert claim is not None and claim.state is ClaimState.RELEASED
    execution = service.store.get_execution(task_id, 1)
    assert execution is not None and execution.workspace_id is not None


def test_repository_summary_is_the_answer_without_a_finish_only_model_turn(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path,
            {
                "README.md": "# Example\n",
                "src/app.py": "print('example')\n",
            },
        )
        answer = (
            "This repository contains a small Python application. Its README "
            "documents the project and `src/app.py` is the executable source."
        )
        provider = ScriptedProvider(
            "repository-summary",
            [
                _tools(
                    _call("list_files", path="."),
                    _call("read_file", path="README.md"),
                    _call("read_file", path="src/app.py"),
                ),
                ModelTurn(text=answer, stop_reason="end_turn"),
            ],
        )
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open_session(service, repository, provider)
        background = await _start(
            service,
            repository,
            credentials,
            "summary",
            ("*",),
            title="Give me a summary of what this repo is doing",
        )
        await asyncio.wait_for(background, timeout=20)

        _assert_completed(service, "summary")
        assert not provider.steps
        assert len(provider.history) == 2
        assert len(provider.results) == 1 and len(provider.results[0]) == 3
        execution = service.store.get_execution("summary", 1)
        assert execution is not None
        accepted = service.store.get_execution_answer(execution.execution_id)
        assert accepted is not None and accepted.payload["answer"] == answer
        events = service.store.list_task_events("summary", limit=500)
        finished = [event for event in events if event.event_type == "model.finished"]
        assert "".join(str(event.payload["answer"]) for event in finished) == answer
        assert {event.payload["message_id"] for event in finished} == {
            _answer_message_id("summary", 1)
        }
        verification = [
            event.payload["status"]
            for event in events
            if event.event_type == "workflow.verification"
        ]
        assert verification == ["not_applicable"]
        assert not any(
            event.event_type in {"model.stalled", "check.started", "check.finished"}
            for event in events
        )
        service.close()

    asyncio.run(scenario())


def test_shared_agent_publishes_a_batch_and_followup_reads_its_result(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    git_run: Callable[..., str],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path, {"docs/guide.md": "base\n", "docs/index.md": "base index\n"}
        )
        service = service_factory(tmp_path)

        def finish_private_batch(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert all(not result.is_error for result in results)
            assert (repository / "docs/guide.md").read_text() == "base\n"
            assert (repository / "docs/index.md").read_text() == "base index\n"
            return _finish("published both documents")

        def update_followup(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert len(results) == 1 and not results[0].is_error
            assert results[0].content == "first result\n"
            assert provider.restored_state is not None
            return _tools(
                _call("write_file", path="docs/guide.md", content="followup result\n")
            )

        provider = ScriptedProvider(
            "followup",
            [
                _tools(
                    _call("read_file", path="docs/guide.md"),
                    _call("read_file", path="docs/index.md"),
                    _call("write_file", path="docs/guide.md", content="first result\n"),
                    _call("write_file", path="docs/index.md", content="new index\n"),
                ),
                finish_private_batch,
                _tools(_call("read_file", path="docs/guide.md")),
                update_followup,
                _finish(),
            ],
        )
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open_session(service, repository, provider)
        refs = git_run(repository, "show-ref")
        index = git_run(repository, "write-tree")

        first = await _start(service, repository, credentials, "first", ("docs/",))
        await asyncio.wait_for(first, timeout=20)
        _assert_completed(service, "first")
        assert (repository / "docs/guide.md").read_text() == "first result\n"
        assert (repository / "docs/index.md").read_text() == "new index\n"
        second = await _start(service, repository, credentials, "second", ("docs/",))
        await asyncio.wait_for(second, timeout=20)
        _assert_completed(service, "second")
        assert (repository / "docs/guide.md").read_text() == "followup result\n"
        assert git_run(repository, "show-ref") == refs
        assert git_run(repository, "write-tree") == index
        assert "run_command" not in provider.offered_tools
        with service.store.connection() as connection:
            batches = connection.execute(
                "SELECT state, workspace_revision FROM workspace_batches "
                "ORDER BY workspace_revision"
            ).fetchall()
        assert [(row["state"], row["workspace_revision"]) for row in batches] == [
            ("published", 1),
            ("published", 2),
        ]
        service.close()

    asyncio.run(scenario())


def test_disjoint_shared_agents_overlap_and_the_combined_program_passes(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    git_run: Callable[..., str],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path,
            {
                "left/calc.py": "def value():\n    return 0\n",
                "left/constants.py": "VALUE = 0\n",
                "right/calc.py": "def value():\n    return 0\n",
            },
        )
        service = service_factory(tmp_path)
        both_staged = threading.Barrier(2)

        def finish_together(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert results and all(not result.is_error for result in results)
            # A blocked model turn must not hold the publication barrier or
            # the daemon authority lock: the second session must reach here.
            both_staged.wait(timeout=10)
            return _finish()

        left = ScriptedProvider(
            "left-agent",
            [
                _tools(
                    _call("read_file", path="left/calc.py"),
                    _call("read_file", path="left/constants.py"),
                    _call(
                        "write_file",
                        path="left/calc.py",
                        content="from .constants import VALUE\n\ndef value():\n"
                        "    return VALUE\n",
                    ),
                    _call(
                        "write_file", path="left/constants.py", content="VALUE = 40\n"
                    ),
                ),
                finish_together,
            ],
        )
        right = ScriptedProvider(
            "right-agent",
            [
                _tools(
                    _call("read_file", path="right/calc.py"),
                    _call(
                        "write_file",
                        path="right/calc.py",
                        content="def value():\n    return 2\n",
                    ),
                ),
                finish_together,
            ],
        )
        _register(service, left, right)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        first_session = await _open_session(service, repository, left)
        second_session = await _open_session(service, repository, right)
        refs = git_run(repository, "show-ref")
        index = git_run(repository, "write-tree")
        first = await _start(service, repository, first_session, "left", ("left/",))
        second = await _start(service, repository, second_session, "right", ("right/",))
        await asyncio.wait_for(asyncio.gather(first, second), timeout=20)

        _assert_completed(service, "left")
        _assert_completed(service, "right")
        subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "from left.calc import value as left; "
                "from right.calc import value as right; assert left() + right() == 42",
            ],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
        assert git_run(repository, "show-ref") == refs
        assert git_run(repository, "write-tree") == index
        service.close()

    asyncio.run(scenario())


def test_one_stale_base_preserves_the_entire_agent_batch_without_publishing(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path, {"docs/a.md": "base a\n", "docs/b.md": "base b\n"}
        )
        service = service_factory(tmp_path)

        def edit_between_stage_and_finish(
            results: Sequence[ToolCallResult],
        ) -> ModelTurn:
            assert all(not result.is_error for result in results)
            assert (repository / "docs/a.md").read_text() == "base a\n"
            assert (repository / "docs/b.md").read_text() == "base b\n"
            replacement = repository / "editor-save.tmp"
            replacement.write_text("external b\n", encoding="utf-8")
            replacement.replace(repository / "docs/b.md")
            return _finish()

        provider = ScriptedProvider(
            "stale-agent",
            [
                _tools(
                    _call("read_file", path="docs/a.md"),
                    _call("read_file", path="docs/b.md"),
                    _call("write_file", path="docs/a.md", content="candidate a\n"),
                    _call("write_file", path="docs/b.md", content="candidate b\n"),
                ),
                edit_between_stage_and_finish,
            ],
        )
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open_session(service, repository, provider)
        background = await _start(service, repository, credentials, "stale", ("docs/",))
        await asyncio.wait_for(background, timeout=20)

        task = service.store.get_task("stale")
        assert task is not None and task.state == "failed"
        assert (repository / "docs/a.md").read_text() == "base a\n"
        assert (repository / "docs/b.md").read_text() == "external b\n"
        execution = service.store.get_execution("stale", 1)
        assert execution is not None
        assert execution.failure_code == ErrorCode.PATH_BASE_MISMATCH
        with service.store.connection() as connection:
            batch = connection.execute(
                "SELECT * FROM workspace_batches WHERE batch_id = ?",
                (execution.execution_id,),
            ).fetchone()
        assert batch is not None and batch["state"] == "diverged"
        assert batch["workspace_revision"] is None
        retained = {
            item["relative_path"]: load_candidate_content(
                service.paths.candidate_dir, item["content_hash"]
            )
            for item in json.loads(batch["request_json"])["files"]
        }
        assert retained == {
            "docs/a.md": b"candidate a\n",
            "docs/b.md": b"candidate b\n",
        }
        service.close()

    asyncio.run(scenario())


def test_a_session_cannot_run_a_second_prompt_while_its_first_is_active(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        service = service_factory(tmp_path)
        started = threading.Event()
        release = threading.Event()

        def block_model(results: Sequence[ToolCallResult]) -> ModelTurn:
            del results
            started.set()
            assert release.wait(timeout=10), "the test did not release the model"
            return _tools(
                _call("read_file", path="docs/guide.md"),
                _call("write_file", path="docs/guide.md", content="first result\n"),
            )

        provider = ScriptedProvider("busy-session", [block_model, _finish()])
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open_session(service, repository, provider)
        first = await _start(service, repository, credentials, "first", ("docs/",))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            with pytest.raises(LlmCoordError) as refusal:
                await service.handle(
                    _request(
                        "task.run",
                        {
                            **credentials,
                            "title": "a conflicting conversation continuation",
                            "path": str(repository),
                            "scopes": ["other/"],
                            "task_id": "second",
                        },
                    )
                )
            assert refusal.value.code is ErrorCode.TASK_NOT_MUTABLE
            assert service.store.get_task("second") is None
        finally:
            release.set()
            await asyncio.wait_for(first, timeout=15)
        _assert_completed(service, "first")
        service.close()

    asyncio.run(scenario())


def test_restart_restores_private_files_without_replaying_completed_writes(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    git_run: Callable[..., str],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(
            tmp_path, {"docs/a.md": "base a\n", "docs/b.md": "base b\n"}
        )
        dead = service_factory(tmp_path)

        def crash_after_completed_writes(
            results: Sequence[ToolCallResult],
        ) -> ModelTurn:
            assert len(results) == 4 and all(not result.is_error for result in results)
            raise KeyboardInterrupt

        first = ScriptedProvider(
            "resumable",
            [
                _tools(
                    _call("read_file", path="docs/a.md"),
                    _call("read_file", path="docs/b.md"),
                    _call("write_file", path="docs/a.md", content="resumed a\n"),
                    _call("write_file", path="docs/b.md", content="resumed b\n"),
                ),
                crash_after_completed_writes,
            ],
        )
        _register(dead, first)
        dead.initialize()
        await dead.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open_session(dead, repository, first)
        registered = dead.store.list_repositories()[0]
        task = dead.store.create_task(
            repository_id=registered.repository_id,
            session_id=credentials["session_id"],
            task_id="resume-private-batch",
            title="resume private files",
        )
        claim = dead.coordinator.request_claim(task.task_id, ["docs/"])
        dead.store.save_execution_launch(
            task_id=task.task_id,
            attempt=task.attempt,
            driver="coding_agent",
            instructions=task.title,
            interactive=False,
            parameters={"provider": first.name, "model": first.model},
        )
        refs = git_run(repository, "show-ref")
        index = git_run(repository, "write-tree")
        # Calling the actual service dispatch synchronously lets the simulated
        # process death bypass worker cleanup, preserving a real harness checkpoint.
        with pytest.raises(KeyboardInterrupt):
            dead._execute(
                repository=registered,
                task=task,
                claim=claim,
                driver=CodingAgentHarness(first),
                instructions=task.title,
            )
        abandoned = dead.store.get_execution(task.task_id, 1)
        assert abandoned is not None and abandoned.workspace_id is not None
        checkpoint = dead.store.get_execution_checkpoint(abandoned.execution_id)
        assert checkpoint is not None
        assert checkpoint.checkpoint["phase"] == "tool_results"
        assert len(checkpoint.checkpoint["tool_results"]) == 4
        assert (repository / "docs/a.md").read_text() == "base a\n"
        assert (repository / "docs/b.md").read_text() == "base b\n"
        dead.close()

        def finish_resumed(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert len(results) == 4 and all(not result.is_error for result in results)
            return _finish("resumed the private batch")

        resumed = ScriptedProvider("resumable", [finish_resumed])
        live = DaemonService(
            dead.paths,
            dead.settings,
            asyncio.Event(),
            boot_id="boot-resumed-shared-agent",
        )
        _register(live, resumed)
        live.initialize()
        assert live.startup_recovery is not None
        assert live.startup_recovery.summary == {"resuming": 1}
        background = live._background_tasks[(task.task_id, 1)]
        await asyncio.wait_for(background, timeout=20)

        _assert_completed(live, task.task_id)
        assert resumed.restored_state is not None
        assert len(resumed.results) == 1
        assert (repository / "docs/a.md").read_text() == "resumed a\n"
        assert (repository / "docs/b.md").read_text() == "resumed b\n"
        writes = [
            event
            for event in live.store.list_task_events(task.task_id)
            if event.event_type == "tool.called"
            and event.payload.get("tool") == "write_file"
        ]
        assert len(writes) == 2
        assert git_run(repository, "show-ref") == refs
        assert git_run(repository, "write-tree") == index
        with live.store.connection() as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM workspace_batches WHERE state = 'published'"
                ).fetchone()[0]
                == 1
            )
        live.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "write_same_content", [False, True], ids=["read-only", "no-op"]
)
def test_unchanged_task_completes_without_publication_and_promotes_conversation_once(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    write_same_content: bool,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        service = service_factory(tmp_path)
        calls = [_call("read_file", path="docs/guide.md")]
        if write_same_content:
            calls.append(_call("write_file", path="docs/guide.md", content="base\n"))
        provider = ScriptedProvider("no-changes", [_tools(*calls), _finish()])
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open_session(service, repository, provider)
        background = await _start(
            service, repository, credentials, "unchanged", ("docs/",)
        )
        await asyncio.wait_for(background, timeout=20)

        _assert_completed(service, "unchanged")
        assert provider.results and all(
            not result.is_error for result in provider.results[0]
        )
        execution = service.store.get_execution("unchanged", 1)
        assert execution is not None
        session = service.store.get_session(credentials["session_id"])
        assert session is not None and session.conversation_revision == 1
        workspace = service.store.get_workspace(str(session.workspace_id))
        assert workspace is not None and workspace.workspace_revision == 0
        assert service.store.session_conversation(session.session_id) is not None
        assert (repository / "docs/guide.md").read_text() == "base\n"
        with service.store.connection() as connection:
            assert (
                connection.execute("SELECT count(*) FROM workspace_batches").fetchone()[
                    0
                ]
                == 0
            )

        # Both duplicate settlement and ordinary recovery must be harmless.
        service.coordinator.settle_shared_execution(
            execution.execution_id, outcome="no_changes"
        )
        report = await service.handle(_request("task.recover", {}))
        assert report["outcomes"] == []
        repeated = service.store.get_session(session.session_id)
        assert repeated is not None and repeated.conversation_revision == 1
        service.close()

    asyncio.run(scenario())


def test_incomplete_read_only_answer_is_promoted_for_the_next_prompt(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"README.md": "base\n"})
        provider = ScriptedProvider(
            "incomplete-conversation",
            [ModelTurn("A useful but unfinished answer.", stop_reason="max_tokens")],
        )
        service = service_factory(tmp_path)
        _register(service, provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open_session(service, repository, provider)

        first = await _start(
            service, repository, credentials, "incomplete-answer", ("*",)
        )
        await asyncio.wait_for(first, timeout=20)
        task = service.store.get_task("incomplete-answer")
        execution = service.store.get_execution("incomplete-answer", 1)
        session = service.store.get_session(credentials["session_id"])
        assert task is not None and task.state == "failed"
        assert execution is not None
        assert execution.failure_code == "RESPONSE_INCOMPLETE"
        assert session is not None and session.conversation_revision == 1
        saved = service.store.session_conversation(session.session_id)
        assert saved is not None
        assert saved[2]["session"] == provider.snapshot()

        provider.steps.append(ModelTurn("A complete follow-up answer."))
        second = await _start(
            service, repository, credentials, "complete-follow-up", ("*",)
        )
        await asyncio.wait_for(second, timeout=20)
        _assert_completed(service, "complete-follow-up")
        assert provider.restored_state == saved[2]["session"]
        assert (repository / "README.md").read_text() == "base\n"
        service.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("disposition", ["cancelled", "expired", "retried"])
def test_restart_terminalizes_an_abandoned_execution_without_rewriting_its_task(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    disposition: str,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        dead = service_factory(tmp_path)

        def interrupt(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert all(not result.is_error for result in results)
            raise KeyboardInterrupt

        provider = ScriptedProvider(
            "cancelled-session",
            [
                _tools(
                    _call("read_file", path="docs/guide.md"),
                    _call("write_file", path="docs/guide.md", content="private edit\n"),
                ),
                interrupt,
            ],
        )
        _register(dead, provider)
        dead.initialize()
        await dead.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open_session(dead, repository, provider)
        registered = dead.store.list_repositories()[0]
        task = dead.store.create_task(
            repository_id=registered.repository_id,
            session_id=credentials["session_id"],
            task_id="abandoned",
            title="abandoned private edit",
        )
        claim = dead.coordinator.request_claim(task.task_id, ["docs/"])
        dead.store.save_execution_launch(
            task_id=task.task_id,
            attempt=task.attempt,
            driver="coding_agent",
            instructions=task.title,
            interactive=False,
            parameters={"provider": provider.name, "model": provider.model},
        )
        with pytest.raises(KeyboardInterrupt):
            dead._execute(
                repository=registered,
                task=task,
                claim=claim,
                driver=CodingAgentHarness(provider),
                instructions=task.title,
            )
        if disposition == "expired":
            fresh_claim = dead.store.get_claim(claim.claim_id)
            assert fresh_claim is not None and fresh_claim.lease_expires_at is not None
            dead.coordinator.reconcile_expired(now=fresh_claim.lease_expires_at + 1)
        else:
            dead.coordinator.release_claim(claim.claim_id, reason="operator_cancelled")
            if disposition == "retried":
                dead.store.begin_new_attempt(task.task_id)
        preserved_task = dead.store.get_task(task.task_id)
        assert preserved_task is not None
        assert (
            preserved_task.state
            == {"cancelled": "cancelled", "expired": "failed", "retried": "queued"}[
                disposition
            ]
        )
        dead.close()

        unused_provider = ScriptedProvider(provider.model, [])
        live = DaemonService(
            dead.paths, dead.settings, asyncio.Event(), boot_id="boot-after-cancel"
        )
        _register(live, unused_provider)
        live.initialize()
        assert live.startup_recovery is not None
        assert live.startup_recovery.summary == {"failed_safe": 1}
        execution = live.store.get_execution(task.task_id, 1)
        assert execution is not None and execution.state == "failed"
        assert live.store.get_task(task.task_id) == preserved_task
        assert (repository / "docs/guide.md").read_text() == "base\n"
        assert unused_provider.restored_state is None
        for _ in range(2):
            assert (await live.handle(_request("task.recover", {})))["outcomes"] == []
            assert live.store.get_task(task.task_id) == preserved_task
        session = live.store.get_session(credentials["session_id"])
        assert session is not None and session.conversation_revision == 0
        assert not live._background_tasks
        live.close()

    asyncio.run(scenario())


def test_restart_settles_a_published_journal_and_promotes_conversation_exactly_once(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        dead = service_factory(tmp_path)
        provider = ScriptedProvider(
            "published-before-death",
            [
                _tools(
                    _call("read_file", path="docs/guide.md"),
                    _call("write_file", path="docs/guide.md", content="published\n"),
                ),
                _finish("finished before the crash"),
            ],
        )
        _register(dead, provider)
        dead.initialize()
        await dead.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open_session(dead, repository, provider)
        registered = dead.store.list_repositories()[0]
        task = dead.store.create_task(
            repository_id=registered.repository_id,
            session_id=credentials["session_id"],
            task_id="published-before-settlement",
            title="publish before settlement",
        )
        claim = dead.coordinator.request_claim(task.task_id, ["docs/"])
        dead.store.save_execution_launch(
            task_id=task.task_id,
            attempt=task.attempt,
            driver="coding_agent",
            instructions=task.title,
            interactive=False,
            parameters={"provider": provider.name, "model": provider.model},
        )

        def crash_before_settlement(*args: object, **kwargs: object) -> None:
            raise KeyboardInterrupt

        with monkeypatch.context() as patch:
            patch.setattr(
                dead.coordinator, "settle_shared_execution", crash_before_settlement
            )
            with pytest.raises(KeyboardInterrupt):
                dead._execute(
                    repository=registered,
                    task=task,
                    claim=claim,
                    driver=CodingAgentHarness(provider),
                    instructions=task.title,
                )
        execution = dead.store.get_execution(task.task_id, 1)
        assert execution is not None and execution.state == "publishing"
        batch = dead.shared_runner.publisher.get(execution.execution_id)
        assert batch is not None and batch.state == "published"
        session = dead.store.get_session(credentials["session_id"])
        assert session is not None and session.conversation_revision == 0
        assert (repository / "docs/guide.md").read_text() == "published\n"
        dead.close()

        unused_provider = ScriptedProvider(provider.model, [])
        live = DaemonService(
            dead.paths, dead.settings, asyncio.Event(), boot_id="boot-after-publish"
        )
        _register(live, unused_provider)
        live.initialize()
        assert live.startup_recovery is not None
        assert live.startup_recovery.summary == {"confirmed": 1}
        _assert_completed(live, task.task_id)
        session = live.store.get_session(credentials["session_id"])
        assert session is not None and session.conversation_revision == 1
        conversation = live.store.session_conversation(session.session_id)
        assert conversation is not None
        assert conversation[2]["session"] == provider.snapshot()
        for _ in range(2):
            assert (await live.handle(_request("task.recover", {})))["outcomes"] == []
            live.coordinator.settle_shared_execution(
                execution.execution_id, outcome="published"
            )
            session = live.store.get_session(credentials["session_id"])
            assert session is not None and session.conversation_revision == 1
        workspace = live.store.get_workspace(str(session.workspace_id))
        assert workspace is not None and workspace.workspace_revision == 1
        assert (
            len(
                [
                    event
                    for event in live.store.list_task_events(task.task_id)
                    if event.event_type == "execution.published"
                ]
            )
            == 1
        )
        assert unused_provider.restored_state is None
        assert not live._background_tasks
        live.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("error_kind", ["workspace", "recovery"])
def test_shared_guard_refusals_exhaust_the_tool_budget(
    tmp_path: Path, error_kind: str
) -> None:
    def refuse() -> None:
        if error_kind == "workspace":
            raise WorkspaceBrokerError("the shared workspace is unavailable")
        raise LlmCoordError(
            ErrorCode.CHECKOUT_RECOVERY_REQUIRED, "recovery is required"
        )

    broker = SharedToolBroker(
        worktree=tmp_path,
        scopes=("docs/",),
        limits=ExecutionLimits(max_tool_calls=2),
        guard=refuse,
    )
    for expected_calls in (1, 2):
        response = broker.invoke("read_file", {"path": "docs/guide.md"})
        assert response.is_error
        assert broker.usage.calls == expected_calls
    with pytest.raises(ToolBudgetExhausted, match="2 tool-call budget"):
        broker.invoke("read_file", {"path": "docs/guide.md"})
    assert broker.usage.calls == 2
    assert broker.candidates() == ()
