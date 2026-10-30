"""Large source inventories keep the original runtime and metadata bounds."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from llm_cli.workspace.hydration import HydrationBudgets, plan_hydration


def source_checkout(
    tmp_path: Path, repository_factory: Callable[..., Path], count: int = 1000
) -> Path:
    return repository_factory(
        tmp_path,
        {
            ".gitignore": "runtime/\n",
            **{
                f"source/{number // 100:03}/{number:06}.py": "pass\n"
                for number in range(count)
            },
        },
    )


def test_ordinary_source_metadata_is_not_retained_like_runtime(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root = source_checkout(tmp_path, repository_factory)
    (root / "runtime").mkdir()
    (root / "runtime/cache").write_bytes(b"private runtime input")
    budget = HydrationBudgets(max_entries=2000, max_metadata_bytes=128 * 1024)
    plan = plan_hydration(root, budgets=budget)
    assert plan.complete, plan.issues
    assert plan.scanned_entries > 1000
    assert plan.inventory_metadata_bytes <= budget.max_metadata_bytes
    assert (
        next(entry for entry in plan.entries if entry.path == "runtime/cache").action
        == "clone_or_copy"
    )
    assert not any(entry.path.startswith("source/") for entry in plan.entries)


def test_source_still_counts_toward_the_inventory_entry_bound(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root = source_checkout(tmp_path, repository_factory)
    plan = plan_hydration(root, budgets=HydrationBudgets(max_entries=500))
    assert not plan.complete
    assert "entry_budget_exceeded" in plan.issues
    assert not any(
        entry.action in {"clone_or_copy", "private_copy", "private_tree"}
        for entry in plan.entries
    )


def test_runtime_metadata_still_uses_the_same_byte_budget(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root = source_checkout(tmp_path, repository_factory, count=1)
    for number in range(1000):
        path = root / f"runtime/{number // 100:03}/{number:06}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ignored private input")
    plan = plan_hydration(
        root, budgets=HydrationBudgets(max_entries=2000, max_metadata_bytes=128 * 1024)
    )
    assert not plan.complete
    assert "metadata_budget_exceeded" in plan.issues
    assert not any(
        entry.action in {"clone_or_copy", "private_copy", "private_tree"}
        for entry in plan.entries
    )


def test_ordinary_source_aliases_still_disable_all_runtime_copies(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root = source_checkout(tmp_path, repository_factory, count=1)
    source = root / "source/000"
    (source / "Alias.py").write_text("one\n")
    (source / "alias.py").write_text("two\n")
    if len(list(source.glob("*lias.py"))) != 2:
        pytest.skip("requires a case-sensitive filesystem")
    (root / "runtime").mkdir()
    (root / "runtime/cache").write_bytes(b"private runtime input")
    plan = plan_hydration(root)
    assert not plan.complete
    assert "path_alias_requires_decision" in plan.issues
    assert (
        next(entry for entry in plan.entries if entry.path == "runtime/cache").action
        == "needs_rescan"
    )


def test_catalogue_allocation_limit_is_fail_closed_and_removed(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.workspace import hydration_catalogue

    root = source_checkout(tmp_path, repository_factory, count=1)
    original = hydration_catalogue.tempfile.mkdtemp
    directories: list[Path] = []

    def made(*args: object, **kwargs: object) -> str:
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        directories.append(Path(result))
        assert not Path(result).stat().st_mode & 0o077
        return result

    monkeypatch.setattr(hydration_catalogue.tempfile, "mkdtemp", made)
    plan = plan_hydration(root, budgets=HydrationBudgets(max_metadata_bytes=4096))
    assert not plan.complete
    assert "metadata_budget_exceeded" in plan.issues
    assert directories
    assert all(not path.exists() for path in directories)


def test_catalogue_is_removed_if_inventory_is_interrupted(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.workspace.hydration_catalogue import PathCatalogue

    root = source_checkout(tmp_path, repository_factory, count=1)
    original = PathCatalogue.add
    directories: list[Path] = []

    def interrupted(self: PathCatalogue, *args: object, **kwargs: object) -> str | None:
        directories.append(self.directory)
        assert not self.path.stat().st_mode & 0o077
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("interrupted fixture inventory")

    monkeypatch.setattr(PathCatalogue, "add", interrupted)
    with pytest.raises(RuntimeError, match="interrupted fixture inventory"):
        plan_hydration(root)
    assert directories
    assert all(not path.exists() for path in directories)
