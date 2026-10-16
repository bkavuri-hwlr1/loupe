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
from llm_cli.agent.shared_tools import CATCH_UP_ROUNDS, SharedToolBroker
from llm_cli.agent.tools import ToolOutcome
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
        "version": 3,
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


def test_external_changes_keep_the_original_base_until_reconciled(
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
    # Both changed the only line: validating merges, and the edits conflict.
    result = broker.invoke("validate_changes", {})
    assert result.is_error and "conflict markers" in result.content
    (candidate,) = broker.candidates()
    assert candidate.base == identify_path(checkout / "docs/guide.md")
    assert candidate.content == (
        b"<<<<<<< your pending edits\nfinished guide\n"
        b"||||||| your starting version\noriginal guide\n"
        b"=======\nanother session\n>>>>>>> current checkout\n"
    )
    assert (checkout / "docs/guide.md").read_text() == "another session\n"


_LINES = "one\ntwo\nthree\nfour\nfive\n"


def test_edits_merge_onto_a_file_that_changed_underneath_them(
    checkout: Path,
) -> None:
    (checkout / "docs/guide.md").write_text(_LINES)
    broker = _broker(checkout)
    _patch(broker, "one", "ONE")
    (checkout / "docs/guide.md").write_text(_LINES.replace("five", "FIVE"))

    result = broker.invoke("validate_changes", {})

    assert not result.is_error, result.content
    assert "docs/guide.md: your edits were merged" in result.content
    (candidate,) = broker.candidates()
    assert candidate.base == identify_path(checkout / "docs/guide.md")
    assert candidate.content == b"ONE\ntwo\nthree\nfour\nFIVE\n"
    assert candidate.original == _LINES.replace("five", "FIVE").encode()
    # The model has not seen the merged file, so it cannot overwrite it blind.
    write = {"path": "docs/guide.md", "content": "ONE\n"}
    assert broker.invoke("write_file", write).is_error
    assert "FIVE" in broker.invoke("read_file", {"path": "docs/guide.md"}).content
    assert not broker.invoke("write_file", write).is_error


def test_a_conflict_must_be_resolved_before_checks_or_finishing(
    checkout: Path,
) -> None:
    (checkout / "docs/guide.md").write_text(_LINES)
    broker = _broker(checkout)
    _patch(broker, "two", "TWO")
    (checkout / "docs/guide.md").write_text(_LINES.replace("two", "2"))
    ran: list[str] = []
    broker.check_runner = broker.catching_up(
        lambda name: ran.append(name) or ToolOutcome("passed", False)
    )
    broker.finish_gate = lambda: broker.finish_gate_with(None, lambda: True)

    check = broker.invoke("run_check", {"name": "test"})
    finish = broker.invoke("finish_task", {"answer": "done"})

    assert check.is_error and "conflict markers (<<<<<<< your pending" in check.content
    assert not ran
    assert finish.is_error and "Resolve the conflict markers in docs/guide.md" in (
        finish.content
    )
    assert broker.unresolved() == ["docs/guide.md"]
    text = broker.invoke("read_file", {"path": "docs/guide.md"}).content
    conflict = text[text.index("<<<<<<<") : text.index("current checkout\n") + 17]
    _patch(broker, conflict, "TWO and 2\n")
    assert broker.unresolved() == []
    assert not broker.invoke("run_check", {"name": "test"}).is_error
    assert ran == ["test"]
    assert not broker.invoke("finish_task", {"answer": "done"}).is_error
    assert broker.candidates()[0].content == b"one\nTWO and 2\nthree\nfour\nfive\n"


def test_merging_only_if_clean_leaves_conflicting_edits_alone(
    checkout: Path,
) -> None:
    (checkout / "docs/guide.md").write_text(_LINES)
    broker = _broker(checkout)
    _patch(broker, "one", "ONE")
    _patch(broker, "five", "FIVE")
    before = broker.usage_snapshot()
    (checkout / "docs/guide.md").write_text(_LINES.replace("five", "5"))

    report = broker.reconcile(only_if_clean=True)

    assert report.conflicted == ("docs/guide.md",) and not report.clean
    assert broker.usage_snapshot() == before


def test_edits_the_checkout_already_has_are_no_longer_pending(
    checkout: Path,
) -> None:
    broker = _broker(checkout)
    _patch(broker)
    (checkout / "docs/guide.md").write_text("edited guide\n")

    report = broker.reconcile()

    assert report.adopted == ("docs/guide.md",) and report.clean
    assert broker.candidates() == ()


@pytest.mark.parametrize("ours_deleted", [True, False])
def test_a_deletion_against_a_change_sets_the_edit_aside(
    checkout: Path, ours_deleted: bool
) -> None:
    broker = _broker(checkout)
    if ours_deleted:
        assert not broker.invoke("delete_file", {"path": "docs/guide.md"}).is_error
        (checkout / "docs/guide.md").write_text("changed guide\n")
    else:
        _patch(broker)
        (checkout / "docs/guide.md").unlink()

    report = broker.reconcile()

    assert report.dropped == ("docs/guide.md",) and not report.clean
    assert "your edit was set aside" in report.note()
    # Nothing pending: the other change stands, and the model can redo its own.
    assert broker.candidates() == ()


def test_finishing_stops_once_when_an_edit_was_set_aside(checkout: Path) -> None:
    broker = _broker(checkout)
    _patch(broker)
    assert not broker.invoke(
        "write_file", {"path": "docs/new.md", "content": "new\n"}
    ).is_error
    (checkout / "docs/guide.md").unlink()
    finish = lambda: broker.finish_gate_with(None, lambda: True)  # noqa: E731

    refused = finish()

    # The rest of the change is not published until the agent has seen this.
    assert refused is not None and "your edit was set aside" in refused.content
    assert [c.relative_path for c in broker.candidates()] == ["docs/new.md"]
    assert finish() is None


def test_a_change_that_cannot_be_merged_still_refuses_publication(
    checkout: Path,
) -> None:
    broker = _broker(checkout)
    _patch(broker)
    (checkout / "docs/guide.md").write_bytes(b"\xff\xfe binary\n")

    result = broker.invoke("validate_changes", {})

    assert result.is_error and "cannot be merged" in result.content
    assert "publication will conflict: docs/guide.md" in result.content
    assert broker.stale_paths() == ["docs/guide.md"]
    # Nor can a path that became a symlink, which tools may not read at all.
    (checkout / "docs/guide.md").unlink()
    (checkout / "docs/guide.md").symlink_to(checkout / "src/app.py")
    assert broker.reconcile().unmerged == ("docs/guide.md",)
    assert broker.stale_paths() == ["docs/guide.md"]


def test_finishing_merges_and_rechecks_until_the_checkout_holds_still(
    checkout: Path,
) -> None:
    (checkout / "docs/guide.md").write_text(_LINES)
    broker = _broker(checkout)
    _patch(broker, "one", "ONE")
    publications = iter(["three", "five"])
    checked: list[bytes | None] = []

    def gate() -> None:
        # Another session publishes while the first two check runs go.
        checked.append(broker.candidates()[0].content)
        word = next(publications, None)
        if word is not None:
            path = checkout / "docs/guide.md"
            path.write_text(path.read_text().replace(word, word.upper()))

    assert broker.finish_gate_with(gate, lambda: True) is None
    assert checked == [
        b"ONE\ntwo\nthree\nfour\nfive\n",
        b"ONE\ntwo\nTHREE\nfour\nfive\n",
        b"ONE\ntwo\nTHREE\nfour\nFIVE\n",
    ]
    assert broker.stale_paths() == []


def test_finishing_stops_catching_up_after_a_few_rounds(checkout: Path) -> None:
    (checkout / "docs/guide.md").write_text(_LINES)
    broker = _broker(checkout)
    _patch(broker, "one", "ONE")
    runs: list[None] = []

    def gate() -> None:
        runs.append(None)
        path = checkout / "docs/guide.md"
        path.write_text(path.read_text() + "more\n")

    # Publication catches up again, so the agent is not sent back to retry.
    assert broker.finish_gate_with(gate, lambda: True) is None
    assert len(runs) == CATCH_UP_ROUNDS
    assert broker.stale_paths() == ["docs/guide.md"]


def test_merged_edits_survive_a_checkpoint(checkout: Path) -> None:
    (checkout / "docs/guide.md").write_text(_LINES)
    broker = _broker(checkout)
    _patch(broker, "one", "ONE")
    (checkout / "docs/guide.md").write_text(_LINES.replace("five", "FIVE"))
    broker.reconcile()

    resumed = _broker(checkout)
    resumed.restore_usage(json.loads(json.dumps(broker.usage_snapshot())))

    assert resumed.candidates() == broker.candidates()
    assert resumed.stale_paths() == []
    # Still unseen after the restart, so still not replaceable blind.
    assert resumed.invoke(
        "write_file", {"path": "docs/guide.md", "content": "x\n"}
    ).is_error


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


@pytest.mark.parametrize("body", [b"\x00\xffbinary", b"ascii binary payload"])
def test_binary_edits_resume_without_merging(checkout: Path, body: bytes) -> None:
    path = checkout / "docs/data.bin"
    path.write_bytes(body)
    broker = _broker(checkout)
    assert broker.invoke(
        "write_binary", {"path": "docs/data.bin", "content_base64": "Yg=="}
    ).is_error
    assert not broker.invoke("read_binary", {"path": "docs/data.bin"}).is_error
    assert not broker.invoke(
        "write_binary", {"path": "docs/data.bin", "content_base64": "Yg=="}
    ).is_error
    restored = _broker(checkout)
    restored.restore_usage(broker.usage_snapshot())
    assert restored.candidates()[0].binary
    assert "Binary change" in restored.invoke("read_diff", {}).content
    path.write_bytes(b"a concurrent replacement")
    assert restored.reconcile().unmerged == ("docs/data.bin",)
    assert restored.candidates()[0].original == body
    assert restored.candidates()[0].content == b"b"


def test_binary_limits_and_truncated_read_preserve_observation(checkout: Path) -> None:
    (checkout / "docs/data.bin").write_bytes(b"x" * 100)
    broker = _broker(checkout, limits=ExecutionLimits(max_tool_output_bytes=20))
    assert (
        "truncated" in broker.invoke("read_binary", {"path": "docs/data.bin"}).content
    )
    assert broker.invoke(
        "write_binary", {"path": "docs/data.bin", "content_base64": "eA=="}
    ).is_error
    assert broker.invoke(
        "write_binary", {"path": "docs/new.bin", "content_base64": "not base64"}
    ).is_error
    assert broker.invoke(
        "write_binary",
        {"path": "docs/new.bin", "content_base64": "A" * (MAX_SHARED_TEXT_BYTES * 2)},
    ).is_error
    assert broker.candidates() == ()


def test_symlink_create_retarget_delete_are_private(checkout: Path) -> None:
    from llm_cli.workspace.identity import SYMLINK_MODE, ObjectKind

    broker = _broker(checkout)
    assert not broker.invoke(
        "write_symlink", {"path": "docs/link", "target": "guide.md"}
    ).is_error
    assert broker.candidates()[0].mode == SYMLINK_MODE
    assert not (checkout / "docs/link").is_symlink()
    assert broker.invoke("read_symlink", {"path": "docs/link"}).content == "guide.md"
    restored = _broker(checkout)
    restored.restore_usage(broker.usage_snapshot())
    assert restored.candidates()[0].content == b"guide.md"
    (checkout / "docs/link").symlink_to("guide.md")
    broker = _broker(checkout)
    assert broker.invoke(
        "write_symlink", {"path": "docs/link", "target": "new.md"}
    ).is_error
    assert broker.invoke("read_symlink", {"path": "docs/link"}).content == "guide.md"
    assert not broker.invoke(
        "write_symlink", {"path": "docs/link", "target": "new.md"}
    ).is_error
    assert broker.candidates()[0].base.kind is ObjectKind.SYMLINK
    assert broker.candidates()[0].original == b"guide.md"
    assert "Symlink" in broker.invoke("read_diff", {}).content
    broker = _broker(checkout)
    assert not broker.invoke("delete_file", {"path": "docs/link"}).is_error
    assert broker.candidates()[0].content is None
    assert (checkout / "docs/link").is_symlink()


@pytest.mark.parametrize(
    "target",
    [
        "/tmp/outside",
        "../../outside",
        "../src/app.py",
        "../.git/config",
        "../.env",
        "link",
    ],
)
def test_symlink_escape_or_out_of_scope_target_refused(
    checkout: Path, target: str
) -> None:
    broker = _broker(checkout)
    assert broker.invoke(
        "write_symlink", {"path": "docs/link", "target": target}
    ).is_error
    assert broker.candidates() == ()


def test_binary_and_link_tools_are_read_only_for_helpers(checkout: Path) -> None:
    broker = _broker(checkout).read_only_view(ExecutionLimits())
    assert "write_binary" not in broker.tool_names()
    assert "write_symlink" not in broker.tool_names()
    assert broker.invoke(
        "write_binary", {"path": "docs/new", "content_base64": "eA=="}
    ).is_error


def test_escaping_existing_link_cannot_enter_an_undoable_candidate(
    checkout: Path,
) -> None:
    (checkout / "docs/escape").symlink_to("../../outside")
    broker = _broker(checkout)
    assert broker.invoke("delete_file", {"path": "docs/escape"}).is_error
    assert broker.candidates() == ()
    assert not broker.invoke("read_symlink", {"path": "docs/escape"}).is_error
    assert broker.invoke(
        "write_symlink", {"path": "docs/escape", "target": "guide.md"}
    ).is_error
    assert broker.candidates() == ()


def test_metadata_binary_read_supports_full_size_opaque_replacement(
    checkout: Path,
) -> None:
    body = b"\x00" * MAX_SHARED_TEXT_BYTES
    (checkout / "docs/data.bin").write_bytes(body)
    broker = _broker(checkout)
    read = broker.invoke(
        "read_binary", {"path": "docs/data.bin", "metadata_only": True}
    )
    assert not read.is_error
    assert json.loads(read.content)["bytes"] == MAX_SHARED_TEXT_BYTES
    write = broker.invoke(
        "write_binary",
        {
            "path": "docs/data.bin",
            "content_base64": base64.b64encode(b"\xff" * MAX_SHARED_TEXT_BYTES).decode(
                "ascii"
            ),
        },
    )
    assert not write.is_error, write.content
    assert broker.candidates()[0].original == body
