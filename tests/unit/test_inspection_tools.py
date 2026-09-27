"""Bounded source inspection remains useful without broadening edit authority."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from llm_cli.agent.limits import ExecutionLimits
from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.tools import ToolBroker
from llm_cli.workspace.identity import identify_path


@pytest.fixture(params=[ToolBroker, SharedToolBroker], ids=["isolated", "shared"])
def broker(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    repository_factory: Callable[..., Path],
) -> ToolBroker:
    root = repository_factory(
        tmp_path,
        {"guide.txt": "first line\nsecond line\nthird line\nfourth line\n"},
    )
    kind: type[ToolBroker] = request.param
    return kind(root, ("*",))


def test_numbered_range_reports_next_line_and_preserves_default_read(
    broker: ToolBroker,
) -> None:
    partial = broker.invoke(
        "read_file", {"path": "guide.txt", "start_line": 2, "end_line": 3}
    )
    assert not partial.is_error
    assert partial.content.startswith("2: second line\n3: third line\n")
    assert partial.metadata["next_start_line"] == 4
    assert partial.metadata["complete_file"] is False
    assert partial.metadata["truncated"] is True
    complete = broker.invoke("read_file", {"path": "guide.txt"})
    assert complete.content == "first line\nsecond line\nthird line\nfourth line\n"
    assert complete.metadata["complete_file"] is True


def test_partial_read_does_not_authorize_whole_file_replacement(
    broker: ToolBroker,
) -> None:
    assert not broker.invoke("read_file", {"path": "guide.txt", "end_line": 1}).is_error
    denied = broker.invoke(
        "write_file", {"path": "guide.txt", "content": "first line\n"}
    )
    assert denied.is_error and "unseen content" in denied.content
    assert (broker.worktree / "guide.txt").read_text().endswith("fourth line\n")
    assert not broker.invoke("read_file", {"path": "guide.txt"}).is_error
    assert not broker.invoke(
        "write_file", {"path": "guide.txt", "content": "replacement\n"}
    ).is_error


def test_partial_read_restriction_survives_checkpoint(broker: ToolBroker) -> None:
    broker.invoke("read_file", {"path": "guide.txt", "end_line": 1})
    restored = type(broker)(broker.worktree, ("*",))
    restored.restore_usage(broker.usage_snapshot())
    assert restored.invoke(
        "write_file", {"path": "guide.txt", "content": "unsafe\n"}
    ).is_error
    assert not restored.invoke(
        "apply_patch",
        {"path": "guide.txt", "old_text": "first line", "new_text": "updated line"},
    ).is_error
    visible = restored.invoke("read_file", {"path": "guide.txt"}).content
    assert visible == "updated line\nsecond line\nthird line\nfourth line\n"


@pytest.mark.parametrize(
    "options",
    [
        {"start_line": 0},
        {"start_line": True},
        {"start_column": -1},
        {"start_line": 3, "end_line": 2},
        {"start_line": 100},
        {"start_column": 100},
    ],
)
def test_invalid_read_ranges_never_grant_replacement_authority(
    broker: ToolBroker, options: dict[str, object]
) -> None:
    assert broker.invoke("read_file", {"path": "guide.txt", **options}).is_error
    if isinstance(broker, SharedToolBroker):
        assert broker.invoke(
            "write_file", {"path": "guide.txt", "content": "unsafe"}
        ).is_error


def test_long_unicode_line_has_lossless_bounded_continuation(
    broker: ToolBroker,
) -> None:
    original = "\u03b1🙂Z" * 100 + "\n"
    (broker.worktree / "guide.txt").write_text(original)
    broker.limits = ExecutionLimits(max_tool_output_bytes=320)
    options: dict[str, object] = {"path": "guide.txt", "start_line": 1}
    reconstructed: list[str] = []
    for _ in range(30):
        result = broker.invoke("read_file", options)
        assert not result.is_error, result.content
        assert len(result.content.encode("utf-8")) <= 320
        body = result.content.split("\n[", 1)[0]
        reconstructed.append(re.sub(r"^1: ", "", body))
        next_line = result.metadata["next_start_line"]
        if next_line is None:
            break
        next_column = result.metadata["next_start_column"]
        assert isinstance(next_column, int)
        assert next_column > options.get("start_column", 1)
        options = {
            "path": "guide.txt",
            "start_line": next_line,
            "start_column": next_column,
        }
    else:
        pytest.fail("read continuation did not advance")
    assert "".join(reconstructed) == original


@pytest.mark.parametrize("tool", ["list_files", "search_text"])
def test_paged_results_have_no_gaps_and_respect_hard_result_limit(
    broker: ToolBroker, tool: str
) -> None:
    for index in range(5):
        (broker.worktree / f"match-{index}.txt").write_text("needle\n")
    broker.limits = ExecutionLimits(max_search_results=2)
    options: dict[str, object] = {"limit": 10, "offset": 0}
    if tool == "search_text":
        options["pattern"] = "needle"
    entries: list[str] = []
    for _ in range(5):
        result = broker.invoke(tool, options)
        assert not result.is_error
        assert result.metadata["returned"] <= 2
        entries.extend(result.content.split("\n[", 1)[0].splitlines())
        following = result.metadata["next_offset"]
        if following is None:
            break
        options["offset"] = following
        options["snapshot"] = result.metadata["snapshot"]
    assert len(entries) == (6 if tool == "list_files" else 5)
    assert len(set(entries)) == len(entries)


def test_search_scan_limit_has_no_misleading_continuation(broker: ToolBroker) -> None:
    (broker.worktree / "z.txt").write_text("needle\n")
    broker.limits = ExecutionLimits(max_scanned_files=1)
    result = broker.invoke("search_text", {"pattern": "line"})
    assert result.metadata["truncated"] is True
    assert result.metadata["next_offset"] is None
    assert "narrow path" in str(result.metadata["stop_reason"])


def test_ranges_charge_visible_source_bytes_instead_of_the_whole_file(
    broker: ToolBroker,
) -> None:
    (broker.worktree / "guide.txt").write_text("first\n" + "short\n" * 999)
    broker.limits = ExecutionLimits(max_read_bytes=100)
    for number in range(1, 11):
        result = broker.invoke(
            "read_file", {"path": "guide.txt", "start_line": number, "end_line": number}
        )
        assert not result.is_error, result.content
        assert result.metadata["bytes_returned"] == len("short\n")
    assert broker.usage.bytes_read == 60
    assert not broker.invoke(
        "apply_patch",
        {
            "path": "guide.txt",
            "old_text": "first\n",
            "new_text": "changed\n",
        },
    ).is_error


def test_output_byte_cap_continuation_advances_only_visible_search_results(
    broker: ToolBroker,
) -> None:
    (broker.worktree / "guide.txt").write_text(
        "".join(f"needle {number} " + "x" * 60 + "\n" for number in range(5))
    )
    broker.limits = ExecutionLimits(max_tool_output_bytes=256)
    offset = 0
    snapshot: object = None
    received: list[str] = []
    for _ in range(6):
        result = broker.invoke(
            "search_text",
            {
                "path": "guide.txt",
                "pattern": "needle",
                "limit": 5,
                "offset": offset,
                **({"snapshot": snapshot} if snapshot is not None else {}),
            },
        )
        assert not result.is_error, result.content
        assert len(result.content.encode("utf-8")) <= 256
        visible = result.content.split("\n[", 1)[0].splitlines()
        assert len(visible) == result.metadata["returned"]
        received.extend(visible)
        following = result.metadata["next_offset"]
        if following is None:
            break
        assert following == offset + len(visible)
        assert isinstance(following, int)
        offset = following
        snapshot = result.metadata["snapshot"]
    assert [line.split(":", 2)[1] for line in received] == ["1", "2", "3", "4", "5"]


def test_directory_enumeration_is_bounded_and_reports_its_limit(
    broker: ToolBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(10):
        (broker.worktree / f"entry-{number}.txt").write_text("content\n")
    broker.limits = ExecutionLimits(max_scanned_files=3)
    original = Path.iterdir
    enumerated: list[Path] = []

    def tracked(path: Path):
        for child in original(path):
            if path == broker.worktree:
                enumerated.append(child)
            yield child

    monkeypatch.setattr(Path, "iterdir", tracked)
    result = broker.invoke("list_files", {})
    assert not result.is_error
    assert len(enumerated) == 4
    assert result.metadata["truncated"] is True
    assert "scanned_file_limit" in str(result.metadata["stop_reason"])


def test_shared_partial_patch_keeps_its_exact_base_across_peer_changes(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root = repository_factory(tmp_path, {"guide.txt": "seen\nunseen original\n"})
    broker = SharedToolBroker(root, ("*",))
    base = identify_path(root / "guide.txt")
    broker.invoke("read_file", {"path": "guide.txt", "end_line": 1})
    (root / "guide.txt").write_text("seen\nunseen peer edit\n")
    next_page = broker.invoke("read_file", {"path": "guide.txt", "start_line": 2})
    assert "unseen original" in next_page.content
    assert "peer edit" not in next_page.content
    assert not broker.invoke(
        "apply_patch",
        {"path": "guide.txt", "old_text": "seen\n", "new_text": "changed\n"},
    ).is_error
    candidate = broker.candidates()[0]
    assert candidate.base == base
    assert candidate.content == b"changed\nunseen original\n"
    assert broker.invoke("validate_changes", {}).is_error
    assert (root / "guide.txt").read_text() == "seen\nunseen peer edit\n"


def test_shared_range_reads_and_search_pages_preserve_private_overlay(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root = repository_factory(tmp_path, {"guide.txt": "first\noriginal\n"})
    broker = SharedToolBroker(root, ("*",))
    broker.invoke(
        "apply_patch",
        {"path": "guide.txt", "old_text": "original", "new_text": "private"},
    )
    result = broker.invoke("read_file", {"path": "guide.txt", "start_line": 2})
    assert result.content.startswith("2: private\n")
    search = broker.invoke("search_text", {"pattern": "private", "limit": 1})
    assert "guide.txt:2: private" in search.content
    assert (root / "guide.txt").read_text() == "first\noriginal\n"


@pytest.mark.parametrize("tool", ["list_files", "search_text"])
def test_pagination_refuses_changed_source_instead_of_skipping_results(
    broker: ToolBroker, tool: str
) -> None:
    for number in range(4):
        (broker.worktree / f"match-{number}.txt").write_text("needle\n")
    args: dict[str, object] = {"limit": 2}
    if tool == "search_text":
        args["pattern"] = "needle"
    first = broker.invoke(tool, args)
    assert not first.is_error
    (broker.worktree / "aaa-new.txt").write_text("needle\n")
    second = broker.invoke(
        tool,
        {
            **args,
            "offset": first.metadata["next_offset"],
            "snapshot": first.metadata["snapshot"],
        },
    )
    assert second.is_error and "changed since the previous page" in second.content


def test_automatic_truncation_pins_default_rereads_until_explicit_refresh(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    original = "old content\n" * 100
    root = repository_factory(tmp_path, {"guide.txt": original})
    broker = SharedToolBroker(
        root, ("*",), limits=ExecutionLimits(max_tool_output_bytes=320)
    )
    first = broker.invoke("read_file", {"path": "guide.txt"})
    assert first.metadata["complete_file"] is False
    (root / "guide.txt").write_text("peer content\n")
    repeated = broker.invoke("read_file", {"path": "guide.txt"})
    assert "old content" in repeated.content and "peer content" not in repeated.content
    assert repeated.metadata["complete_file"] is False
    assert broker.invoke(
        "write_file", {"path": "guide.txt", "content": "unsafe"}
    ).is_error
    refreshed = broker.invoke("read_file", {"path": "guide.txt", "refresh": True})
    assert refreshed.content == "peer content\n"
    assert not broker.invoke(
        "write_file", {"path": "guide.txt", "content": "safe"}
    ).is_error
    assert broker.candidates()[0].base == identify_path(root / "guide.txt")


def test_refresh_of_peer_deleted_partial_read_allows_recreating_exact_absent_base(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root = repository_factory(tmp_path, {"guide.txt": "old content\n" * 100})
    broker = SharedToolBroker(
        root, ("*",), limits=ExecutionLimits(max_tool_output_bytes=320)
    )
    first = broker.invoke("read_file", {"path": "guide.txt"})
    assert first.metadata["complete_file"] is False

    (root / "guide.txt").unlink()
    refreshed = broker.invoke("read_file", {"path": "guide.txt", "refresh": True})
    assert refreshed.is_error and "is absent" in refreshed.content

    recreated = broker.invoke(
        "write_file", {"path": "guide.txt", "content": "replacement\n"}
    )
    assert not recreated.is_error
    candidate = broker.candidates()[0]
    assert candidate.relative_path == "guide.txt"
    assert candidate.base.absent
    assert candidate.content == b"replacement\n"
