"""Managed process tools preserve approval, budgets and publication liveness."""

from __future__ import annotations

import base64
import json
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from llm_cli.agent.limits import ExecutionLimits
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import (
    TaskCancelled,
    ToolBroker,
    ToolBudgetExhausted,
    ToolOutcome,
    tool_schemas,
)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.questions: list[str] = []

    def result(self, state: str = "running") -> ToolOutcome:
        return ToolOutcome(
            json.dumps(
                {
                    "process_id": "process-test",
                    "state": state,
                    "output": "ready [private value]",
                    "truncated": False,
                    "stop_command": "loupe workspace process stop process-test",
                }
            ),
            metadata={"process_id": "process-test"},
        )

    def start(self, argv: list[str], cwd: str) -> ToolOutcome:
        self.calls.append(("start", (argv, cwd)))
        return self.result()

    def output(self, process_id: str) -> ToolOutcome:
        self.calls.append(("output", process_id))
        return self.result()

    def stop(self, process_id: str) -> ToolOutcome:
        self.calls.append(("stop", process_id))
        return self.result("stopped")

    def command(self, argv: list[str], cwd: str, timeout: int) -> ToolOutcome:
        self.calls.append(("command", (argv, cwd, timeout)))
        return ToolOutcome("completed")


def broker(
    tmp_path: Path,
    *,
    cls: type[ToolBroker] = ToolBroker,
    approval: str = "allow",
    mode: str = "auto",
    answers: list[str] | None = None,
    **kwargs: Any,
) -> tuple[ToolBroker, Recorder]:
    recorded = Recorder()

    def ask(question: str) -> str:
        recorded.questions.append(question)
        assert answers, "unexpected approval prompt"
        return answers.pop(0)

    instance = cls(
        tmp_path,
        ("*",),
        command_runner=recorded.command,
        process_starter=recorded.start,
        process_reader=recorded.output,
        process_stopper=recorded.stop,
        command_approval=approval,
        asker=ask if answers is not None else None,
        agent_mode=mode,
        **kwargs,
    )
    return instance, recorded


def test_only_configured_process_callbacks_are_offered(tmp_path: Path) -> None:
    plain = ToolBroker(tmp_path, ("*",))
    assert not {"start_process", "process_output", "stop_process"} & set(
        plain.tool_names()
    )
    active, _ = broker(tmp_path)
    assert {"start_process", "process_output", "stop_process"} <= set(
        active.tool_names()
    )
    inactive, _ = broker(tmp_path, approval="ask")
    assert "start_process" not in inactive.tool_names()
    assert {"process_output", "stop_process"} <= set(inactive.tool_names())
    disabled, _ = broker(tmp_path, approval="off", answers=["1"])
    assert "start_process" not in disabled.tool_names()
    assert {"process_output", "stop_process"} <= set(disabled.tool_names())


def test_process_schema_uses_argv_and_opaque_process_ids() -> None:
    start, output, stop = tool_schemas(
        ["start_process", "process_output", "stop_process"]
    )
    assert start["input_schema"]["required"] == ["argv"]  # type: ignore[index]
    assert "timeout" not in start["input_schema"]["properties"]  # type: ignore[index]
    assert output["input_schema"]["required"] == ["process_id"]  # type: ignore[index]
    assert stop["input_schema"]["required"] == ["process_id"]  # type: ignore[index]


