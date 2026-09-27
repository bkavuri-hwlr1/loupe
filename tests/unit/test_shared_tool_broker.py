"""Shared model edits remain private and retain the exact base across resume."""

from __future__ import annotations

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from llm_cli.agent.limits import ExecutionLimits
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.git.environment import run_git
from llm_cli.workspace.broker import MAX_SHARED_TEXT_BYTES, WorkspaceBrokerError
from llm_cli.workspace.identity import ABSENT, EXECUTABLE_MODE, identify_path


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "docs/guide.md").write_text("original guide\n")
    (root / "src/app.py").write_text("print('hi')\n")
    (root / ".gitignore").write_text(".env\n.venv/\n")
    run_git(root, ["init", "-b", "main"])
    run_git(root, ["add", "."])
    return root


def _broker(checkout: Path, **kwargs: Any) -> SharedToolBroker:
    return SharedToolBroker(worktree=checkout, scopes=("docs/",), **kwargs)


def _patch(
    broker: SharedToolBroker, old: str = "original", new: str = "edited"
) -> None:
    result = broker.invoke(
        "apply_patch",
        {"path": "docs/guide.md", "old_text": old, "new_text": new},
    )
    assert not result.is_error, result.content


def test_edits_are_private_with_read_search_and_diff_overlay(checkout: Path) -> None:
    broker = _broker(checkout)
    original = identify_path(checkout / "docs/guide.md")
    _patch(broker)

    assert (checkout / "docs/guide.md").read_text() == "original guide\n"
    assert broker.invoke("read_file", {"path": "docs/guide.md"}).content == (
        "edited guide\n"
    )
    search = broker.invoke("search_text", {"pattern": "edited"})
    assert "docs/guide.md:1: edited guide" in search.content
    assert broker.invoke("search_text", {"pattern": "original"}).content == (
        "(no matches)"
    )
    diff = broker.invoke("read_diff", {}).content
    assert "-original guide" in diff and "+edited guide" in diff
    (candidate,) = broker.candidates()
    assert candidate.base == original
    assert candidate.content == b"edited guide\n"
    assert (
        _broker(checkout).invoke("read_file", {"path": "docs/guide.md"}).content
        == "original guide\n"
    )


def test_existing_full_write_requires_an_explicit_read(checkout: Path) -> None:
    broker = _broker(checkout)
    request = {"path": "docs/guide.md", "content": "replaced\n"}
    assert broker.invoke("write_file", request).is_error
    assert broker.candidates() == ()
    assert not broker.invoke("read_file", {"path": "docs/guide.md"}).is_error
    assert not broker.invoke("write_file", request).is_error
    assert broker.candidates()[0].content == b"replaced\n"
    assert (checkout / "docs/guide.md").read_text() == "original guide\n"


def test_search_is_not_a_full_file_read_authorizing_replacement(checkout: Path) -> None:
    broker = _broker(checkout)
    assert (
        "original guide"
        in broker.invoke("search_text", {"pattern": "original"}).content
    )
    assert broker.invoke(
        "write_file", {"path": "docs/guide.md", "content": "unread replacement"}
    ).is_error
    assert broker.usage_snapshot()["shared_workspace_state"] == {
        "version": 2,
        "files": [],
    }


def test_new_files_are_listed_and_searchable_without_being_created(
    checkout: Path,
) -> None:
    broker = _broker(checkout)
    assert not broker.invoke(
        "write_file", {"path": "docs/new.md", "content": "a new guide\n"}
    ).is_error
    assert not (checkout / "docs/new.md").exists()
    assert "docs/new.md" in broker.invoke("list_files", {"path": "docs"}).content
    assert (
        "docs/new.md:1:"
        in broker.invoke("search_text", {"pattern": "new guide"}).content
    )
    assert "--- /dev/null" in broker.invoke("read_diff", {}).content
    assert broker.candidates()[0].base == ABSENT


def test_multiple_edits_and_external_changes_keep_the_original_base(
    checkout: Path,
) -> None:
    broker = _broker(checkout)
    base = identify_path(checkout / "docs/guide.md")
    _patch(broker)
    (checkout / "docs/guide.md").write_text("another session\n")
    _patch(broker, "edited", "finished")

    (candidate,) = broker.candidates()
    assert candidate.base == base
    assert candidate.content == b"finished guide\n"
    result = broker.invoke("validate_changes", {})
    assert result.is_error and "publication will conflict" in result.content
    assert (checkout / "docs/guide.md").read_text() == "another session\n"


