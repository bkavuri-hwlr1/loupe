"""The user-facing edit/check/review/undo loop against real Git repositories."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from test_shared_agent_execution import (
    ScriptedProvider,
    _assert_completed,
    _call,
    _finish,
    _open_session,
    _register,
    _request,
    _start,
    _tools,
)

from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.execution.checks import digest, verification_status
from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult
from llm_cli.workspace.workflow import decode_files


async def setup(
    service: DaemonService,
    root: Path,
    provider: ScriptedProvider,
    *,
    review: bool = False,
    code: str | None = None,
    required: bool = True,
    timeout: int = 10,
) -> dict[str, str]:
    _register(service, provider)
    service.initialize()
    await service.handle(_request("repo.add", {"path": str(root)}))
    credentials = await _open_session(service, root, provider)
    if review:
        await service.handle(
            _request("session.set_mode", {**credentials, "mode": "normal"})
        )
    if code is not None:
        await service.handle(
            _request(
                "checks.configure",
                {
                    "path": str(root),
                    "config": {
                        "checks": {
                            "test": {
                                "argv": [sys.executable, "-c", code],
                                "required": required,
                                "timeout": timeout,
                            }
                        }
                    },
                },
            )
        )
    return credentials


def edit_provider(*steps: Any) -> ScriptedProvider:
    return ScriptedProvider(
        "workflow-session",
        [
            _tools(
                _call("read_file", path="a.py"),
                _call("write_file", path="a.py", content="value = 2\n"),
            ),
            *steps,
        ],
    )


def test_complete_file_operations_check_diff_and_undo(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    git_run: Callable[..., str],
) -> None:
    async def scenario() -> None:
        root = repository_factory(
            tmp_path, {"a.py": "value = 1\n", "old.txt": "obsolete\n"}
        )
        before = git_run(root, "show-ref"), git_run(root, "write-tree")
        service = service_factory(tmp_path)

        def finish(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert all(not r.is_error for r in results), results
            assert not (root / "pkg").exists()
            assert (root / "old.txt").exists()
            return _finish()

        provider = ScriptedProvider(
            "workflow-session",
            [
                _tools(
                    _call("rename_file", source="a.py", destination="pkg/new.py"),
                    _call("delete_file", path="old.txt"),
                    _call(
                        "write_file",
                        path="pkg/test_new.py",
                        content="from new import value\nassert value == 1\n",
                    ),
                ),
                finish,
            ],
        )
        creds = await setup(
            service,
            root,
            provider,
            code=(
                "import runpy; runpy.run_path('pkg/new.py'); assert not "
                "__import__('pathlib').Path('old.txt').exists(); print('"
                "verified')"
            ),
        )
        await await_task(service, root, creds)
        _assert_completed(service, "task")
        assert not (root / "a.py").exists()
        assert not (root / "old.txt").exists()
        assert (root / "pkg/new.py").read_text() == "value = 1\n"
        view = await service.handle(_request("task.diff", {"task_id": "task"}))
        assert "pkg/new.py" in view["diff"] and "old.txt" in view["diff"]
        assert view["checks"][0]["state"] == "passed"
        undo = await service.handle(_request("task.undo", {"task_id": "task"}))
        assert undo["state"] == "published"
        assert (root / "a.py").read_text() == "value = 1\n"
        assert (root / "old.txt").read_text() == "obsolete\n"
        assert not (root / "pkg").exists()
        again = await service.handle(_request("task.undo", {"task_id": "task"}))
        assert again["task_id"] == undo["task_id"]
        assert (git_run(root, "show-ref"), git_run(root, "write-tree")) == before

    asyncio.run(scenario())


async def await_task(
    service: DaemonService, root: Path, creds: dict[str, str], task_id: str = "task"
) -> None:
    work = await _start(service, root, creds, task_id, ("*",))
    await asyncio.wait_for(work, 30)


def test_review_can_apply_and_undo_refuses_external_changes(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        provider = edit_provider(_finish())
        creds = await setup(service, root, provider, review=True)
        await await_task(service, root, creds)
        assert (root / "a.py").read_text() == "value = 1\n"
        assert service._task_view("task")["state"] == "awaiting_review"
        applied = await service.handle(_request("task.apply", {"task_id": "task"}))
        assert applied["state"] == "published"
        assert (root / "a.py").read_text() == "value = 2\n"
        (root / "a.py").write_text("external edit\n")
        with pytest.raises(LlmCoordError, match="Conflict"):
            await service.handle(_request("task.undo", {"task_id": "task"}))
        assert (root / "a.py").read_text() == "external edit\n"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "code,expected",
    [
        ("raise SystemExit(3)", "failed"),
        ("import time; time.sleep(20)", "timed_out"),
        (
            "from pathlib import Path; Path('a.py').write_text('mutation')",
            "source_mutated",
        ),
    ],
)
def test_failed_checks_retain_proposal(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    code: str,
    expected: str,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        provider = edit_provider(_finish())
        creds = await setup(service, root, provider, code=code, timeout=1)
        await await_task(service, root, creds)
        assert service._task_view("task")["state"] == "awaiting_review"
        assert (root / "a.py").read_text() == "value = 1\n"
        assert service.workflow.checks("task")[0]["state"] == expected
        with pytest.raises(LlmCoordError, match="Required checks"):
            await service.handle(_request("task.apply", {"task_id": "task"}))
        applied = await service.handle(
            _request("task.apply", {"task_id": "task", "allow_unverified": True})
        )
        assert applied["state"] == "published"
        assert (root / "a.py").read_text() == "value = 2\n"

    asyncio.run(scenario())


def test_cancel_running_check_preserves_checkout(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        provider = edit_provider(_finish())
        creds = await setup(
            service,
            root,
            provider,
            code="import time; print('running', flush=True); time.sleep(60)",
            timeout=60,
        )
        work = await _start(service, root, creds, "task", ("*",))
        for _ in range(150):
            events = service.store.list_task_events("task")
            if any(e.event_type == "check.output" for e in events):
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("check did not start")
        service.initialize()
        assert service.workflow.checks("task")[0]["state"] == "running"
        assert (await service.handle(_request("task.cancel", {"task_id": "task"})))[
            "state"
        ] == "stopping"
        await asyncio.wait_for(work, 10)
        assert service._task_view("task")["state"] == "cancelled"
        assert (root / "a.py").read_text() == "value = 1\n"
        assert service.workflow.inspect("task")["paths"] == ["a.py"]
        assert service.workflow.checks("task")[0]["state"] == "cancelled"
        workflow = service.workflow.get("task")
        assert workflow is not None
        proposal = workflow["proposal_json"]
        assert isinstance(proposal, str)
        assert (
            verification_status(service.store, workflow, root, decode_files(proposal))
            == "interrupted"
        )
        config = json.loads(workflow["config_json"])
        test_spec = config["checks"]["test"]
        config["checks"] = {"aaa_missing": test_spec, "test": test_spec}
        multi_check_workflow = {
            **workflow,
            "config_json": json.dumps(config, sort_keys=True),
        }
        with service.store.connection() as connection:
            connection.execute(
                "UPDATE check_runs SET config_digest=? WHERE task_id=?",
                (digest(multi_check_workflow["config_json"]), "task"),
            )
        files = decode_files(proposal)
        assert (
            verification_status(service.store, multi_check_workflow, root, files)
            == "interrupted"
        )
        with service.store.connection() as connection:
            connection.execute(
                "UPDATE check_runs SET state='failed' WHERE task_id=?", ("task",)
            )
        assert (
            verification_status(service.store, multi_check_workflow, root, files)
            == "failed"
        )

    asyncio.run(scenario())


def test_source_baseline_change_invalidates_check_evidence(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(
            tmp_path, {"a.py": "value = 1\n", "dependency.py": "base\n"}
        )
        service = service_factory(tmp_path)
        creds = await setup(
            service,
            root,
            edit_provider(_finish()),
            review=True,
            code=(
                "assert __import__('pathlib').Path('a.py').read_text() ="
                "= 'value = 2\\n'"
            ),
        )
        await await_task(service, root, creds)
        assert service.workflow.checks("task")[0]["state"] == "passed"
        (root / "dependency.py").write_text("new dependency\n")
        with pytest.raises(LlmCoordError, match="stale"):
            await service.handle(_request("task.apply", {"task_id": "task"}))
        assert (root / "a.py").read_text() == "value = 1\n"

    asyncio.run(scenario())


def test_required_check_failure_can_be_repaired(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)

        def repair(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert results[0].is_error
            assert results[0].content.endswith(
                "[test failed. Fix the cause and run it again; 3 repair attempts left.]"
            )
            return _tools(_call("write_file", path="a.py", content="value = 3\n"))

        provider = edit_provider(_finish(), repair, _finish())
        creds = await setup(
            service,
            root,
            provider,
            code=(
                "assert __import__('pathlib').Path('a.py').read_text() ="
                "= 'value = 3\\n'"
            ),
        )
        await await_task(service, root, creds)
        _assert_completed(service, "task")
        assert [x["state"] for x in service.workflow.checks("task")] == [
            "failed",
            "passed",
        ]
        assert (root / "a.py").read_text() == "value = 3\n"

    asyncio.run(scenario())


def _attempt(value: int, *expected: str) -> Callable[..., ModelTurn]:
    """Check the last finish was refused with ``expected``, then edit and retry."""

    def step(results: Sequence[ToolCallResult]) -> ModelTurn:
        assert results[-1].is_error
        for text in expected:
            assert text in results[-1].content
        return _tools(
            _call("write_file", path="a.py", content=f"value = {value}\n"),
            _call("finish_task", answer="fixed"),
        )

    return step


def _settled_outcome(service: DaemonService) -> object:
    (held,) = [
        event
        for event in service.store.list_task_events("task")
        if event.event_type == "workflow.awaiting_review"
    ]
    return held.payload["completion_outcome"]


@pytest.mark.parametrize(
    ("code", "failures", "reason"),
    [
        # Each edit changes the output, so all three repair attempts are used.
        (
            "import pathlib, sys; v = pathlib.Path('a.py').read_text(); "
            "print('got', v); sys.exit(v != 'value = 3\\n')",
            4,
            "after 3 repair attempts",
        ),
        # The edits never change the failure, so the repair stops sooner.
        (
            "assert __import__('pathlib').Path('a.py').read_text() == 'value = 3\\n'",
            3,
            "with the same failure the last 3 times",
        ),
    ],
)
def test_exhausted_repairs_finish_partial_with_edits_kept(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    code: str,
    failures: int,
    reason: str,
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        attempts = [
            _attempt(4, "3 repair attempts left"),
            _attempt(5, "Repair attempt 1 of 3 for test failed", "2 repair attempts"),
            _attempt(6, "Repair attempt 2 of 3 for test failed", "1 repair attempt"),
        ][: failures - 1]

        def stopped(results: Sequence[ToolCallResult]) -> ModelTurn:
            assert (
                f"Repair limit reached: test failed {failures} times in a row, "
                f"{reason}." in results[-1].content
            )
            return _tools(
                _call("write_file", path="a.py", content="value = 3\n"),
                ToolCallRequest("run_check:test", "run_check", {"name": "test"}),
                _call("finish_task", answer="value is still wrong"),
            )

        def finished(results: Sequence[ToolCallResult]) -> ModelTurn:
            raise AssertionError("finish_task should have ended the task")

        provider = edit_provider(_finish(), *attempts, stopped, finished)
        creds = await setup(service, root, provider, code=code)
        await await_task(service, root, creds)

        write, check, finish = provider.recorded_results[-1]
        assert write.is_error and "repair limit for test was reached" in write.content
        assert check.is_error and "repair limit" in check.content
        assert not finish.is_error
        assert finish.content == (
            "task marked finished as partial: test still fails after the repair limit"
        )
        assert service._task_view("task")["state"] == "awaiting_review"
        assert _settled_outcome(service) == "partial"
        assert [run["state"] for run in service.workflow.checks("task")] == [
            "failed"
        ] * failures
        (exhausted,) = [
            event.payload
            for event in service.store.list_task_events("task")
            if event.event_type == "repair.exhausted"
        ]
        assert exhausted == {
            "check": "test",
            "failures": failures,
            "unchanged": failures == 3,
        }
        # The checkout is untouched; the last edit before the limit is kept.
        assert (root / "a.py").read_text() == "value = 1\n"
        workflow = service.workflow.get("task")
        assert workflow is not None
        (kept,) = decode_files(workflow["proposal_json"])
        assert kept.content == f"value = {6 if failures == 4 else 5}\n".encode()

    asyncio.run(scenario())


def test_dirty_untracked_and_runtime_inputs_are_private(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(
            tmp_path,
            {
                "a.py": "value = 1\n",
                "dependency.txt": "committed",
                ".gitignore": "runtime/\n",
            },
        )
        (root / "dependency.txt").write_text("dirty")
        (root / "fixture.txt").write_text("untracked")
        (root / "runtime").mkdir()
        (root / "runtime/input.txt").write_text("copied")
        service = service_factory(tmp_path)
        provider = edit_provider(_finish())
        creds = await setup(service, root, provider, review=True)
        code = (
            "from pathlib import Path; "
            "assert Path('dependency.txt').read_text() == 'dirty'; "
            "assert Path('fixture.txt').read_text() == 'untracked'; "
            "assert Path('runtime/input.txt').read_text() == 'copied'; "
            "assert Path('runtime/setup.txt').read_text() == 'setup'; "
            "Path('runtime/input.txt').write_text('private')"
        )
        await service.handle(
            _request(
                "checks.configure",
                {
                    "path": str(root),
                    "config": {
                        "runtime_paths": ["runtime"],
                        "setup": [
                            [
                                sys.executable,
                                "-c",
                                (
                                    "from pathlib import Path; "
                                    "Path('runtime/setup.txt').write_text('setup')"
                                ),
                            ]
                        ],
                        "checks": {"test": {"argv": [sys.executable, "-c", code]}},
                    },
                },
            )
        )
        await await_task(service, root, creds)
        assert service.workflow.checks("task")[0]["state"] == "passed"
        assert (root / "runtime/input.txt").read_text() == "copied"
        assert not (root / "runtime/setup.txt").exists()
        (root / "runtime/input.txt").write_text("changed dependency")
        with pytest.raises(LlmCoordError, match="stale"):
            await service.handle(_request("task.apply", {"task_id": "task"}))

    asyncio.run(scenario())


def test_review_survives_closed_session_and_does_not_block_followup(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        provider = edit_provider(_finish(), _finish("no edits"))
        creds = await setup(service, root, provider, review=True)
        await await_task(service, root, creds)
        await await_task(service, root, creds, "followup")
        _assert_completed(service, "followup")
        service.store.close_session(creds["session_id"])
        assert (await service.handle(_request("task.apply", {"task_id": "task"})))[
            "state"
        ] == "published"
        assert (root / "a.py").read_text() == "value = 2\n"

    asyncio.run(scenario())


def test_retry_refuses_a_conversation_task_and_keeps_its_proposal(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        provider = edit_provider(_finish(), _finish("no edits"))
        creds = await setup(service, root, provider, review=True)
        await await_task(service, root, creds)
        before = service.store.get_task("task")

        with pytest.raises(LlmCoordError, match="cannot be retried") as refused:
            await service.handle(_request("task.retry", {"task_id": "task"}))

        assert refused.value.code is ErrorCode.TASK_NOT_MUTABLE
        assert service.store.get_task("task") == before
        # The conversation still takes prompts, and the proposal still applies.
        await await_task(service, root, creds, "followup")
        _assert_completed(service, "followup")
        applied = await service.handle(_request("task.apply", {"task_id": "task"}))
        assert applied["state"] == "published"
        assert (root / "a.py").read_text() == "value = 2\n"

    asyncio.run(scenario())


def test_retry_opens_a_new_attempt_for_a_task_outside_conversations(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(root)}))
        registered = service.store.list_repositories()[0]
        task = service.store.create_task(
            repository_id=registered.repository_id, task_id="solo", title="solo"
        )
        claim = service.coordinator.request_claim(task.task_id, ["a.py"])
        service.coordinator.release_claim(claim.claim_id, reason="finished")

        retried = await service.handle(_request("task.retry", {"task_id": "solo"}))

        assert retried["attempt"] == 2 and retried["state"] == "queued"

    asyncio.run(scenario())


def test_advisory_failure_does_not_gate_publication(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        provider = edit_provider(
            _tools(
                ToolCallRequest(
                    call_id="check", name="run_check", arguments={"name": "test"}
                )
            ),
            _finish(),
        )
        creds = await setup(
            service, root, provider, code="raise SystemExit(1)", required=False
        )
        await await_task(service, root, creds)
        _assert_completed(service, "task")
        assert service.workflow.checks("task")[0]["state"] == "failed"

    asyncio.run(scenario())


def test_undo_preserves_later_directory_contents(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        provider = ScriptedProvider(
            "workflow-session",
            [
                _tools(_call("write_file", path="new/deep/file.txt", content="task")),
                _finish(),
            ],
        )
        creds = await setup(service, root, provider)
        await await_task(service, root, creds)
        (root / "new/deep/later.txt").write_text("external")
        await service.handle(_request("task.undo", {"task_id": "task"}))
        assert (root / "new/deep/later.txt").read_text() == "external"
        assert not (root / "new/deep/file.txt").exists()

    asyncio.run(scenario())


def test_apply_recovery_is_idempotent_after_partial_materialization(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.workspace import batches

    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        provider = ScriptedProvider(
            "workflow-session",
            [
                _tools(_call("rename_file", source="a.py", destination="new/a.py")),
                _finish(),
            ],
        )
        creds = await setup(service, root, provider, review=True)
        await await_task(service, root, creds)
        original = batches.atomic_replace_regular_file

        def unavailable(**kwargs: Any) -> None:
            raise OSError("injected write interruption")

        monkeypatch.setattr(batches, "atomic_replace_regular_file", unavailable)
        result = await service.handle(_request("task.apply", {"task_id": "task"}))
        assert result["state"] == "operator_attention"
        assert (root / "new").is_dir()
        monkeypatch.setattr(batches, "atomic_replace_regular_file", original)
        await service.handle(_request("task.recover", {}))
        assert (root / "new/a.py").read_text() == "value = 1\n"
        assert not (root / "a.py").exists()
        retried = await service.handle(_request("task.apply", {"task_id": "task"}))
        assert retried["task_id"] == result["task_id"]
        assert service._task_view("task")["state"] == "completed"

    asyncio.run(scenario())


def test_setup_failure_is_recorded_without_publication(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
) -> None:
    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        creds = await setup(service, root, edit_provider(_finish()))
        await service.handle(
            _request(
                "checks.configure",
                {
                    "path": str(root),
                    "config": {
                        "setup": [[sys.executable, "-c", "raise SystemExit(9)"]],
                        "checks": {
                            "test": {
                                "argv": [
                                    sys.executable,
                                    "-c",
                                    "print('should not run')",
                                ]
                            }
                        },
                    },
                },
            )
        )
        await await_task(service, root, creds)
        assert service.workflow.checks("task")[0]["exit_code"] == 9
        assert service._task_view("task")["state"] == "awaiting_review"
        assert (root / "a.py").read_text() == "value = 1\n"

    asyncio.run(scenario())


def test_supervisor_stops_descendants_when_owner_pipe_closes(tmp_path: Path) -> None:
    import os
    import subprocess
    import time

    from llm_cli.execution import check_process

    marker = tmp_path / "started"
    heartbeat = tmp_path / "heartbeat"
    child_code = (
        "import time; from pathlib import Path; "
        f"[(Path({str(heartbeat)!r}).write_text(str(time.time())), "
        "time.sleep(.05)) for _ in range(1200)]"
    )
    script = (
        "from pathlib import Path; import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"Path({str(marker)!r}).write_text('ready'); time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, check_process.__file__, sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={"PATH": os.defpath},
    )
    try:
        deadline = time.monotonic() + 5
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists() and heartbeat.exists()
        assert proc.stdin is not None
        proc.stdin.close()
        exit_code = proc.wait(timeout=5)
        assert proc.stderr is not None
        os.set_blocking(proc.stderr.fileno(), False)
        assert exit_code == 125, proc.stderr.read(4096)
        stamp = heartbeat.read_text()
        time.sleep(0.2)
        assert heartbeat.read_text() == stamp
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=5)
        assert proc.stderr is not None
        proc.stderr.close()


def test_interrupted_snapshot_cleanup_uses_recorded_worktree(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    service_factory: Callable[..., DaemonService],
    git_run: Callable[..., str],
) -> None:
    from llm_cli.execution.checks import cleanup_interrupted_checks
    from llm_cli.git.worktrees import create_managed_worktree

    async def scenario() -> None:
        root = repository_factory(tmp_path, {"a.py": "value = 1\n"})
        service = service_factory(tmp_path)
        creds = await setup(service, root, edit_provider(_finish()), code="print('ok')")
        await await_task(service, root, creds)
        run_id = service.workflow.checks("task")[0]["run_id"]
        managed_root = service.paths.data_dir / "check-worktrees"
        worktree = create_managed_worktree(
            root,
            managed_root=managed_root,
            task_id=run_id,
            base_oid=git_run(root, "rev-parse", "HEAD"),
        )
        with service.store.connection() as con:
            con.execute(
                "UPDATE check_runs SET state='uncertain' WHERE run_id=?", (run_id,)
            )
        unrelated = managed_root / "unrelated"
        unrelated.mkdir()
        cleanup_interrupted_checks(service.store, managed_root)
        assert not worktree.path.exists()
        assert unrelated.exists()
        assert (root / "a.py").read_text() == "value = 2\n"
        cleanup_interrupted_checks(service.store, managed_root)

    asyncio.run(scenario())
