"""Delegated tools keep parent state, approvals and waiting lifecycle explicit."""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from test_explorer import Provider, _calls, _text

from llm_cli.agent.driver import RunRequest
from llm_cli.agent.explorer import Explorer
from llm_cli.agent.harness import CodingAgentHarness
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import ToolOutcome
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.web.access import WebAccess
from llm_cli.web.fetch import Page, WebFetchError


def _parent(tmp_path: Path) -> SharedToolBroker:
    (tmp_path / "a.txt").write_text("first\nsecond\nthird\n")
    run_git(tmp_path, ["init", "-b", "main"])
    run_git(tmp_path, ["add", "."])
    parent = SharedToolBroker(tmp_path, ("*",), command_approval="allow")
    assert not parent.invoke("read_file", {"path": "a.txt"}).is_error
    assert not parent.invoke(
        "apply_patch", {"path": "a.txt", "old_text": "first", "new_text": "ours"}
    ).is_error
    parent.command_runner = parent.catching_up(lambda *args: ToolOutcome("ran", False))
    return parent


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("restore", [False, True])
def test_helper_reconciliation_refuses_parent_action_until_notice_consumed(
    tmp_path: Path, conflict: bool, restore: bool
) -> None:
    parent = _parent(tmp_path)
    helper = parent.read_only_view(parent.limits)
    (tmp_path / "a.txt").write_text(
        "peer\nsecond\nthird\n" if conflict else "first\nsecond\npeer\n"
    )
    result = helper.invoke("run_command", {"argv": ["pwd"]})
    assert ("conflict" if conflict else "merged") in result.content
    before = parent.candidates()
    if restore:
        saved = parent.usage_snapshot()
        parent = SharedToolBroker(tmp_path, ("*",))
        parent.restore_usage(saved)
    blocked = parent.invoke(
        "apply_patch", {"path": "a.txt", "old_text": "ours", "new_text": "later"}
    )
    assert blocked.is_error
    assert ("conflict" if conflict else "merged") in blocked.content
    assert parent.candidates() == before
    notice = parent.pending_parent_notice()
    assert notice is not None
    version, text = notice
    assert ("conflict" if conflict else "merged") in text
    # A caller cannot accidentally clear a newer unseen update.
    parent.acknowledge_parent_notice(version - 1)
    assert parent.pending_parent_notice() == notice
    parent.acknowledge_parent_notice(version)
    assert parent.pending_parent_notice() is None


def test_approval_questions_identify_helper_without_leaking_its_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    questions: list[str] = []

    def ask(question: str) -> str:
        questions.append(question)
        return "1"

    def fetch(url: str, **kwargs: Any) -> Page:
        refusal = kwargs["allowed"](urlsplit(url).hostname, url)
        if refusal:
            raise WebFetchError(refusal)
        return Page(url, 200, "text/plain", "page", False)

    monkeypatch.setattr("llm_cli.web.access.fetch", fetch)
    parent = SharedToolBroker(
        tmp_path,
        ("*",),
        asker=ask,
        web=WebAccess("ask"),
        command_runner=lambda *args: ToolOutcome("ran", False),
    )
    runner = parent.command_runner
    task = "PRIVATE_EXPLORATION_TASK_TEXT"
    provider = Provider(
        [],
        {
            task: [
                _calls(
                    ("run_command", {"argv": ["pwd"]}),
                    ("web_fetch", {"url": "https://helper.example/"}),
                ),
                _text("done"),
            ]
        },
    )
    assert not Explorer(provider, parent)(task).is_error
    assert not parent.invoke("run_command", {"argv": ["pwd"]}).is_error
    assert not parent.invoke("web_fetch", {"url": "https://parent.example/"}).is_error
    assert len(questions) == 4
    assert all(
        "exploration helper" in question.casefold() for question in questions[:2]
    )
    assert all(
        "exploration helper" not in question.casefold() for question in questions[2:]
    )
    assert task not in "\n".join(questions)
    assert parent.asker is ask and parent.command_runner is runner