@pytest.mark.parametrize("cls", [ToolBroker, SharedToolBroker])
def test_process_calls_reach_callbacks_and_preserve_identity_metadata(
    tmp_path: Path, cls: type[ToolBroker]
) -> None:
    active, recorded = broker(tmp_path, cls=cls)
    start = active.invoke("start_process", {"argv": ["python", "server.py"]})
    output = active.invoke("process_output", {"process_id": "process-test"})
    stop = active.invoke("stop_process", {"process_id": "process-test"})
    assert not start.is_error and not output.is_error and not stop.is_error
    assert recorded.calls == [
        ("start", (["python", "server.py"], ".")),
        ("output", "process-test"),
        ("stop", "process-test"),
    ]
    assert start.metadata == {"process_id": "process-test"}
    assert json.loads(stop.content)["state"] == "stopped"
    assert active.usage.calls == 3


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"argv": []},
        {"argv": "python server.py"},
        {"argv": [""]},
        {"argv": ["echo", 3]},
        {"argv": ["echo", "a\0b"]},
        {"argv": ["x"] * 101},
        {"argv": ["echo", "x" * 70_000]},
        {"argv": ["ls"], "cwd": 3},
        {"argv": ["python"], "timeout": 60},
    ],
)
def test_start_rejects_invalid_arguments_before_approval_or_launch(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    active, recorded = broker(tmp_path, approval="ask", answers=["1"])
    result = active.invoke("start_process", arguments)
    assert result.is_error
    assert recorded.calls == []
    assert recorded.questions == []


@pytest.mark.parametrize("tool", ["process_output", "stop_process"])
@pytest.mark.parametrize("process_id", ["", " ", "x\0y", "x/y", "x" * 201, 3])
def test_invalid_process_identity_does_not_reach_manager(
    tmp_path: Path, tool: str, process_id: object
) -> None:
    active, recorded = broker(tmp_path)
    result = active.invoke(tool, {"process_id": process_id})
    assert result.is_error
    assert recorded.calls == []


def test_shared_refusal_is_actionable_and_precedes_approval(tmp_path: Path) -> None:
    active, recorded = broker(
        tmp_path,
        cls=SharedToolBroker,
        approval="ask",
        answers=["1"],
        process_start_refusal=(
            "Managed background processes require isolated mode. "
            "Use /workspace isolated, then start_process."
        ),
    )
    result = active.invoke("start_process", {"argv": ["python", "server.py"]})
    assert result.is_error
    assert "/workspace isolated" in result.content
    assert recorded.questions == []
    assert recorded.calls == []


def test_persistent_processes_need_truthful_distinct_approval(tmp_path: Path) -> None:
    active, recorded = broker(tmp_path, approval="ask", answers=["1", "1"])
    first = active.invoke(
        "start_process", {"argv": ["python", "server.py"], "cwd": "src"}
    )
    second = active.invoke("start_process", {"argv": ["python", "watcher.py"]})
    assert not first.is_error and not second.is_error
    assert len(recorded.questions) == 2
    assert "private worktree" in recorded.questions[0]
    assert "remain" in recorded.questions[0]
    assert "discarded" not in recorded.questions[0]
    assert "timeout" not in recorded.questions[0]
    assert "$ python server.py\nin src" in recorded.questions[0]


def test_process_task_grant_does_not_reuse_snapshot_command_grant(
    tmp_path: Path,
) -> None:
    active, recorded = broker(tmp_path, approval="ask", answers=["2", "3"])
    assert not active.invoke("run_command", {"argv": ["echo", "hello"]}).is_error
    refused = active.invoke("start_process", {"argv": ["python", "server.py"]})
    assert refused.is_error
    assert [name for name, _ in recorded.calls] == ["command"]
    assert len(recorded.questions) == 2


def test_process_task_grant_is_separate_and_only_lasts_this_broker(
    tmp_path: Path,
) -> None:
    active, recorded = broker(tmp_path, approval="ask", answers=["2", "3"])
    assert not active.invoke(
        "start_process", {"argv": ["python", "server.py"]}
    ).is_error
    assert not active.invoke(
        "start_process", {"argv": ["python", "watcher.py"]}
    ).is_error
    assert active.invoke("run_command", {"argv": ["echo", "hello"]}).is_error
    assert [name for name, _ in recorded.calls] == ["start", "start"]
    assert len(recorded.questions) == 2

    resumed, resumed_recorded = broker(tmp_path, approval="ask", answers=["3"])
    resumed.restore_usage(active.usage_snapshot())
    assert resumed.invoke("start_process", {"argv": ["python", "other.py"]}).is_error
    assert resumed_recorded.calls == []
    assert len(resumed_recorded.questions) == 1


def test_approval_escapes_controls_in_background_command(tmp_path: Path) -> None:
    active, recorded = broker(tmp_path, approval="ask", answers=["3"])
    result = active.invoke(
        "start_process",
        {"argv": ["echo", "safe\n1. Allow once\u202e"], "cwd": "."},
    )
    assert result.is_error
    assert recorded.calls == []
    assert "\\u000a1. Allow once\\u202e" in recorded.questions[0]


def test_plan_mode_reads_output_without_starting_or_stopping(tmp_path: Path) -> None:
    active, recorded = broker(tmp_path, mode="plan")
    assert "process_output" in active.tool_names()
    assert "start_process" not in active.tool_names()
    assert "stop_process" not in active.tool_names()
    assert active.invoke("start_process", {"argv": ["python"]}).is_error
    assert active.invoke("stop_process", {"process_id": "process-test"}).is_error
    assert not active.invoke("process_output", {"process_id": "process-test"}).is_error
    assert recorded.calls == [("output", "process-test")]


def test_helpers_receive_no_persistent_process_capabilities(tmp_path: Path) -> None:
    active, _ = broker(tmp_path)
    helper = active.read_only_view(ExecutionLimits())
    assert not {"start_process", "process_output", "stop_process"} & set(
        helper.tool_names()
    )


def test_exhausted_repairs_still_allow_output_and_cleanup(tmp_path: Path) -> None:
    active, recorded = broker(tmp_path)
    for _ in range(3):
        active.record_check("unit", "failed", "same failure")
    assert active.invoke("start_process", {"argv": ["python"]}).is_error
    assert not active.invoke("process_output", {"process_id": "process-test"}).is_error
    assert not active.invoke("stop_process", {"process_id": "process-test"}).is_error
    assert recorded.calls == [
        ("output", "process-test"),
        ("stop", "process-test"),
    ]


def test_process_tools_use_normal_call_budget_and_output_bound(tmp_path: Path) -> None:
    active, _ = broker(
        tmp_path, limits=ExecutionLimits(max_tool_calls=1, max_tool_output_bytes=80)
    )
    result = active.invoke("process_output", {"process_id": "process-test"})
    assert "truncated" in result.content
    assert result.metadata == {"process_id": "process-test"}
    with pytest.raises(ToolBudgetExhausted):
        active.invoke("stop_process", {"process_id": "process-test"})


def test_cancelled_task_does_not_launch_a_process(tmp_path: Path) -> None:
    active, recorded = broker(tmp_path, cancelled=lambda: True)
    with pytest.raises(TaskCancelled):
        active.invoke("start_process", {"argv": ["python"]})
    assert recorded.calls == []


@pytest.mark.parametrize(
    ("tool", "arguments", "callback_name"),
    [
        ("start_process", {"argv": ["python"]}, "process_starter"),
        ("process_output", {"process_id": "process-test"}, "process_reader"),
        ("stop_process", {"process_id": "process-test"}, "process_stopper"),
    ],
)
def test_managed_process_calls_never_hold_publication_lock(
    tmp_path: Path, tool: str, arguments: dict[str, object], callback_name: str
) -> None:
    lock = threading.RLock()
    entered = threading.Event()
    active, _ = broker(tmp_path, cls=SharedToolBroker, publication_lock=lock)

    def callback(*args: object) -> ToolOutcome:
        entered.set()
        return ToolOutcome("screened")

    setattr(active, callback_name, callback)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with lock:
            future = pool.submit(active.invoke, tool, arguments)
            entered_without_barrier = entered.wait(1)
        result = future.result(timeout=2)
    assert entered_without_barrier
    assert not result.is_error


@pytest.fixture
def private_checkout(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    git_run: Callable[..., str],
) -> tuple[Path, Path, Path]:
    primary = repository_factory(
        tmp_path,
        {
            "server.py": "VALUE = 'old'\n",
            "data.bin": "old binary input\n",
            "deleted.py": "delete me\n",
        },
    )
    private = tmp_path / "private"
    git_run(primary, "worktree", "add", "--detach", str(private), "HEAD")
    git_dir = Path(git_run(private, "rev-parse", "--absolute-git-dir"))
    return primary, private, git_dir


def test_materialize_pending_private_http_code_and_new_paths(
    private_checkout: tuple[Path, Path, Path],
) -> None:
    primary, private, git_dir = private_checkout
    active = SharedToolBroker(private, ("*",))
    assert not active.invoke("read_file", {"path": "server.py"}).is_error
    assert not active.invoke(
        "write_file", {"path": "server.py", "content": "VALUE = 'new'\n"}
    ).is_error
    assert not active.invoke(
        "write_file", {"path": "generated/deep/server.py", "content": "READY = True\n"}
    ).is_error
    assert not active.invoke("delete_file", {"path": "deleted.py"}).is_error
    writes = active.usage.writes
    assert (private / "server.py").read_text() == "VALUE = 'old'\n"

    installed = active.materialize_private_candidates(git_dir)

    assert "server.py" in installed
    assert (private / "server.py").read_text() == "VALUE = 'new'\n"
    assert (private / "generated/deep/server.py").read_text() == "READY = True\n"
    assert not (private / "deleted.py").exists()
    assert active.candidates() == ()
    assert active.usage.writes == writes
    assert (primary / "server.py").read_text() == "VALUE = 'old'\n"
    assert (primary / "deleted.py").exists()
    assert not (primary / "generated").exists()
    assert not active.invoke("read_file", {"path": "server.py"}).is_error
    assert not active.invoke(
        "write_file", {"path": "server.py", "content": "VALUE = 'newer'\n"}
    ).is_error
    assert active.candidates()[0].original == b"VALUE = 'new'\n"


def test_materialized_binary_and_symlink_survive_checkpoint_replay(
    private_checkout: tuple[Path, Path, Path],
) -> None:
    _, private, git_dir = private_checkout
    active = SharedToolBroker(private, ("*",))
    body = b"\x00\xffnew binary\n"
    assert not active.invoke("read_binary", {"path": "data.bin"}).is_error
    assert not active.invoke(
        "write_binary",
        {
            "path": "data.bin",
            "content_base64": base64.b64encode(body).decode(),
        },
    ).is_error
    assert not active.invoke(
        "write_symlink", {"path": "server-link", "target": "server.py"}
    ).is_error
    before = active.usage_snapshot()

    active.materialize_private_candidates(git_dir)

    assert (private / "data.bin").read_bytes() == body
    assert (private / "server-link").is_symlink()
    assert os.readlink(private / "server-link") == "server.py"
    assert active.candidates() == ()
    assert active.reconcile().clean
    restored = SharedToolBroker(private, ("*",))
    restored.restore_usage(before)
    replay = restored.reconcile()
    assert replay.clean
    assert set(replay.adopted) == {"data.bin", "server-link"}
    assert restored.candidates() == ()


def test_materialize_checks_all_bases_before_any_write(
    private_checkout: tuple[Path, Path, Path],
) -> None:
    from llm_cli.workspace.broker import WorkspaceBrokerError

    _, private, git_dir = private_checkout
    active = SharedToolBroker(private, ("*",))
    assert not active.invoke("read_file", {"path": "server.py"}).is_error
    assert not active.invoke(
        "write_file", {"path": "server.py", "content": "our edit\n"}
    ).is_error
    assert not active.invoke(
        "write_file", {"path": "a-first.py", "content": "must not install\n"}
    ).is_error
    (private / "server.py").write_text("late user edit\n")

    with pytest.raises(WorkspaceBrokerError, match="read the current file"):
        active.materialize_private_candidates(git_dir)

    assert (private / "server.py").read_text() == "late user edit\n"
    assert not (private / "a-first.py").exists()
    assert len(active.candidates()) == 2


def test_materialize_atomic_writer_refuses_late_private_edits(
    private_checkout: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import llm_cli.agent.shared_tools as source
    from llm_cli.workspace.broker import WorkspaceBrokerError

    _, private, git_dir = private_checkout
    active = SharedToolBroker(private, ("*",))
    active.invoke("read_file", {"path": "server.py"})
    active.invoke("write_file", {"path": "server.py", "content": "our edit\n"})
    original = source.atomic_replace_regular_file

    def raced(**kwargs: Any) -> None:
        (private / "server.py").write_text("late during staging\n")
        original(**kwargs)

    monkeypatch.setattr(source, "atomic_replace_regular_file", raced)
    with pytest.raises(WorkspaceBrokerError, match="base changed"):
        active.materialize_private_candidates(git_dir)
    assert (private / "server.py").read_text() == "late during staging\n"
    assert active.candidates()[0].content == b"our edit\n"


def test_materialize_partial_write_retry_recognizes_exact_installed_result(
    private_checkout: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import llm_cli.agent.shared_tools as source

    _, private, git_dir = private_checkout
    active = SharedToolBroker(private, ("*",))
    active.invoke("read_binary", {"path": "data.bin"})
    active.invoke("write_binary", {"path": "data.bin", "content_base64": "AG5ldw=="})
    before = active.usage_snapshot()
    original = source.atomic_replace_regular_file

    def interrupted(**kwargs: Any) -> None:
        original(**kwargs)
        raise RuntimeError("simulated process death before adoption")

    monkeypatch.setattr(source, "atomic_replace_regular_file", interrupted)
    with pytest.raises(RuntimeError, match="before adoption"):
        active.materialize_private_candidates(git_dir)
    assert (private / "data.bin").read_bytes() == b"\x00new"

    monkeypatch.setattr(source, "atomic_replace_regular_file", original)
    restored = SharedToolBroker(private, ("*",))
    restored.restore_usage(before)
    assert restored.materialize_private_candidates(git_dir) == ("data.bin",)
    assert restored.candidates() == ()


def test_materialize_preserves_tightened_access_permissions(
    private_checkout: tuple[Path, Path, Path],
) -> None:
    _, private, git_dir = private_checkout
    path = private / "server.py"
    path.chmod(0o644)
    active = SharedToolBroker(private, ("*",))
    active.invoke("read_file", {"path": "server.py"})
    active.invoke("write_file", {"path": "server.py", "content": "our edit\n"})
    path.chmod(0o600)
    active.materialize_private_candidates(git_dir)
    assert path.stat().st_mode & 0o777 == 0o600
    assert active.candidates() == ()


@pytest.mark.parametrize("wrong_admin", [False, True])
def test_materialize_refuses_main_or_wrong_private_administration(
    private_checkout: tuple[Path, Path, Path], wrong_admin: bool
) -> None:
    from llm_cli.workspace.broker import WorkspaceBrokerError

    primary, private, _git_dir = private_checkout
    target = private if wrong_admin else primary
    active = SharedToolBroker(target, ("*",))
    active.invoke("read_file", {"path": "server.py"})
    active.invoke("write_file", {"path": "server.py", "content": "our edit\n"})
    with pytest.raises(WorkspaceBrokerError, match="private"):
        active.materialize_private_candidates(primary / ".git")
    assert (target / "server.py").read_text() == "VALUE = 'old'\n"


def test_materialize_file_io_does_not_hold_publication_barrier(
    private_checkout: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import llm_cli.agent.shared_tools as source
    import llm_cli.workspace.broker as filesystem

    _, private, git_dir = private_checkout
    lock = threading.RLock()
    active = SharedToolBroker(private, ("*",), publication_lock=lock)
    active.invoke("read_file", {"path": "server.py"})
    active.invoke("write_file", {"path": "server.py", "content": "our edit\n"})
    observed = source.read_candidate_path
    checks: list[bool] = []

    def barrier_is_available() -> bool:
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        return acquired

    with ThreadPoolExecutor(max_workers=1) as pool:

        def inspect(*args: Any, **kwargs: Any) -> Any:
            checks.append(pool.submit(barrier_is_available).result(timeout=1))
            return observed(*args, **kwargs)

        monkeypatch.setattr(source, "read_candidate_path", inspect)
        monkeypatch.setattr(filesystem, "read_candidate_path", inspect)
        active.materialize_private_candidates(git_dir)
    assert checks and all(checks)


def test_prompt_distinguishes_persistent_processes_from_discarded_commands(
    tmp_path: Path,
) -> None:
    from llm_cli.agent.driver import RunRequest
    from llm_cli.agent.harness import CodingAgentHarness
    from llm_cli.providers.base import ModelTurn

    class Script:
        name = "script"
        model = "script"

        def __init__(self) -> None:
            self.systems: list[str] = []

        def session(self, *, system: str, **kwargs: object) -> Script:
            self.systems.append(system)
            return self

        def snapshot(self) -> dict[str, object]:
            return {"messages": []}

        def send_user(self, text: str) -> ModelTurn:
            return ModelTurn(text="answer")

        def send_tool_results(self, results: object) -> ModelTurn:
            raise AssertionError("no tools expected")

        def record_tool_results(self, results: object) -> None:
            pass

    provider = Script()
    active, _ = broker(tmp_path)
    request = RunRequest("task", 1, "explain", ("*",), tmp_path, "base")
    CodingAgentHarness(provider).run(request, active)
    assert "Managed background processes:" in provider.systems[0]
    assert "Its changes persist" in provider.systems[0]
    assert "configured checks" in provider.systems[0]
