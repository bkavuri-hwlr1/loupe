"""The tool surface a model drives, and the boundaries it must not cross.

A denied write returns a correctable error rather than raising: a path found
outside scope is supposed to make the model replan, not kill the run.  What
must never happen is the write landing anyway, so every denial here asserts the
filesystem too.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from llm_cli.agent.limits import ExecutionLimits
from llm_cli.agent.tools import MAX_ANSWER_CHARACTERS, ToolBroker, ToolBudgetExhausted
from llm_cli.git.worktrees import create_managed_worktree


def _git(path: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A real managed linked worktree, because the broker refuses any other.

    Git operations here deliberately reject the primary worktree, so exercising
    the broker against a plain repository would test a path production never
    takes.
    """

    repository = tmp_path / "repository"
    (repository / "docs").mkdir(parents=True)
    (repository / "src").mkdir()
    (repository / "docs" / "guide.md").write_text("original guide\n", encoding="utf-8")
    (repository / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "base")
    base_oid = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1"},
    ).stdout.strip()
    managed = create_managed_worktree(
        repository,
        managed_root=tmp_path / "managed",
        task_id="task-broker",
        base_oid=base_oid,
    )
    return managed.path


def _broker(worktree: Path, *scopes: str, **kwargs: object) -> ToolBroker:
    return ToolBroker(worktree=worktree, scopes=scopes or ("docs/",), **kwargs)  # type: ignore[arg-type]


def test_a_write_inside_the_claim_lands(worktree: Path) -> None:
    broker = _broker(worktree, "docs/")

    outcome = broker.invoke(
        "write_file", {"path": "docs/new.md", "content": "written\n"}
    )

    assert not outcome.is_error
    assert (worktree / "docs" / "new.md").read_text(encoding="utf-8") == "written\n"
    assert broker.usage.writes == 1


def test_a_write_outside_the_claim_is_refused_and_never_reaches_disk(
    worktree: Path,
) -> None:
    broker = _broker(worktree, "docs/")

    outcome = broker.invoke(
        "write_file", {"path": "src/app.py", "content": "clobbered\n"}
    )

    assert outcome.is_error
    assert "outside this task's claimed scopes" in outcome.content
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert broker.usage.denied == 1
    assert broker.usage.writes == 0


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        "docs/../../escape.md",
        "/etc/passwd",
        ".git/config",
        "docs/../.git/hooks/pre-commit",
    ],
)
def test_unsafe_paths_are_refused(worktree: Path, path: str) -> None:
    broker = _broker(worktree, "*")

    outcome = broker.invoke("write_file", {"path": path, "content": "x"})

    assert outcome.is_error
    assert not (worktree.parent / "escape.md").exists()


def test_a_symlinked_directory_cannot_be_written_through(
    worktree: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktree / "docs" / "link").symlink_to(outside, target_is_directory=True)
    broker = _broker(worktree, "docs/")

    outcome = broker.invoke(
        "write_file", {"path": "docs/link/escaped.md", "content": "escaped\n"}
    )

    assert outcome.is_error
    assert not (outside / "escaped.md").exists()


def test_reads_are_bounded_by_the_worktree_but_not_by_the_claim(
    worktree: Path,
) -> None:
    # Reading outside the claim is how a model builds context; only writing is
    # an authority question, so src/ stays readable under a docs/ claim.
    broker = _broker(worktree, "docs/")

    readable = broker.invoke("read_file", {"path": "src/app.py"})
    escaping = broker.invoke("read_file", {"path": "../../etc/hosts"})

    assert not readable.is_error
    assert readable.content == "print('hi')\n"
    assert escaping.is_error


def test_a_case_folded_alias_is_refused_on_a_case_insensitive_tree(
    worktree: Path,
) -> None:
    broker = _broker(worktree, "docs/", case_insensitive_filesystem=True)

    outcome = broker.invoke("write_file", {"path": "Docs/guide.md", "content": "x"})

    assert outcome.is_error
    assert broker.usage.writes == 0


def test_apply_patch_refuses_an_ambiguous_anchor(worktree: Path) -> None:
    (worktree / "docs" / "twice.md").write_text("same\nsame\n", encoding="utf-8")
    broker = _broker(worktree, "docs/")

    outcome = broker.invoke(
        "apply_patch",
        {"path": "docs/twice.md", "old_text": "same\n", "new_text": "other\n"},
    )

    assert outcome.is_error
    assert "appears 2 times" in outcome.content
    assert (worktree / "docs" / "twice.md").read_text(
        encoding="utf-8"
    ) == "same\nsame\n"


def test_validate_changes_reports_a_change_made_behind_the_broker(
    worktree: Path,
) -> None:
    # A cooperative driver can write straight to disk. The broker cannot stop
    # that, but it must tell the model the change would be refused later.
    (worktree / "src" / "sneaky.py").write_text("sneaky\n", encoding="utf-8")
    broker = _broker(worktree, "docs/")

    outcome = broker.invoke("validate_changes", {})

    assert outcome.is_error
    assert "src/sneaky.py" in outcome.content


def test_validate_changes_accepts_an_in_scope_change(worktree: Path) -> None:
    broker = _broker(worktree, "docs/")
    broker.invoke("write_file", {"path": "docs/guide.md", "content": "edited\n"})

    outcome = broker.invoke("validate_changes", {})

    assert not outcome.is_error
    assert "docs/guide.md" in outcome.content