def test_coalesced_fetch_waits_for_human_approval_beyond_network_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asking = threading.Event()
    release = threading.Event()
    joined = threading.Event()
    web = WebAccess("ask", timeout=0.01)
    original_wait = web._wait_for_page

    def wait(*args: Any) -> Page:
        joined.set()
        return original_wait(*args)

    def approve(host: str, url: str) -> None:
        asking.set()
        assert release.wait(5)

    def fetch(
        url: str, *, allowed: Callable[[str, str], str | None], **kw: Any
    ) -> Page:
        allowed("example.com", url)
        return Page(url, 200, "text/plain", "approved", False)

    monkeypatch.setattr(web, "_wait_for_page", wait)
    monkeypatch.setattr("llm_cli.web.access.fetch", fetch)
    with ThreadPoolExecutor(max_workers=2) as workers:
        owner = workers.submit(web.page, "https://example.com/", allowed=approve)
        assert asking.wait(5)
        waiter = workers.submit(web.page, "https://example.com/", allowed=approve)
        try:
            assert joined.wait(5)
            threading.Event().wait(0.05)
            assert not waiter.done(), "network timeout must not cap human approval"
        finally:
            release.set()
        assert owner.result(timeout=5)[0].text == "approved"
        assert waiter.result(timeout=5)[0].text == "approved"


def _request(tmp_path: Path, **kwargs: Any) -> RunRequest:
    return RunRequest(
        "helper-controls", 1, "inspect", ("*",), tmp_path, "base", **kwargs
    )


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("stale_write", [False, True])
def test_parent_receives_helper_reconciliation_before_proposing_next_write(
    tmp_path: Path, conflict: bool, stale_write: bool
) -> None:
    parent = _parent(tmp_path)

    def changed_checkout() -> Any:
        (tmp_path / "a.txt").write_text(
            "peer\nsecond\nthird\n" if conflict else "first\nsecond\npeer\n"
        )
        return _calls(("run_command", {"argv": ["pwd"]}))

    def next_action() -> Any:
        payload = provider.main_session().sent[-1]
        assert isinstance(payload, tuple)
        assert len(payload) == (2 if stale_write else 1)
        assert "Helper workspace update" in payload[-1].content
        assert ("conflict" if conflict else "merged") in payload[-1].content
        if stale_write:
            assert payload[1].is_error  # The same batch cannot act on stale context.
        assert "later" not in str(parent.candidates())
        # The parent sees the update before proposing its next read and write.
        return _calls(("read_file", {"path": "a.txt"}))

    calls: list[tuple[str, dict[str, object]]] = [("explore", {"task": "check"})]
    if stale_write:
        calls.append(
            ("apply_patch", {"path": "a.txt", "old_text": "ours", "new_text": "later"})
        )
    provider = Provider(
        [
            _calls(*calls),
            next_action,
            _calls(
                ("write_file", {"path": "a.txt", "content": "resolved\nsecond\npeer\n"})
            ),
            _text("done"),
        ],
        {"check": [changed_checkout, _text("The command completed.")]},
    )
    CodingAgentHarness(provider).run(_request(tmp_path), parent)
    assert parent.pending_parent_notice() is None