def test_rereading_live_source_refreshes_the_base_before_first_edit(
    checkout: Path,
) -> None:
    broker = _broker(checkout)
    broker.invoke("read_file", {"path": "docs/guide.md"})
    (checkout / "docs/guide.md").write_text("updated guide\n")
    updated_base = identify_path(checkout / "docs/guide.md")
    assert broker.invoke("read_file", {"path": "docs/guide.md"}).content == (
        "updated guide\n"
    )
    _patch(broker, "updated", "final")
    assert broker.candidates()[0].base == updated_base
    assert broker.candidates()[0].content == b"final guide\n"
    assert not broker.invoke("validate_changes", {}).is_error


def test_checkpoint_round_trip_preserves_candidates_and_observed_reads(
    checkout: Path,
) -> None:
    broker = _broker(checkout)
    broker.invoke("read_file", {"path": "src/app.py"})
    _patch(broker)
    broker.invoke("write_file", {"path": "docs/new.md", "content": ""})
    saved = json.loads(json.dumps(broker.usage_snapshot()))
    (checkout / "docs/guide.md").write_text("published by another session\n")

    resumed = _broker(checkout)
    resumed.restore_usage(saved)
    assert resumed.candidates() == broker.candidates()
    assert resumed.usage_snapshot() == saved
    assert (
        "edited guide" in resumed.invoke("read_file", {"path": "docs/guide.md"}).content
    )
    assert resumed.invoke("validate_changes", {}).is_error


def test_a_restored_read_authorizes_edit_against_its_old_base(checkout: Path) -> None:
    broker = _broker(checkout)
    broker.invoke("read_file", {"path": "docs/guide.md"})
    base = identify_path(checkout / "docs/guide.md")
    saved = broker.usage_snapshot()
    (checkout / "docs/guide.md").write_text("changed while stopped\n")
    resumed = _broker(checkout)
    resumed.restore_usage(saved)
    result = resumed.invoke(
        "write_file", {"path": "docs/guide.md", "content": "my retained proposal\n"}
    )
    assert not result.is_error
    assert resumed.candidates()[0].base == base
    assert resumed.invoke("validate_changes", {}).is_error


def test_interrupted_call_snapshot_keeps_only_previously_checkpointed_edits(
    checkout: Path,
) -> None:
    broker = _broker(checkout)
    _patch(broker)
    before_call = broker.usage_snapshot()
    _patch(broker, "edited", "uncertain")
    resumed = _broker(checkout)
    resumed.restore_usage(before_call)
    resumed.record_interrupted_call()
    assert resumed.candidates()[0].content == b"edited guide\n"
    assert resumed.usage.calls == before_call["calls"] + 1  # type: ignore[operator]
    assert (checkout / "docs/guide.md").read_text() == "original guide\n"


@pytest.mark.parametrize("field", ["digest", "mode", "size", "kind"])
def test_checkpoint_identity_tampering_is_rejected(checkout: Path, field: str) -> None:
    broker = _broker(checkout)
    _patch(broker)
    saved = json.loads(json.dumps(broker.usage_snapshot()))
    saved["shared_workspace_state"]["files"][0]["base"][field] = "invalid"
    with pytest.raises(ValueError):
        _broker(checkout).restore_usage(saved)


@pytest.mark.parametrize(
    "change",
    [
        "read_bytes",
        "invalid_encoding",
        "candidate_scope",
        "traversal",
        "duplicate",
        "version",
    ],
)
def test_malformed_checkpoint_files_are_rejected(checkout: Path, change: str) -> None:
    broker = _broker(checkout)
    _patch(broker)
    saved = json.loads(json.dumps(broker.usage_snapshot()))
    state = saved["shared_workspace_state"]
    item = state["files"][0]
    if change == "read_bytes":
        item["read_content"] = base64.b64encode(b"not the observed source").decode()
    elif change == "invalid_encoding":
        item["content"] = "%%%"
    elif change == "candidate_scope":
        item["path"] = "src/app.py"
    elif change == "traversal":
        item["path"] = "../outside"
    elif change == "duplicate":
        state["files"].append(item)
    else:
        state["version"] = True
    with pytest.raises(ValueError):
        _broker(checkout).restore_usage(saved)


def test_missing_observations_cannot_restore_an_execution_that_already_read(
    checkout: Path,
) -> None:
    with pytest.raises(ValueError, match="observations are missing"):
        _broker(checkout).restore_usage({"files_read": 1})
    broker = _broker(checkout)
    broker.restore_usage({"calls": 3})
    assert broker.usage.calls == 3


