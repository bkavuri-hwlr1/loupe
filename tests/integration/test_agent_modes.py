"""Agent modes constrain real tool dispatch and survive harness recovery."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from test_agent_loop import ScriptedProvider, _call, _tools

from llm_cli.agent.driver import RunRequest
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.modes import (
    AGENT_MODES,
    publication_mode,
    resolve_agent_mode,
    validate_agent_mode,
)
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import ToolBroker, ToolOutcome


@pytest.mark.parametrize(
    "mode,publish,expected",
    [
        (None, None, "normal"),
        (None, "review", "normal"),
        (None, "auto", "auto"),
        ("plan", None, "plan"),
        ("normal", None, "normal"),
        ("auto", None, "auto"),
        ("normal", "review", "normal"),
        ("auto", "auto", "auto"),
    ],
)
def test_explicit_modes_and_legacy_publication_flags_resolve_consistently(
    mode: object, publish: object, expected: str
) -> None:
    assert resolve_agent_mode(mode, publish) == expected
    assert publication_mode(expected) == ("review" if expected == "normal" else "auto")
    assert resolve_agent_mode(default="auto") == "auto"


@pytest.mark.parametrize(
    "mode,publish",
    [("plan", "auto"), ("plan", "review"), ("normal", "auto"), ("auto", "review")],
)
def test_conflicting_mode_and_publication_flags_are_rejected(
    mode: str, publish: str
) -> None:
    with pytest.raises(ValueError, match="conflicts"):
        resolve_agent_mode(mode, publish)


@pytest.mark.parametrize("value", [None, True, 1, [], "", "review", "AUTO"])
def test_invalid_modes_are_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="mode must be"):
        validate_agent_mode(value)


@pytest.mark.parametrize("shared", [False, True], ids=["isolated", "shared"])
@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("write_file", {"path": "a.txt", "content": "changed\n"}),
        ("apply_patch", {"path": "a.txt", "old_text": "base", "new_text": "changed"}),
        ("create_directory", {"path": "new"}),
        ("delete_file", {"path": "a.txt"}),
        ("rename_file", {"source": "a.txt", "destination": "new.txt"}),
    ],
)
def test_plan_denies_fabricated_mutation_calls_before_any_effect(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    shared: bool,
    tool: str,
    arguments: dict[str, object],
) -> None:
    root = repository_factory(tmp_path, {"a.txt": "base\n"})
    before = git_run(root, "show-ref"), git_run(root, "write-tree")
    broker_type = SharedToolBroker if shared else ToolBroker
    broker = broker_type(root, ("*",), agent_mode="plan")
    assert tool not in broker.tool_names()
    assert not broker.invoke("read_file", {"path": "a.txt"}).is_error
    refused = broker.invoke(tool, arguments)
    assert refused.is_error and "Plan mode is read-only" in refused.content
    assert broker.usage.writes == 0 and broker.usage.denied == 1
    assert (root / "a.txt").read_text() == "base\n"
    assert not (root / "new").exists() and not (root / "new.txt").exists()
    if isinstance(broker, SharedToolBroker):
        assert broker.candidates() == ()
    assert (git_run(root, "show-ref"), git_run(root, "write-tree")) == before


@pytest.mark.parametrize("shared", [False, True], ids=["isolated", "shared"])
def test_plan_never_runs_checks_even_through_finish_gate(
    tmp_path: Path, repository_factory: Callable[..., Path], shared: bool
) -> None:
    root = repository_factory(tmp_path, {"a.txt": "base\n"})
    calls: list[str] = []

    def run_check(name: str) -> ToolOutcome:
        calls.append(name)
        return ToolOutcome("check ran")

    broker_type = SharedToolBroker if shared else ToolBroker
    broker = broker_type(
        root,
        ("*",),
        agent_mode="plan",
        check_runner=run_check,
        finish_gate=lambda: run_check("finish gate"),
    )
    assert "run_check" not in broker.tool_names()
    assert broker.invoke("run_check", {"name": "test"}).is_error
    assert not broker.invoke("finish_task", {"answer": "Here is the plan"}).is_error
    assert calls == [] and broker.usage.finished


@pytest.mark.parametrize("mode", ["normal", "auto"])
@pytest.mark.parametrize("shared", [False, True], ids=["isolated", "shared"])
def test_implementation_modes_preserve_editing_and_check_tools(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    mode: str,
    shared: bool,
) -> None:
    root = repository_factory(tmp_path, {"a.txt": "base\n"})
    checks: list[str] = []

    def check(name: str) -> ToolOutcome:
        checks.append(name)
        return ToolOutcome("passed")

    broker_type = SharedToolBroker if shared else ToolBroker
    broker = broker_type(root, ("*",), agent_mode=mode, check_runner=check)
    assert "write_file" in broker.tool_names() and "run_check" in broker.tool_names()
    assert not broker.invoke("read_file", {"path": "a.txt"}).is_error
    assert not broker.invoke(
        "write_file", {"path": "a.txt", "content": "new\n"}
    ).is_error
    assert not broker.invoke("run_check", {"name": "test"}).is_error
    assert checks == ["test"]
    if isinstance(broker, SharedToolBroker):
        assert broker.candidates()[0].content == b"new\n"
    assert (root / "a.txt").read_text() == ("base\n" if shared else "new\n")


def test_plan_rejects_restored_pending_overlay_even_with_zero_write_counter(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root = repository_factory(tmp_path, {"a.txt": "base\n"})
    original = SharedToolBroker(root, ("*",))
    assert not original.invoke("read_file", {"path": "a.txt"}).is_error
    assert not original.invoke(
        "write_file", {"path": "a.txt", "content": "new\n"}
    ).is_error
    checkpoint = original.usage_snapshot()
    checkpoint["writes"] = 0
    plan = SharedToolBroker(root, ("*",), agent_mode="plan")
    with pytest.raises(ValueError, match=r"plan mode cannot restore.*edits"):
        plan.restore_usage(checkpoint)
    assert plan.candidates() == () and plan.usage.writes == 0
    assert (root / "a.txt").read_text() == "base\n"


def _request(root: Path, mode: str) -> RunRequest:
    return RunRequest(
        task_id="modes",
        attempt=1,
        instructions="Improve the documented workflow.",
        scopes=("*",),
        worktree=root,
        base_oid="0" * 40,
        agent_mode=mode,
        workspace_mode="shared",
    )


@pytest.mark.parametrize("mode", AGENT_MODES)
def test_mode_prompt_and_checkpoint_match_the_enforced_tool_surface(
    tmp_path: Path, repository_factory: Callable[..., Path], mode: str
) -> None:
    root = repository_factory(tmp_path, {"a.txt": "base\n"})
    checkpoints: list[dict[str, object]] = []
    provider = ScriptedProvider(
        [_tools(_call("finish_task", answer="done", summary="done"))]
    )
    broker = SharedToolBroker(root, ("*",), agent_mode=mode, asker=lambda _: "answer")
    result = CodingAgentHarness(provider).run(
        replace(
            _request(root, mode),
            checkpoint=lambda state: checkpoints.append(copy.deepcopy(dict(state))),
        ),
        broker,
    )
    assert result.summary == "done"
    assert all(saved["agent_mode"] == mode for saved in checkpoints)
    assert "ask_user" in provider.offered_tools and "Clarification:" in provider.system
    assert "advisory" in provider.system and "scope" in provider.system
    # Broad questions get a structured explanation rather than a terse summary.
    assert "Answer depth:" in provider.system
    assert "thorough,\norganized explanation" in provider.system
    if mode == "plan":
        assert "read-only" in provider.system and "validation" in provider.system
        assert "risks" in provider.system and "implementation steps" in provider.system
        assert "write_file" not in provider.offered_tools
        assert "cannot edit files" in provider.user_messages[0]
        assert "You may write" not in provider.user_messages[0]
    elif mode == "normal":
        assert "explicitly run /apply" in provider.system
        assert "automatically publishes" not in provider.system
    else:
        assert "automatically publishes" in provider.system
    assert broker.candidates() == ()


def _finished_checkpoint(root: Path, mode: str) -> dict[str, object]:
    checkpoints: list[dict[str, object]] = []
    CodingAgentHarness(
        ScriptedProvider(
            [_tools(_call("finish_task", answer="saved", summary="saved"))]
        )
    ).run(
        replace(
            _request(root, mode),
            checkpoint=lambda state: checkpoints.append(copy.deepcopy(dict(state))),
        ),
        SharedToolBroker(root, ("*",), agent_mode=mode),
    )
    return checkpoints[-1]


@pytest.mark.parametrize(
    "before,after", [("auto", "plan"), ("normal", "auto"), ("plan", "normal")]
)
def test_resume_rejects_changed_mode_before_model_or_tools_run(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    before: str,
    after: str,
) -> None:
    root = repository_factory(tmp_path, {"a.txt": "base\n"})
    saved = _finished_checkpoint(root, before)
    provider = ScriptedProvider([])
    broker = SharedToolBroker(root, ("*",), agent_mode=after)
    with pytest.raises(ValueError, match="saved harness mode"):
        CodingAgentHarness(provider).run(
            replace(_request(root, after), resume_state=saved), broker
        )
    assert not provider.user_messages and provider.restored_state is None
    assert broker.usage.calls == 0


@pytest.mark.parametrize("mode", AGENT_MODES)
def test_legacy_checkpoint_without_mode_retains_compatible_recovery(
    tmp_path: Path, repository_factory: Callable[..., Path], mode: str
) -> None:
    root = repository_factory(tmp_path, {"a.txt": "base\n"})
    saved = _finished_checkpoint(root, "auto")
    saved.pop("agent_mode")
    result = CodingAgentHarness(ScriptedProvider([])).run(
        replace(_request(root, mode), resume_state=saved),
        SharedToolBroker(root, ("*",), agent_mode=mode),
    )
    assert result.summary == "saved" and result.tool_calls == 1


def test_new_task_can_change_mode_without_reusing_previous_tool_authority(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root = repository_factory(tmp_path, {"a.txt": "base\n"})
    prior = _finished_checkpoint(root, "auto")
    provider = ScriptedProvider(
        [_tools(_call("finish_task", answer="plan", summary="plan"))]
    )
    result = CodingAgentHarness(provider).run(
        replace(_request(root, "plan"), conversation_state=prior),
        SharedToolBroker(root, ("*",), agent_mode="plan"),
    )
    assert provider.restored_state is not None
    assert "write_file" not in provider.offered_tools
    assert "read-only" in provider.system
    assert result.summary == "plan" and result.tool_calls == 1


def test_harness_rejects_broker_with_different_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="broker mode"):
        CodingAgentHarness(ScriptedProvider([])).run(
            _request(tmp_path, "plan"), ToolBroker(tmp_path, ("*",))
        )