def test_failed_parent_request_keeps_notice_for_checkpoint_replay(
    tmp_path: Path,
) -> None:
    parent = _parent(tmp_path)
    checkpoints: list[dict[str, object]] = []

    def changed_checkout() -> Any:
        (tmp_path / "a.txt").write_text("first\nsecond\npeer\n")
        return _calls(("run_command", {"argv": ["pwd"]}))

    first = Provider(
        [
            _calls(("explore", {"task": "check"})),
            LlmCoordError(ErrorCode.PROTOCOL_MISMATCH, "provider failed"),
        ],
        {"check": [changed_checkout, _text("The command completed.")]},
    )

    def checkpoint(state: Mapping[str, object]) -> None:
        checkpoints.append(copy.deepcopy(dict(state)))

    with pytest.raises(LlmCoordError, match="provider failed"):
        CodingAgentHarness(first).run(_request(tmp_path, checkpoint=checkpoint), parent)
    assert parent.pending_parent_notice() is not None
    saved = checkpoints[-1]
    restored = SharedToolBroker(tmp_path, ("*",))

    def replayed_response() -> Any:
        payload = resumed.main_session().sent[-1]
        assert isinstance(payload, tuple)
        assert len(payload) == 1
        assert payload[0].content.count("[Helper workspace update]") == 1
        assert "merged" in payload[0].content
        return _text("received")

    resumed = Provider([replayed_response])
    CodingAgentHarness(resumed).run(_request(tmp_path, resume_state=saved), restored)
    assert restored.pending_parent_notice() is None


def test_parent_notice_survives_compaction_until_continue_response(
    tmp_path: Path,
) -> None:
    from test_context_compaction import Compactable

    parent = _parent(tmp_path)
    # A previous helper reconciliation was checkpointed but not yet delivered.
    parent.notify_parent("The pending edit was merged with a checkout change.")
    provider = Compactable(
        [replace(_calls(("read_diff", {})), context_tokens=90_000), _text("received")]
    )

    # The first opening prompt consumes the queued update, so queue a new one
    # during the actual tool action to exercise pending-result compaction.
    def on_event(kind: str, payload: dict[str, object]) -> None:
        if kind == "tool.called" and payload["tool"] == "read_diff":
            parent.notify_parent("The helper merged another checkout change.")

    parent.on_event = on_event
    CodingAgentHarness(provider).run(_request(tmp_path), parent)
    assert provider.summarize_calls
    assert any(
        isinstance(call, str) and "The helper merged another checkout change." in call
        for call in provider.calls[1:]
    )
    assert parent.pending_parent_notice() is None


def test_broker_web_waiter_uses_task_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloading = threading.Event()
    release = threading.Event()
    clock = [0.0]
    parent = SharedToolBroker(
        tmp_path,
        ("*",),
        web=WebAccess("allow"),
        deadline_at=1.0,
        clock=lambda: clock[0],
    )

    def fetch(url: str, **kwargs: Any) -> Page:
        downloading.set()
        assert release.wait(5)
        return Page(url, 200, "text/plain", "page", False)

    monkeypatch.setattr("llm_cli.web.access.fetch", fetch)
    assert parent.web is not None
    with ThreadPoolExecutor(max_workers=1) as workers:
        owner = workers.submit(
            parent.web.page, "https://example.com/", allowed=lambda *args: None
        )
        assert downloading.wait(5)
        try:
            clock[0] = 2.0
            result = parent.read_only_view(parent.limits).invoke(
                "web_fetch", {"url": "https://example.com/"}
            )
            assert result.is_error and "wall-clock budget" in result.content
            assert not owner.done()
        finally:
            release.set()
        assert owner.result(timeout=5)[0].text == "page"


def test_unseen_notice_blocks_batched_source_reads_and_pre_tool_hooks(
    tmp_path: Path,
) -> None:
    from llm_cli.agent.hooks import HookRunner

    parent = _parent(tmp_path)
    (tmp_path / "fresh.txt").write_text("fresh\n")
    reads: list[str] = []
    hooks: list[str] = []
    parent.on_read = lambda path, identity: reads.append(path)
    parent.hooks = HookRunner((), lambda *args, **kwargs: None, lambda *args: None)
    parent.hooks.before_tool = lambda name, arguments: hooks.append(name)  # type: ignore[method-assign, assignment]
    parent.notify_parent("Pending edits were merged.")
    outcomes = parent.invoke_reads(
        [
            ("read_file", {"path": "fresh.txt"}),
            ("read_file", {"path": "a.txt"}),
        ]
    )
    assert all(result.is_error for result in outcomes)
    assert not reads and not hooks