def test_write_scope_is_enforced_but_outside_source_can_be_read(checkout: Path) -> None:
    broker = _broker(checkout)
    assert not broker.invoke("read_file", {"path": "src/app.py"}).is_error
    assert broker.invoke(
        "write_file", {"path": "src/app.py", "content": "denied"}
    ).is_error
    assert broker.usage.denied == 1
    assert broker.candidates() == ()


@pytest.mark.parametrize("tool", ["write_file", "apply_patch", "read_file"])
def test_symlinks_cannot_be_used_as_source_or_candidates(
    checkout: Path, tool: str
) -> None:
    (checkout / "docs/link").symlink_to(checkout / "src", target_is_directory=True)
    broker = _broker(checkout)
    result = broker.invoke(
        tool,
        {
            "path": "docs/link/app.py",
            "content": "bad",
            "old_text": "hi",
            "new_text": "bye",
        },
    )
    assert result.is_error
    assert broker.candidates() == ()
    assert "docs/link" not in broker.invoke("list_files", {"path": "docs"}).content


def test_missing_parent_directories_remain_private(checkout: Path) -> None:
    broker = _broker(checkout)
    result = broker.invoke(
        "write_file", {"path": "docs/missing/new.md", "content": "new"}
    )
    assert not result.is_error
    assert not (checkout / "docs/missing").exists()


def test_ignored_runtime_and_nested_repository_files_are_not_exposed(
    checkout: Path,
) -> None:
    (checkout / ".env").write_text("private secret")
    (checkout / ".venv").mkdir()
    (checkout / ".venv/runtime.py").write_text("private runtime")
    (checkout / "nested/.git").mkdir(parents=True)
    (checkout / "nested/file.py").write_text("private nested")
    broker = _broker(checkout)
    listing = broker.invoke("list_files", {}).content
    assert ".env" not in listing and ".venv" not in listing and "nested" not in listing
    assert (
        broker.invoke("search_text", {"pattern": "private"}).content == "(no matches)"
    )
    for path in (".env", ".venv/runtime.py", "nested/file.py"):
        assert broker.invoke("read_file", {"path": path}).is_error


def test_full_read_budget_failure_does_not_authorize_a_later_write(
    checkout: Path,
) -> None:
    broker = _broker(checkout, limits=ExecutionLimits(max_read_bytes=2))
    assert broker.invoke("read_file", {"path": "docs/guide.md"}).is_error
    assert broker.invoke(
        "write_file", {"path": "docs/guide.md", "content": "x"}
    ).is_error
    assert broker.candidates() == ()


def test_candidate_sizes_and_expanding_patches_are_bounded(checkout: Path) -> None:
    broker = _broker(checkout)
    assert broker.invoke(
        "write_file",
        {"path": "docs/big.md", "content": "x" * (MAX_SHARED_TEXT_BYTES + 1)},
    ).is_error
    assert broker.invoke(
        "apply_patch",
        {
            "path": "docs/guide.md",
            "old_text": "original",
            "new_text": "x" * MAX_SHARED_TEXT_BYTES,
        },
    ).is_error
    assert broker.candidates() == ()


def test_candidates_are_limited_to_fifty_files(checkout: Path) -> None:
    broker = _broker(checkout)
    for index in range(50):
        assert not broker.invoke(
            "write_file", {"path": f"docs/{index}.md", "content": ""}
        ).is_error
    assert broker.invoke(
        "write_file", {"path": "docs/too-many.md", "content": ""}
    ).is_error
    assert len(broker.candidates()) == 50


def test_total_retained_candidate_bytes_are_bounded(checkout: Path) -> None:
    broker = _broker(checkout)
    for index in range(16):
        assert not broker.invoke(
            "write_file",
            {"path": f"docs/{index}.md", "content": "x" * MAX_SHARED_TEXT_BYTES},
        ).is_error
    assert broker.invoke(
        "write_file", {"path": "docs/overflow.md", "content": "x"}
    ).is_error
    assert len(broker.candidates()) == 16


def test_executable_mode_is_preserved(checkout: Path) -> None:
    (checkout / "docs/guide.md").chmod(0o755)
    broker = _broker(checkout)
    _patch(broker)
    assert broker.candidates()[0].mode == EXECUTABLE_MODE


def test_recovery_guard_returns_a_correctable_tool_error(checkout: Path) -> None:
    def blocked() -> None:
        raise WorkspaceBrokerError("workspace publication needs recovery")

    broker = _broker(checkout, guard=blocked)
    outcome = broker.invoke("read_file", {"path": "docs/guide.md"})
    assert outcome.is_error and "needs recovery" in outcome.content
    assert broker.candidates() == ()


