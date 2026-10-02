"""Secret screening precedes native replay, transcript events, and checkpoints."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from llm_cli.agent.driver import RunRequest
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.tools import ToolBroker, ToolOutcome
from llm_cli.errors import LlmCoordError
from llm_cli.providers.base import ModelTurn, ToolCallRequest, ToolCallResult

_SECRET = "-----BEGIN " + "PRIVATE KEY-----\ndummy-private-material\n"


class Script:
    name = "script"
    model = "script"

    def __init__(self, turns: Sequence[ModelTurn] = ()) -> None:
        self.turns = iter(turns)
        self.sessions = 0
        self.history: list[object] = []
        self.received: list[ToolCallResult] = []

    def session(self, *, system: str, tools: object, state: object = None) -> Script:
        self.sessions += 1
        self.history.append(copy.deepcopy(state))
        return self

    def snapshot(self) -> dict[str, object]:
        return {"messages": copy.deepcopy(self.history)}

    def send_user(self, text: str) -> ModelTurn:
        self.history.append(text)
        return next(self.turns, ModelTurn(text="done", stop_reason="end_turn"))

    def record_tool_results(self, results: Sequence[ToolCallResult]) -> None:
        self.received.extend(results)
        self.history.append([result.content for result in results])

    def send_tool_results(self, results: Sequence[ToolCallResult]) -> ModelTurn:
        self.record_tool_results(results)
        return next(self.turns, ModelTurn(text="done", stop_reason="end_turn"))


def _request(root: Path) -> RunRequest:
    return RunRequest("task-privacy", 1, "inspect", ("*",), root, "base")


@pytest.mark.parametrize(
    "location", ["conversation", "checkpoint", "tool_results", "openai_output"]
)
def test_saved_secrets_are_rejected_before_session_or_events(
    tmp_path: Path, location: str
) -> None:
    provider = Script()
    events: list[object] = []
    checkpoints: list[object] = []
    broker = ToolBroker(tmp_path, ("*",), on_event=lambda *event: events.append(event))
    state: dict[str, object] = {
        "version": 2,
        "provider": "script",
        "model": "script",
        "session": {"messages": [{"content": _SECRET}]},
        "phase": "opening",
        "deadline_at": time.time() + 60,
        "tool_usage": broker.usage_snapshot(),
        "tool_results": [],
        "usage_total": {},
    }
    if location == "tool_results":
        state["session"] = {"messages": []}
        state["tool_results"] = [
            {"call_id": "r", "content": _SECRET, "is_error": False}
        ]
    if location == "openai_output":
        state["session"] = {
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "r",
                    "output": json.dumps({"content": _SECRET, "is_error": False}),
                }
            ]
        }
    request = replace(
        _request(tmp_path),
        conversation_state=state
        if location in {"conversation", "openai_output"}
        else None,
        resume_state=state if location in {"checkpoint", "tool_results"} else None,
        checkpoint=lambda saved: checkpoints.append(dict(saved)),
    )
    with pytest.raises(LlmCoordError, match="secret material"):
        CodingAgentHarness(provider).run(request, broker)
    assert provider.sessions == 0
    assert events == checkpoints == []


def test_secret_check_output_is_replaced_before_persistence_and_model(
    tmp_path: Path,
) -> None:
    provider = Script(
        [
            ModelTurn(
                text="",
                stop_reason="tool_use",
                tool_calls=(ToolCallRequest("r", "run_check", {"name": "tests"}),),
            )
        ]
    )
    events: list[object] = []
    checkpoints: list[object] = []
    broker = ToolBroker(
        tmp_path,
        ("*",),
        check_runner=lambda _: ToolOutcome(_SECRET),
        on_event=lambda *event: events.append(event),
    )
    CodingAgentHarness(provider).run(
        replace(
            _request(tmp_path),
            checkpoint=lambda saved: checkpoints.append(copy.deepcopy(dict(saved))),
        ),
        broker,
    )
    assert provider.received[0].is_error
    assert "secret material" in provider.received[0].content
    assert "dummy-private-material" not in repr(events + checkpoints + provider.history)


@pytest.mark.parametrize("native", ["openai", "anthropic", "checkpoint"])
@pytest.mark.parametrize("outcome", ["success", "denied", "bad_path"])
@pytest.mark.parametrize("excluded_path", ["ignored", "git_symlink"])
def test_replay_rechecks_successful_read_paths_against_current_exclusions(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    native: str,
    outcome: str,
    excluded_path: str,
) -> None:
    is_error = outcome != "success"
    root = repository_factory(tmp_path, {"source.txt": "ordinary source"})
    (root / ".gitignore").write_text("private.txt\n")
    relative = "private.txt"
    if excluded_path == "git_symlink":
        (root / ".git/private-note").write_text("old private notes")
        (root / "metadata-note").symlink_to(".git/private-note")
        relative = "metadata-note"
    call: dict[str, object] = {
        "call_id": "r",
        "name": "read_file",
        "arguments": {"path": "../outside" if outcome == "bad_path" else relative},
    }
    result: dict[str, object] = {
        "call_id": "r",
        "content": "old private notes",
        "is_error": is_error,
    }
    state: dict[str, object] = {"version": 2, "provider": "script", "model": "script"}
    if native == "openai":
        state["session"] = {
            "input": [
                {
                    **call,
                    "type": "function_call",
                    "arguments": json.dumps(call["arguments"]),
                },
                {
                    "type": "function_call_output",
                    "call_id": "r",
                    "output": json.dumps(result),
                },
            ]
        }
    elif native == "anthropic":
        state["session"] = {
            "messages": [
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "r",
                            "name": "read_file",
                            "input": call["arguments"],
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "r",
                            "content": result["content"],
                            "is_error": is_error,
                        },
                    ]
                }
            ]
        }
    else:
        state.update(
            session={"messages": []},
            phase="opening",
            deadline_at=time.time() + 60,
            turn={
                "text": "",
                "stop_reason": "tool_use",
                "tool_calls": [call],
                "usage": {},
            },
            tool_results=[result],
            tool_usage=ToolBroker(root, ("*",)).usage_snapshot(),
            usage_total={},
        )
    provider = Script()
    request = replace(
        _request(root),
        conversation_state=state if native != "checkpoint" else None,
        resume_state=state if native == "checkpoint" else None,
    )
    if is_error:
        CodingAgentHarness(provider).run(request, ToolBroker(root, ("*",)))
        assert provider.sessions == 1
    else:
        with pytest.raises(LlmCoordError, match="excluded source"):
            CodingAgentHarness(provider).run(request, ToolBroker(root, ("*",)))
        assert provider.sessions == 0


@pytest.mark.parametrize("deleted", [False, True])
def test_replay_allows_ordinary_source_aliases_and_deleted_source(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    deleted: bool,
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "ordinary source"})
    (root / "source-alias.txt").symlink_to("source.txt")
    if deleted:
        (root / "source.txt").unlink()
    state = {
        "version": 2,
        "provider": "script",
        "model": "script",
        "session": {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "r",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "source-alias.txt"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": "r",
                    "output": json.dumps(
                        {"content": "ordinary source", "is_error": False}
                    ),
                },
            ]
        },
    }
    provider = Script()

    CodingAgentHarness(provider).run(
        replace(_request(root), conversation_state=state),
        ToolBroker(root, ("*",)),
    )

    assert provider.sessions == 1
