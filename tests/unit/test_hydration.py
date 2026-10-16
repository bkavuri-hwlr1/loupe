from __future__ import annotations

import json
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from llm_cli.cli.app import build_parser, dispatch
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.protocol.client import DaemonClient
from llm_cli.workspace.hydration import HydrationBudgets, plan_hydration


@pytest.fixture
def checkout(
    tmp_path: Path, repository_factory: Callable[[Path, dict[str, str]], Path]
) -> Path:
    return repository_factory(
        tmp_path,
        {
            ".gitignore": ".env*\n.aws/\n.venv/\nnode_modules/\nruntime/\n",
            "app.py": "pass\n",
        },
    )


def _entries(plan: Any) -> dict[str, Any]:
    return {entry.path: entry for entry in plan.entries}


def test_inventory_uses_actual_ignored_entries_and_never_reads_file_bytes(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (checkout / ".env").write_text("TOKEN=not-for-the-plan\n")
    (checkout / "runtime").mkdir()
    (checkout / "runtime/database.sqlite").write_bytes(b"\x00" * 83)
    (checkout / "notes.txt").write_text("ordinary untracked source")
    original_open = os.open

    def directories_only(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        assert flags & os.O_DIRECTORY, "the planner opened file contents"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", directories_only)
    plan = plan_hydration(checkout)

    entries = _entries(plan)
    assert plan.complete
    assert entries["runtime/database.sqlite"].action == "clone_or_copy"
    assert entries["runtime/database.sqlite"].apparent_bytes == 83
    assert entries[".env"].action == "needs_consent"
    assert entries[".env"].sensitivity == "likely_secret"
    assert "notes.txt" not in entries
    assert "app.py" not in entries
    assert "not-for-the-plan" not in str(plan.to_dict())
    assert plan.to_dict()["preview_only"] is True
    assert plan.to_dict()["cow_capability"] == "unprobed"


def test_secret_approval_is_exact_path_and_never_directory_recursive(
    checkout: Path,
) -> None:
    (checkout / ".aws").mkdir()
    (checkout / ".aws/credentials").write_text("opaque")
    (checkout / ".aws/config").write_text("opaque")
    plan = plan_hydration(checkout, approved_secret_paths=(".aws", ".aws/credentials"))
    entries = _entries(plan)
    assert entries[".aws/credentials"].action == "private_copy"
    assert entries[".aws/credentials"].destination_mode == "0600"
    assert entries[".aws/config"].action == "needs_consent"


def test_virtualenv_requires_rebuild_without_opening_absolute_shebangs(
    checkout: Path,
) -> None:
    (checkout / ".venv/bin").mkdir(parents=True)
    (checkout / ".venv/bin/python").write_text("#!/private/primary/.venv/bin/python\n")
    entries = _entries(plan_hydration(checkout))
    assert entries[".venv"].action == "needs_rebuild"
    assert "absolute" in entries[".venv"].reason
    assert entries[".venv"].descendants_inventoried is False
    assert ".venv/bin/python" not in entries


def test_ignored_node_modules_is_deferred_without_enumerating_descendants(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (checkout / "node_modules/pkg").mkdir(parents=True)
    (checkout / "node_modules/pkg/index.js").write_text("module.exports = 1")
    original = os.scandir
    dependency_inode = (checkout / "node_modules").stat().st_ino

    def no_dependency_scan(descriptor: Any) -> Any:
        if isinstance(descriptor, int):
            assert os.fstat(descriptor).st_ino != dependency_inode
        return original(descriptor)

    monkeypatch.setattr(os, "scandir", no_dependency_scan)
    plan = plan_hydration(checkout)
    entry = _entries(plan)["node_modules"]
    assert plan.complete
    assert entry.action == "needs_rebuild"
    assert entry.requires_decision
    assert entry.descendants_inventoried is False
    assert entry.directory_entries is None
    assert "unknown" in entry.reason
    assert not any(item.path.startswith("node_modules/") for item in plan.entries)


def test_large_dependency_directory_does_not_disable_independent_candidates(
    checkout: Path,
) -> None:
    dependencies = checkout / "node_modules"
    dependencies.mkdir()
    for index in range(20_001):
        (dependencies / f"entry-{index:05d}").touch()
    (checkout / "runtime").mkdir()
    (checkout / "runtime/data").write_text("small independent runtime")
    plan = plan_hydration(checkout)
    entries = _entries(plan)
    assert plan.complete
    assert not plan.issues
    assert plan.scanned_entries < 20
    assert entries["node_modules"].action == "needs_rebuild"
    assert entries["runtime/data"].action == "clone_or_copy"
    assert plan.planned_apparent_bytes == len("small independent runtime")


def test_nonignored_node_modules_is_still_inventoried(
    checkout: Path,
) -> None:
    (checkout / ".gitignore").write_text("node_modules/*\n!node_modules/source\n")
    (checkout / "node_modules").mkdir()
    (checkout / "node_modules/source").write_text("ordinary source")
    (checkout / "node_modules/cache").write_text("ignored runtime")
    plan = plan_hydration(checkout)
    assert plan.complete
    assert _entries(plan)["node_modules/cache"].action == "clone_or_copy"
    assert "node_modules/source" not in _entries(plan)


def test_deferred_dependency_ignore_rule_change_invalidates_copy_candidates(
    checkout: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import llm_cli.workspace.hydration as hydration

    (checkout / ".gitignore").write_text("runtime/\n")
    external_rules = tmp_path / "global-ignore"
    external_rules.write_text("node_modules/\n")
    config = tmp_path / "gitconfig"
    config.write_text(f"[core]\nexcludesFile = {external_rules}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    (checkout / "node_modules").mkdir()
    (checkout / "runtime").mkdir()
    (checkout / "runtime/data").touch()
    original = hydration._ignored_paths
    changed = False

    def remove_dependency_rule(*args: Any, **kwargs: Any) -> set[str]:
        nonlocal changed
        result = original(*args, **kwargs)
        if result == {"node_modules"} and not changed:
            changed = True
            external_rules.write_text("")
        return result

    monkeypatch.setattr(hydration, "_ignored_paths", remove_dependency_rule)
    plan = plan_hydration(checkout)
    assert changed
    assert not plan.complete
    assert "ignore_policy_changed" in plan.issues
    assert _entries(plan)["runtime/data"].action == "needs_rescan"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "nested_repository"])
def test_dependency_deferral_preserves_unsafe_entry_refusals(
    checkout: Path,
    tmp_path: Path,
    kind: str,
) -> None:
    path = checkout / "node_modules"
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        path.symlink_to(outside, target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
        (path / ".git").write_text("gitdir: /outside\n")
    plan = plan_hydration(checkout)
    expected = "refuse_special" if kind == "fifo" else f"refuse_{kind}"
    assert _entries(plan)["node_modules"].action == expected
    assert not any(entry.path.startswith("node_modules/") for entry in plan.entries)


def test_dependency_ignore_query_limit_keeps_descendants_explicitly_unknown(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import llm_cli.workspace.hydration as hydration

    (checkout / "node_modules").mkdir()
    original = hydration._ignored_paths

    def exhaust_query(root: Path, paths: list[str], **kwargs: Any) -> set[str]:
        if paths == ["node_modules"]:
            raise hydration._BudgetExceeded("time_budget_exceeded")
        return original(root, paths, **kwargs)

    monkeypatch.setattr(hydration, "_ignored_paths", exhaust_query)
    plan = plan_hydration(checkout)
    assert not plan.complete
    assert "time_budget_exceeded" in plan.issues
    assert _entries(plan)["node_modules"].descendants_inventoried is False
    assert not any(entry.action == "clone_or_copy" for entry in plan.entries)


def test_contained_escaping_absolute_and_parent_symlinks_are_not_followed(
    checkout: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("never-inventory-this")
    (checkout / "runtime").mkdir()
    (checkout / "runtime/plain").write_text("ordinary")
    (checkout / "runtime/contained").symlink_to("plain")
    (checkout / "runtime/escape").symlink_to("../../outside", target_is_directory=True)
    (checkout / "runtime/absolute").symlink_to(outside, target_is_directory=True)
    entries = _entries(plan_hydration(checkout))
    assert entries["runtime/contained"].action == "refuse_symlink"
    assert "contained" in entries["runtime/contained"].reason
    assert "escapes" in entries["runtime/escape"].reason
    assert "absolute" in entries["runtime/absolute"].reason
    assert all("/secret" not in path for path in entries)
    assert "never-inventory-this" not in str(entries)


def test_nested_repository_and_fifo_are_explicit_and_never_opened(
    checkout: Path,
) -> None:
    nested = checkout / "runtime/vendor"
    nested.mkdir(parents=True)
    (nested / ".git").write_text("gitdir: /somewhere/outside\n")
    (nested / "data").write_text("nested-secret")
    os.mkfifo(checkout / "runtime/channel")
    entries = _entries(plan_hydration(checkout))
    assert entries["runtime/vendor"].action == "refuse_nested_repository"
    assert entries["runtime/vendor"].descendants_inventoried is False
    assert entries["runtime/channel"].action == "refuse_special"
    assert "runtime/vendor/data" not in entries


def test_socket_is_explicit(checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (checkout / "runtime").mkdir()
    with socket.socket(socket.AF_UNIX) as server:
        monkeypatch.chdir(checkout)
        server.bind("runtime/socket")
        assert _entries(plan_hydration(checkout))["runtime/socket"].kind == "socket"


def test_budgets_choose_in_stable_path_order_and_report_refused_files(
    checkout: Path,
) -> None:
    (checkout / "runtime").mkdir()
    for name, size in (("z", 4), ("a", 4), ("large", 20)):
        (checkout / f"runtime/{name}").write_bytes(b"x" * size)
    plan = plan_hydration(
        checkout,
        budgets=HydrationBudgets(max_file_bytes=10, max_total_bytes=5),
    )
    entries = _entries(plan)
    assert plan.complete
    assert entries["runtime/a"].action == "clone_or_copy"
    assert entries["runtime/z"].action == "needs_budget"
    assert entries["runtime/large"].action == "needs_budget"
    assert plan.planned_apparent_bytes == 4
    assert plan.requires_decision


@pytest.mark.parametrize("limit", ["max_entries", "max_metadata_bytes", "max_depth"])
def test_enumeration_limits_never_report_complete_inventory(
    checkout: Path, limit: str
) -> None:
    (checkout / "runtime/deep").mkdir(parents=True)
    for index in range(12):
        (checkout / f"runtime/deep/{index:02}").touch()
    limits = {
        limit: {"max_entries": 5, "max_metadata_bytes": 256, "max_depth": 1}[limit]
    }
    plan = plan_hydration(checkout, budgets=HydrationBudgets(**limits))
    assert not plan.complete
    assert plan.requires_decision
    assert any("budget" in issue for issue in plan.issues)
    assert not any(
        entry.action in {"clone_or_copy", "private_copy"} for entry in plan.entries
    )


def test_nul_safe_names_and_tracked_ignored_source(
    checkout: Path, git_run: Callable[..., str]
) -> None:
    (checkout / "runtime").mkdir()
    (checkout / "runtime/with\nnewline").touch()
    (checkout / "runtime/-leading").touch()
    (checkout / "runtime/tracked").write_text("tracked")
    git_run(checkout, "add", "--force", "runtime/tracked")
    entries = _entries(plan_hydration(checkout))
    assert "runtime/with\nnewline" in entries
    assert "runtime/-leading" in entries
    assert "runtime/tracked" not in entries


def test_git_policy_change_during_preview_invalidates_plan(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.workspace import hydration

    (checkout / "runtime").mkdir()
    (checkout / "runtime/data").touch()
    original = hydration._ignored_paths
    calls = 0

    def changed(*args: Any, **kwargs: Any) -> set[str]:
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 1:
            (checkout / ".gitignore").write_text(".env*\n")
        return result

    monkeypatch.setattr(hydration, "_ignored_paths", changed)
    plan = plan_hydration(checkout)
    assert not plan.complete
    assert "ignore_policy_changed" in plan.issues
    assert not any(entry.action == "clone_or_copy" for entry in plan.entries)


def test_directory_replaced_by_symlink_is_never_descended(
    checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (checkout / "runtime/target").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "not-in-checkout").touch()
    original = os.open
    changed = False

    def replace_directory(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal changed
        if path == "target" and not changed:
            changed = True
            (checkout / "runtime/target").rmdir()
            (checkout / "runtime/target").symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_directory)
    plan = plan_hydration(checkout)
    assert not plan.complete
    assert all("not-in-checkout" not in entry.path for entry in plan.entries)
    assert any("changed" in issue for issue in plan.issues)


def test_preview_cli_never_contacts_daemon_or_creates_profile(
    checkout: Path,
    tmp_path: Path,
) -> None:
    class NoDaemon:
        def call(self, *args: Any, **kwargs: Any) -> Any:
            pytest.fail("the preview contacted the daemon")

    (checkout / ".env").write_text("opaque")
    arguments = build_parser().parse_args(
        [
            "--json",
            "workspace",
            "hydrate",
            str(checkout),
            "--plan",
            "--approve-secret",
            ".env",
        ]
    )
    before = sorted(tmp_path.iterdir())
    result = dispatch(arguments, cast(DaemonClient, NoDaemon()))
    assert result["preview_only"]
    assert (
        next(entry for entry in result["entries"] if entry["path"] == ".env")["action"]
        == "private_copy"
    )
    assert sorted(tmp_path.iterdir()) == before


def test_cli_requires_explicit_plan_flag() -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["workspace", "hydrate"])
    assert caught.value.code == 2


@pytest.mark.parametrize("value", ["../.env", "/.env", "runtime/../.env", ".", ""])
def test_secret_consent_cannot_escape_or_authorize_a_tree(
    checkout: Path, value: str
) -> None:
    with pytest.raises(LlmCoordError) as caught:
        plan_hydration(checkout, approved_secret_paths=(value,))
    assert caught.value.code is ErrorCode.CONFIG_INVALID


def test_non_repository_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(LlmCoordError) as caught:
        plan_hydration(tmp_path)
    assert caught.value.code is ErrorCode.REPOSITORY_NOT_FOUND


def test_global_ignore_and_nested_negation_are_honored(
    checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ignore = tmp_path / "global-ignore"
    ignore.write_text("global-cache\n")
    config = tmp_path / "gitconfig"
    config.write_text(f"[core]\nexcludesFile = {ignore}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    (checkout / "global-cache").write_text("runtime")
    (checkout / "runtime").mkdir()
    (checkout / ".gitignore").write_text("runtime/*\n!runtime/keep\n")
    (checkout / "runtime/keep").touch()
    (checkout / "runtime/cache").touch()
    entries = _entries(plan_hydration(checkout))
    assert entries["global-cache"].action == "clone_or_copy"
    assert entries["runtime/cache"].action == "clone_or_copy"
    assert "runtime/keep" not in entries


def test_case_aliases_invalidate_all_copy_actions(checkout: Path) -> None:
    (checkout / "runtime").mkdir()
    (checkout / "runtime/A").touch()
    (checkout / "runtime/a").touch()
    if len(list((checkout / "runtime").iterdir())) != 2:
        pytest.skip("requires a case-sensitive filesystem")
    plan = plan_hydration(checkout)
    assert not plan.complete
    assert "path_alias_requires_decision" in plan.issues
    assert _entries(plan)["runtime/A"].action == "refuse_path_alias"


def test_budgeted_git_output_is_killed_and_not_returned(
    checkout: Path, git_run: Callable[..., str]
) -> None:
    import time

    from llm_cli.workspace.hydration import _bounded_git, _BudgetExceeded

    git_run(checkout, "config", "test.large", "x" * 20_000)
    with pytest.raises(_BudgetExceeded, match="git_metadata_budget_exceeded"):
        _bounded_git(
            checkout,
            ["config", "--get", "test.large"],
            deadline=time.monotonic() + 1,
            limit=128,
        )


def test_expired_time_budget_returns_an_incomplete_preview(checkout: Path) -> None:
    plan = plan_hydration(checkout, budgets=HydrationBudgets(max_seconds=0.000001))
    assert not plan.complete
    assert "time_budget_exceeded" in plan.issues
    assert not plan.entries


def test_byte_changes_during_stat_observation_invalidate_preview(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (checkout / "runtime").mkdir()
    (checkout / "runtime/data").write_text("before")
    original = os.stat
    changed = False

    def mutate(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal changed
        result = original(path, *args, **kwargs)
        if path == "data" and not changed:
            changed = True
            (checkout / "runtime/data").write_text("different bytes after observation")
        return result

    monkeypatch.setattr(os, "stat", mutate)
    plan = plan_hydration(checkout)
    assert not plan.complete
    assert _entries(plan)["runtime/data"].action == "needs_rescan"
    assert "entry_changed_during_inventory" in plan.issues


def test_administration_and_example_configuration_are_explicit(checkout: Path) -> None:
    (checkout / ".env.example").write_text("TOKEN=placeholder")
    (checkout / ".codex/private").mkdir(parents=True)
    (checkout / ".codex/private/state").write_text("opaque coordinator state")
    entries = _entries(plan_hydration(checkout))
    assert entries[".git"].action == "exclude_administration"
    assert entries[".codex"].action == "exclude_administration"
    assert ".codex/private/state" not in entries
    assert entries[".env.example"].sensitivity == "unclassified"
    assert entries[".env.example"].action == "clone_or_copy"


def test_cli_json_entrypoint_does_not_create_a_profile(
    checkout: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from llm_cli.cli.app import main

    for name in ("CONFIG_HOME", "DATA_HOME", "STATE_HOME", "RUNTIME_DIR"):
        monkeypatch.setenv(f"LLM_COORD_{name}", str(tmp_path / f"unused-{name}"))
    before = sorted(tmp_path.iterdir())
    main(
        [
            "--profile",
            "preview-only",
            "--json",
            "workspace",
            "hydrate",
            str(checkout),
            "--plan",
        ]
    )
    assert json.loads(capsys.readouterr().out)["preview_only"]
    assert sorted(tmp_path.iterdir()) == before


@pytest.mark.parametrize("plain", [False, True])
def test_large_hydration_cli_is_bounded_and_json_keeps_full_details(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
    plain: bool,
) -> None:
    from llm_cli.cli.app import main

    (checkout / "runtime").mkdir()
    for index in range(600):
        (checkout / "runtime" / f"cache-{index:04d}.dat").write_bytes(b"x")
    (checkout / ".env").write_text("private-value-never-in-output")
    (checkout / "node_modules").mkdir()
    expected = json.loads(json.dumps(plan_hydration(checkout).to_dict()))
    command = ["workspace", "hydrate", str(checkout), "--plan"]
    assert main([*(["--plain"] if plain else []), *command]) is None
    human = capsys.readouterr().out
    assert len(human.encode()) < 8_000
    assert max(map(len, human.splitlines())) < 500
    assert "600 Copy candidates" in human
    assert "1 Consent needed" in human
    assert "1 Rebuild needed" in human
    assert "clone_or_copy" not in human
    assert "needs_consent" not in human
    assert ".env" in human
    assert "node_modules" in human
    assert "unknown" in human
    assert "omitted" in human
    assert "--json" in human
    assert "materialization" not in human
    assert "private-value-never-in-output" not in human
    assert "mtime_ns" not in human
    assert main(["--json", *command]) is None
    structured = capsys.readouterr().out
    assert len(structured.encode()) > 350_000
    assert json.loads(structured) == expected
    assert len(json.loads(structured)["entries"]) == 604


def test_incomplete_hydration_cli_explains_limits_and_decisions(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from llm_cli.cli.app import main

    (checkout / "runtime").mkdir()
    for index in range(20):
        (checkout / "runtime" / str(index)).touch()
    assert (
        main(
            [
                "workspace",
                "hydrate",
                str(checkout),
                "--plan",
                "--max-entries",
                "10",
            ]
        )
        is None
    )
    human = capsys.readouterr().out
    assert "incomplete" in human
    assert "entry_budget_exceeded" in human
    assert "no copy candidates" in human
    assert "--json" in human
    assert len(human.encode()) < 8_000


def test_hydration_human_samples_escape_path_newlines(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from llm_cli.cli.app import main

    (checkout / "runtime").mkdir()
    (checkout / "runtime" / "name\nforged heading\x1b[31m").touch()
    assert main(["workspace", "hydrate", str(checkout), "--plan"]) is None
    human = capsys.readouterr().out
    assert "\\nforged heading" in human
    assert "\nforged heading" not in human
    assert "\x1b" not in human


def test_copy_allocation_budget_counts_source_blocks_conservatively(
    checkout: Path,
) -> None:
    (checkout / "runtime").mkdir()
    path = checkout / "runtime/data"
    path.write_bytes(b"x")
    allocated = path.stat().st_blocks * 512
    if allocated <= 1:
        pytest.skip("filesystem has no block allocation metadata")
    plan = plan_hydration(
        checkout, budgets=HydrationBudgets(max_allocated_bytes=allocated - 1)
    )
    assert _entries(plan)["runtime/data"].action == "needs_budget"
    assert plan.planned_estimated_copy_bytes == 0