def test_asking_the_operator_does_not_block_other_shared_sessions(
    checkout: Path,
) -> None:
    lock = threading.RLock()
    entered = threading.Event()
    answered = threading.Event()

    def ask(question: str) -> str:
        entered.set()
        if not answered.wait(timeout=5):
            raise RuntimeError("test operator was never answered")
        return f"answer to {question}"

    broker = _broker(checkout, publication_lock=lock, asker=ask)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(broker.invoke, "ask_user", {"question": "continue?"})
        acquired = False
        try:
            assert entered.wait(timeout=2)
            acquired = lock.acquire(timeout=0.2)
            assert acquired, "an operator question must not own the shared read barrier"
        finally:
            if acquired:
                lock.release()
            answered.set()
        assert not pending.result(timeout=2).is_error


def test_noop_writes_and_reverting_edits_leave_no_candidates(checkout: Path) -> None:
    broker = _broker(checkout)
    broker.invoke("read_file", {"path": "docs/guide.md"})
    result = broker.invoke(
        "write_file", {"path": "docs/guide.md", "content": "original guide\n"}
    )
    assert not result.is_error
    assert broker.candidates() == ()
    _patch(broker)
    _patch(broker, "edited", "original")
    assert broker.candidates() == ()
    state = broker.usage_snapshot()["shared_workspace_state"]
    assert isinstance(state, dict)
    assert state["files"][0]["content"] is None
    assert broker.invoke("read_diff", {}).content == "(no changes yet)"


def test_validating_private_changes_does_not_include_unrelated_checkout_edits(
    checkout: Path,
) -> None:
    broker = _broker(checkout)
    _patch(broker)
    (checkout / "src/app.py").write_text("someone else's unrelated change\n")
    outcome = broker.invoke("validate_changes", {})
    assert not outcome.is_error
    assert "docs/guide.md" in outcome.content and "src/app.py" not in outcome.content


def test_single_file_search_uses_private_overlay_without_authorizing_writes(
    checkout: Path,
) -> None:
    broker = _broker(checkout)
    result = broker.invoke("search_text", {"path": "docs/guide.md", "pattern": "guide"})
    assert result.content == "docs/guide.md:1: original guide"
    assert not result.is_error
    assert broker.invoke(
        "write_file", {"path": "docs/guide.md", "content": "unread replacement"}
    ).is_error
    _patch(broker)
    result = broker.invoke("search_text", {"path": "docs/guide.md", "pattern": "guide"})
    assert result.content == "docs/guide.md:1: edited guide"
    broker.invoke("write_file", {"path": "docs/new.md", "content": "new guide"})
    result = broker.invoke("search_text", {"path": "docs/new.md", "pattern": "guide"})
    assert result.content == "docs/new.md:1: new guide"
    broker.invoke("delete_file", {"path": "docs/guide.md"})
    assert (
        broker.invoke(
            "search_text", {"path": "docs/guide.md", "pattern": "guide"}
        ).content
        == "(no matches)"
    )
    assert (checkout / "docs/guide.md").read_text() == "original guide\n"


def test_single_file_search_retains_shared_read_boundaries(checkout: Path) -> None:
    (checkout / ".env").write_text("SECRET")
    (checkout / "docs/link.md").symlink_to(checkout / "docs/guide.md")
    (checkout / "nested/.git").mkdir(parents=True)
    (checkout / "nested/source.py").write_text("SECRET")
    broker = _broker(checkout)
    for path in (
        ".env",
        "docs/link.md",
        "nested/source.py",
        "../outside",
        "/etc/passwd",
    ):
        result = broker.invoke("search_text", {"path": path, "pattern": "."})
        assert result.is_error, path
        assert "SECRET" not in result.content
    assert not broker.invoke(
        "search_text", {"path": "src/app.py", "pattern": "print"}
    ).is_error


def test_missing_path_exposes_invisible_characters_without_rewriting(
    checkout: Path,
) -> None:
    broker = _broker(checkout)
    result = broker.invoke("read_file", {"path": "src/ap\u200bp.py"})
    assert result.is_error
    assert r"src/ap\u200bp.py" in result.content
    assert "list_files" in result.content
    assert "write_file" not in result.content
    assert "\u200b" not in result.content
    assert not broker.invoke("read_file", {"path": "src/app.py"}).is_error
    (checkout / "src/ap\u200bp.py").write_text("exact filename")
    assert (
        broker.invoke("read_file", {"path": "src/ap\u200bp.py"}).content
        == "exact filename"
    )
