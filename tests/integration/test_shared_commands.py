"""Shared tasks run model-chosen commands against their pending edits."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from llm_cli.daemon.service import DaemonService
from llm_cli.execution import commands, shared
from llm_cli.execution.sandbox import SandboxPolicy, available_sandbox
from llm_cli.protocol.envelopes import Request
from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult

type Step = ModelTurn | Callable[[Sequence[ToolCallResult]], ModelTurn]


class ScriptedProvider:
    name = "scripted"

    def __init__(self, model: str, steps: Sequence[Step]) -> None:
        self.model = model
        self.steps = list(steps)
        self.history: list[object] = []
        self.offered_tools: tuple[str, ...] = ()
        self.results: list[tuple[ToolCallResult, ...]] = []

    def session(
        self,
        *,
        system: str,
        tools: Sequence[Mapping[str, object]],
        state: Mapping[str, object] | None = None,
    ) -> ScriptedProvider:
        self.offered_tools = tuple(str(tool["name"]) for tool in tools)
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
        self.history.append({"role": "tool", "results": len(results)})


def _tools(*calls: ToolCallRequest) -> ModelTurn:
    return ModelTurn(text="", tool_calls=calls, stop_reason="tool_use")


def _request(method: str, params: dict[str, Any]) -> Request:
    return Request.create(
        request_id=f"request_{method.replace('.', '_')}",
        method=method,
        params=params,
        profile_id="test",
    )


@pytest.fixture
def fake_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run commands directly so this test works where no sandbox does."""

    def passthrough(
        kind: str, policy: SandboxPolicy, argv: Sequence[str], *, cwd: Path
    ) -> list[str]:
        return list(argv)

    monkeypatch.setattr(shared, "available_sandbox", lambda: "test-sandbox")
    monkeypatch.setattr(commands, "wrap", passthrough)


def _allow_commands(service: DaemonService) -> None:
    """These tasks have no interactive session to answer approval questions."""

    assert service.shared_runner.commands is not None
    service.shared_runner.commands = replace(
        service.shared_runner.commands, approval="allow"
    )


async def _open(
    service: DaemonService, repository: Path, provider: ScriptedProvider, mode: str
) -> dict[str, str]:
    secret = "resume-secret-for-commands"
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
                "mode": mode,
            },
        )
    )
    await service.handle(
        _request(
            "session.ack", {**credentials, "sequence": opened["bootstrap_sequence"]}
        )
    )
    return credentials


async def _run_task(
    service: DaemonService,
    repository: Path,
    credentials: dict[str, str],
    task_id: str,
) -> None:
    await service.handle(
        _request(
            "task.run",
            {
                **credentials,
                "title": "check the guide",
                "path": str(repository),
                "scopes": ["docs/"],
                "task_id": task_id,
            },
        )
    )
    await asyncio.wait_for(service._background_tasks[(task_id, 1)], timeout=30)


