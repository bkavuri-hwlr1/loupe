"""The bounded index prefilter agrees with Git's ordinary ignore semantics."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from llm_cli.errors import LlmCoordError
from llm_cli.workspace import hydration


@pytest.mark.parametrize(
    "case",
    [
        "tracked_ignored",
        "intent_to_add",
        "deleted_index_path",
        "case_sensitive",
        "ignore_case",
        "unicode_spelling",
        "file_prefix",
        "symlink_prefix",
        "gitlink_prefix",
        "actual_submodule",
    ],
)
def test_index_prefilter_matches_default_git(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    case: str,
) -> None:
    root = repository_factory(
        tmp_path,
        {".gitignore": "", "tracked.txt": "source\n", "Case.py": "pass\n"},
    )
    (root / ".gitignore").write_text("runtime/\ntracked.txt\ncase.py\n*.pending\n")
    (root / "runtime").mkdir()
    (root / "runtime/cache").write_text("ignored runtime\n")
    if case == "intent_to_add":
        (root / "new.pending").write_text("intent\n")
        git_run(root, "add", "-N", "-f", "new.pending")
    elif case == "deleted_index_path":
        (root / "tracked.txt").unlink()
    elif case in {"case_sensitive", "ignore_case"}:
        git_run(root, "config", "core.ignorecase", str(case == "ignore_case").lower())
        (root / "Case.py").rename(root / "case.py")
    elif case == "unicode_spelling":
        (root / "caf\u00e9.py").write_text("unicode source\n")
        git_run(root, "add", "caf\u00e9.py")
        (root / ".gitignore").write_text("*.py\nruntime/\n")
    elif case in {"file_prefix", "symlink_prefix"}:
        path = root / "tracked-prefix"
        if case == "symlink_prefix":
            path.symlink_to("tracked.txt")
        else:
            path.write_text("indexed file\n")
        git_run(root, "add", "tracked-prefix")
        path.unlink()
        path.mkdir()
        (path / "child").write_text("outside replacement\n")
        with (root / ".gitignore").open("a") as handle:
            handle.write("tracked-prefix/\n")
    elif case in {"gitlink_prefix", "actual_submodule"}:
        revision = git_run(root, "rev-parse", "HEAD").strip()
        git_run(root, "update-index", "--add", "--cacheinfo", f"160000,{revision},sub")
        (root / "sub").mkdir()
        (root / "sub/child").write_text("submodule file\n")
        if case == "actual_submodule":
            (root / "sub/.git").write_text("gitdir: unavailable\n")
        with (root / ".gitignore").open("a") as handle:
            handle.write("sub/\n")
    inventory = hydration._Inventory(
        root, hydration.HydrationBudgets(), time.monotonic() + 5
    )
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        inventory.walk(descriptor)
        assert inventory.catalogue is not None
        paths = tuple(inventory.catalogue.records())
        try:
            expected = hydration._ignored_paths(
                root, paths, deadline=inventory.deadline, limit=4 * 1024 * 1024
            )
        except LlmCoordError:
            with pytest.raises(LlmCoordError):
                inventory.ignored_paths()
        else:
            assert inventory.ignored_paths() == expected
    finally:
        os.close(descriptor)
        inventory.close()


def test_tracked_index_change_between_classifications_disables_copies(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {".gitignore": "", "tracked.txt": "source\n"})
    (root / ".gitignore").write_text("tracked.txt\nruntime/\n")
    (root / "runtime").mkdir()
    (root / "runtime/cache").write_text("runtime\n")
    original = hydration._Inventory.promote

    def changed(self: hydration._Inventory, descriptor: int, ignored: set[str]) -> None:
        original(self, descriptor, ignored)
        git_run(root, "rm", "--cached", "tracked.txt")

    monkeypatch.setattr(hydration._Inventory, "promote", changed)
    plan = hydration.plan_hydration(root)
    assert not plan.complete
    assert "tracked_index_changed" in plan.issues
    assert not any(entry.action == "clone_or_copy" for entry in plan.entries)


def test_tracked_object_change_without_ignore_change_is_still_detected(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {".gitignore": "", "tracked.txt": "source\n"})
    original = hydration._Inventory.promote

    def changed(self: hydration._Inventory, descriptor: int, ignored: set[str]) -> None:
        original(self, descriptor, ignored)
        (root / "tracked.txt").write_text("new staged source\n")
        git_run(root, "add", "tracked.txt")

    monkeypatch.setattr(hydration._Inventory, "promote", changed)
    plan = hydration.plan_hydration(root)
    assert not plan.complete
    assert "tracked_index_changed" in plan.issues


def test_malformed_indexed_name_absent_from_checkout_is_a_bounded_refusal(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
) -> None:
    import subprocess

    root = repository_factory(tmp_path, {"tracked.txt": "source\n"})
    blob = git_run(root, "rev-parse", "HEAD:tracked.txt").strip().encode()
    subprocess.run(
        [
            b"git", b"update-index", b"--add", b"--cacheinfo", b"100644", blob,
            b"absent-\xff",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=5,
    )
    plan = hydration.plan_hydration(root)
    assert not plan.complete
    assert "unsupported_path_encoding_budget" in plan.issues