def test_tool_output_is_truncated_to_the_limit(worktree: Path) -> None:
    limits = ExecutionLimits(max_tool_output_bytes=256)
    (worktree / "docs" / "big.md").write_text("x" * 10_000, encoding="utf-8")
    broker = _broker(worktree, "docs/", limits=limits)

    outcome = broker.invoke("read_file", {"path": "docs/big.md"})

    assert not outcome.is_error
    assert "output truncated" in outcome.content
    assert len(outcome.content.encode("utf-8")) < 1_000


def test_the_tool_call_budget_is_a_hard_stop(worktree: Path) -> None:
    broker = _broker(worktree, "docs/", limits=ExecutionLimits(max_tool_calls=2))

    broker.invoke("list_files", {"path": "."})
    broker.invoke("list_files", {"path": "."})

    with pytest.raises(ToolBudgetExhausted):
        broker.invoke("list_files", {"path": "."})


def test_finishing_closes_the_tool_surface(worktree: Path) -> None:
    broker = _broker(worktree, "docs/")

    finished = broker.invoke(
        "finish_task",
        {"answer": "The guide now covers setup.", "summary": "did the thing"},
    )
    afterwards = broker.invoke("write_file", {"path": "docs/late.md", "content": "no"})

    assert not finished.is_error
    assert broker.usage.finished
    assert broker.usage.summary == "did the thing"
    assert broker.usage.outcome == "completed"
    assert broker.usage.answer == "The guide now covers setup."
    assert afterwards.is_error
    assert not (worktree / "docs" / "late.md").exists()


def test_answer_is_preserved_separately_from_bounded_audit_summary(
    worktree: Path,
) -> None:
    broker = _broker(worktree, "docs/")
    answer = "The requested explanation.\n" * 400
    report = "audit " * 1000
    result = broker.invoke("finish_task", {"answer": answer, "summary": report})
    assert not result.is_error
    assert broker.usage.answer == answer
    assert broker.usage.summary == report[:2000]
    restored = _broker(worktree, "docs/")
    restored.restore_usage(broker.usage_snapshot())
    assert restored.usage.answer == answer
    assert restored.usage.outcome == "completed"


@pytest.mark.parametrize(
    "arguments",
    [
        {"summary": "Prepared answer"},
        {"summary": "Could not proceed", "outcome": "blocked"},
        {"summary": "Partly done", "outcome": "partial"},
        {"answer": "", "summary": "Prepared answer"},
        {"answer": "  "},
        {"answer": "x" * (MAX_ANSWER_CHARACTERS + 1)},
        {"answer": "Done", "outcome": "published"},
        {},
    ],
)
def test_invalid_answer_intents_do_not_complete_the_task(
    worktree: Path, arguments: dict[str, object]
) -> None:
    broker = _broker(worktree, "docs/")
    assert broker.invoke("finish_task", arguments).is_error
    assert not broker.usage.finished


def test_legacy_tool_checkpoint_does_not_promote_report_into_answer(
    worktree: Path,
) -> None:
    broker = _broker(worktree, "docs/")
    broker.restore_usage({"calls": 1, "finished": True, "summary": "Read the files"})
    assert broker.usage.finished and broker.usage.summary == "Read the files"
    assert broker.usage.answer == ""


def test_ask_user_is_absent_without_an_attached_session(worktree: Path) -> None:
    background = _broker(worktree, "docs/")
    interactive = _broker(
        worktree, "docs/", asker=lambda question: f"answer:{question}"
    )

    assert "ask_user" not in background.tool_names()
    assert background.invoke("ask_user", {"question": "which one?"}).is_error
    assert "ask_user" in interactive.tool_names()
    assert interactive.invoke("ask_user", {"question": "which?"}).content == (
        "answer:which?"
    )


def test_search_finds_matches_and_skips_the_git_directory(worktree: Path) -> None:
    broker = _broker(worktree, "docs/")

    outcome = broker.invoke("search_text", {"pattern": r"print\("})

    assert not outcome.is_error
    assert "src/app.py:1:" in outcome.content
    assert ".git/" not in outcome.content


@pytest.mark.parametrize("tool", ["write_file", "apply_patch"])
def test_no_write_tool_can_reach_outside_the_claim_through_a_symlink(
    worktree: Path, tool: str
) -> None:
    """A link inside the claim pointing elsewhere inside the worktree.

    Final-containment checking alone accepts this: the resolved path is still
    under the worktree. Only refusing symlinked components catches it, and
    every write tool has to do that -- one that does not is a way around the
    claim, whatever the others do.
    """

    (worktree / "docs" / "link").symlink_to(worktree / "src", target_is_directory=True)
    broker = _broker(worktree, "docs/")

    outcome = broker.invoke(
        tool,
        {"path": "docs/link/app.py", "content": "pwned\n"}
        if tool == "write_file"
        else {
            "path": "docs/link/app.py",
            "old_text": "print('hi')",
            "new_text": "pwned",
        },
    )

    assert outcome.is_error
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_search_accepts_a_single_file_and_keeps_path_boundaries(worktree: Path) -> None:
    broker = _broker(worktree)
    result = broker.invoke("search_text", {"path": "src/app.py", "pattern": "print"})
    assert not result.is_error
    assert result.content == "src/app.py:1: print('hi')"
    assert (
        broker.invoke(
            "search_text", {"path": "docs/guide.md", "pattern": "print"}
        ).content
        == "(no matches)"
    )
    for path in ("../outside", "/etc/passwd", ".git/config"):
        assert broker.invoke("search_text", {"path": path, "pattern": "."}).is_error


def test_unreadable_path_makes_invisible_characters_visible(worktree: Path) -> None:
    result = _broker(worktree).invoke("read_file", {"path": "src/ap\u200bp.py"})
    assert result.is_error
    assert r"src/ap\u200bp.py" in result.content
    assert "\u200b" not in result.content
    assert "list_files" in result.content