@pytest.mark.usefixtures("fake_sandbox")
def test_command_sees_the_pending_edit_before_publication(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    seen: list[str] = []

    def after_command(results: Sequence[ToolCallResult]) -> ModelTurn:
        seen.append(results[0].content)
        return ModelTurn(text="The guide now reads 'edited'.")

    provider = ScriptedProvider(
        "commands-session",
        [
            _tools(
                ToolCallRequest("read", "read_file", {"path": "docs/guide.md"}),
                ToolCallRequest(
                    "write",
                    "write_file",
                    {"path": "docs/guide.md", "content": "edited\n"},
                ),
            ),
            _tools(
                ToolCallRequest(
                    "run", "run_command", {"argv": ["cat", "docs/guide.md"]}
                )
            ),
            after_command,
        ],
    )

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "original\n"})
        service = service_factory(tmp_path)
        _allow_commands(service)
        service.providers.register("scripted", lambda model: provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open(service, repository, provider, "normal")
        await _run_task(service, repository, credentials, "commands")

        assert not any(result.is_error for result in provider.results[0])
        assert "run_command" in provider.offered_tools
        assert seen and seen[0].startswith("exit 0 after ")
        assert "edited" in seen[0]
        # Normal mode holds the edit for review: the checkout is unchanged.
        assert (repository / "docs/guide.md").read_text() == "original\n"
        events = service.store.list_task_events("commands", limit=500)
        started = [e.payload for e in events if e.event_type == "command.started"]
        finished = [e.payload for e in events if e.event_type == "command.finished"]
        assert started[0]["argv"] == ["cat", "docs/guide.md"]
        assert started[0]["sandbox"] == "test-sandbox"
        assert finished[0]["state"] == "completed" and finished[0]["exit_code"] == 0
        assert not list((tmp_path / "data").rglob("command_*"))
        service.close()

    asyncio.run(scenario())


@pytest.mark.usefixtures("fake_sandbox")
def test_commands_are_offered_only_when_allowed_and_answerable(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    async def offered(mode: str, *, approval: str, name: str) -> tuple[str, ...]:
        provider = ScriptedProvider(name, [ModelTurn(text="answer")])
        root = tmp_path / name
        root.mkdir()
        repository = repository_factory(root, {"docs/guide.md": "original\n"})
        service = service_factory(root)
        assert service.shared_runner.commands is not None
        service.shared_runner.commands = replace(
            service.shared_runner.commands, approval=approval
        )
        service.providers.register("scripted", lambda model: provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open(service, repository, provider, mode)
        await _run_task(service, repository, credentials, "ask")
        service.close()
        return provider.offered_tools

    async def scenario() -> None:
        assert "run_command" in await offered("auto", approval="allow", name="on")
        assert "run_command" not in await offered("auto", approval="off", name="off")
        assert "run_command" not in await offered("plan", approval="allow", name="plan")
        # Asking needs an interactive session; this background task has none.
        assert "run_command" not in await offered("auto", approval="ask", name="ask")

    asyncio.run(scenario())


def test_daemon_protects_its_own_state_and_home_secrets(
    tmp_path: Path, service_factory: Callable[[Path], DaemonService]
) -> None:
    service = service_factory(tmp_path)
    settings = service.shared_runner.commands

    assert settings is not None and settings.approval == "ask"
    assert settings.snapshot_root == service.paths.data_dir / "command-snapshots"
    for path in (
        service.paths.config_dir,
        service.paths.data_dir,
        service.paths.state_dir,
        service.paths.runtime_dir,
        Path.home() / ".ssh",
    ):
        assert path in settings.protected


def test_daemon_startup_removes_leftover_command_snapshots(
    tmp_path: Path, service_factory: Callable[[Path], DaemonService]
) -> None:
    service = service_factory(tmp_path)
    leftover = service.paths.data_dir / "command-snapshots" / "command_old"
    (leftover / "source").mkdir(parents=True)

    service.initialize()

    assert not leftover.exists()
    service.close()


@pytest.mark.skipif(
    available_sandbox() is None,
    reason="no working operating-system sandbox on this machine",
)
def test_real_sandbox_hides_the_checkout_and_loupe_state(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
) -> None:
    seen: list[str] = []
    repository = repository_factory(tmp_path, {"docs/guide.md": "original\n"})
    service = service_factory(tmp_path)

    def after_command(results: Sequence[ToolCallResult]) -> ModelTurn:
        seen.extend(result.content for result in results)
        return ModelTurn(text="done")

    provider = ScriptedProvider(
        "real-sandbox-session",
        [
            _tools(
                ToolCallRequest(
                    "copy", "run_command", {"argv": ["cat", "docs/guide.md"]}
                ),
                ToolCallRequest(
                    "real",
                    "run_command",
                    {"argv": ["cat", str(repository / "docs/guide.md")]},
                ),
                ToolCallRequest(
                    "state",
                    "run_command",
                    {"argv": ["ls", str(service.paths.data_dir)]},
                ),
            ),
            after_command,
        ],
    )

    async def scenario() -> None:
        _allow_commands(service)
        service.providers.register("scripted", lambda model: provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open(service, repository, provider, "normal")
        await _run_task(service, repository, credentials, "sandboxed")
        service.close()

    asyncio.run(scenario())

    copy_result, real_result, state_result = seen
    assert copy_result.startswith("exit 0") and "original" in copy_result
    assert not real_result.startswith("exit 0")
    # Seatbelt refuses to list Loupe's data directory. Bubblewrap shows an empty
    # stand-in that holds only the mount points for this command's own copy.
    listing = state_result.splitlines()[1:]
    assert not state_result.startswith("exit 0") or listing == ["command-snapshots"]
    assert "control.sqlite3" not in state_result


async def _until(predicate: Callable[[], object], timeout: float = 10) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.02)


@pytest.mark.usefixtures("fake_sandbox")
@pytest.mark.parametrize(("answer", "ran"), [("1", True), ("3", False)])
def test_interactive_task_asks_before_each_command(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    answer: str,
    ran: bool,
) -> None:
    seen: list[ToolCallResult] = []

    def after_command(results: Sequence[ToolCallResult]) -> ModelTurn:
        seen.extend(results)
        return ModelTurn(text="done")

    provider = ScriptedProvider(
        "approval-session",
        [
            _tools(
                ToolCallRequest(
                    "run", "run_command", {"argv": ["cat", "docs/guide.md"]}
                )
            ),
            after_command,
        ],
    )

    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "original\n"})
        service = service_factory(tmp_path)
        service.providers.register("scripted", lambda model: provider)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        credentials = await _open(service, repository, provider, "normal")
        await service.handle(
            _request(
                "task.run",
                {
                    **credentials,
                    "title": "check the guide",
                    "path": str(repository),
                    "scopes": ["docs/"],
                    "task_id": "approval",
                    "interactive": True,
                },
            )
        )
        worker = service._background_tasks[("approval", 1)]
        await _until(lambda: service._task_question("approval")["pending"])
        question = service._task_question("approval")
        assert "$ cat docs/guide.md" in question["question"]
        delivered = await service.handle(
            _request(
                "task.answer",
                {
                    "task_id": "approval",
                    "question_id": question["question_id"],
                    "answer": answer,
                },
            )
        )
        assert delivered["delivered"]
        await asyncio.wait_for(worker, timeout=30)
        events = service.store.list_task_events("approval", limit=500)
        assert any(e.event_type == "command.started" for e in events) is ran
        service.close()

    asyncio.run(scenario())

    assert "run_command" in provider.offered_tools
    if ran:
        assert seen[0].content.startswith("exit 0") and "original" in seen[0].content
    else:
        assert seen[0].is_error and "declined" in seen[0].content
