"""Repository configuration cannot start unapproved programs through trusted Git."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path

import pytest

from llm_cli.agent.tools import ToolBroker
from llm_cli.errors import LlmCoordError
from llm_cli.git.environment import run_git, sanitized_git_environment
from llm_cli.git.worktrees import create_managed_worktree


@pytest.mark.parametrize("kind", ["clean", "smudge", "process"])
def test_repository_filters_are_refused_before_they_run(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    kind: str,
) -> None:
    repository = repository_factory(
        tmp_path, {".gitattributes": "*.txt filter=probe\n", "source.txt": "base\n"}
    )
    marker = tmp_path / "helper-ran"
    helper = f"touch {shlex.quote(str(marker))}; " + (
        "exit 1" if kind == "process" else "cat"
    )
    git_run(repository, "config", f"filter.probe.{kind}", helper)
    if kind == "smudge":
        arguments = ["worktree", "add", "--detach", str(tmp_path / "linked"), "HEAD"]
    else:
        (repository / "source.txt").write_text("changed\n")
        arguments = ["add", "--all"]
    with pytest.raises(LlmCoordError, match="filters"):
        run_git(repository, arguments)
    assert not marker.exists()


@pytest.mark.parametrize("kind", ["smudge", "process"])
def test_new_worktree_rechecks_conditionally_enabled_filters_before_checkout(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    kind: str,
) -> None:
    repository = repository_factory(
        tmp_path, {".gitattributes": "*.txt filter=probe\n", "source.txt": "base\n"}
    )
    marker = tmp_path / "conditional-helper-ran"
    helper = f"touch {shlex.quote(str(marker))}; " + (
        "exit 1" if kind == "process" else "cat"
    )
    conditional = tmp_path / "child-only.conf"
    conditional.write_text(f'[filter "probe"]\n\t{kind} = {helper}\n')
    git_run(
        repository,
        "config",
        f"includeIf.gitdir:{repository}/.git/worktrees/**.path",
        str(conditional),
    )
    # The original preflight in the primary checkout cannot see this filter.
    assert (
        run_git(
            repository,
            ["config", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"],
            check=False,
        ).returncode
        == 1
    )
    oid = git_run(repository, "rev-parse", "HEAD")
    original_index = git_run(repository, "write-tree")
    managed_root = tmp_path / "managed"
    with pytest.raises(LlmCoordError, match="filters"):
        create_managed_worktree(
            repository,
            managed_root=managed_root,
            task_id="conditional",
            base_oid=oid,
        )
    assert not marker.exists()
    assert not (managed_root / "conditional").exists()
    records = git_run(repository, "worktree", "list", "--porcelain")
    assert records.count("worktree ") == 1
    assert git_run(repository, "rev-parse", "HEAD") == oid
    assert git_run(repository, "write-tree") == original_index
    assert (repository / "source.txt").read_text() == "base\n"


def test_child_checkout_does_not_reenable_conditional_global_filters(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = repository_factory(
        tmp_path, {".gitattributes": "*.txt filter=probe\n", "source.txt": "base\n"}
    )
    marker = tmp_path / "global-helper-ran"
    conditional = tmp_path / "child-only.conf"
    conditional.write_text(
        f'[filter "probe"]\n\tsmudge = touch {shlex.quote(str(marker))}; cat\n'
    )
    global_config = tmp_path / "global.conf"
    global_config.write_text(
        f'[includeIf "gitdir:{repository}/.git/worktrees/**"]\n\tpath = {conditional}\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    managed = create_managed_worktree(
        repository,
        managed_root=tmp_path / "managed",
        task_id="global-conditional",
        base_oid=git_run(repository, "rev-parse", "HEAD"),
    )
    assert not marker.exists()
    assert (managed.path / "source.txt").read_text() == "base\n"


def test_child_guard_honors_worktree_specific_configuration(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
) -> None:
    repository = repository_factory(
        tmp_path, {".gitattributes": "*.txt filter=probe\n", "source.txt": "base\n"}
    )
    git_run(repository, "config", "extensions.worktreeConfig", "true")
    managed = create_managed_worktree(
        repository,
        managed_root=tmp_path / "managed",
        task_id="worktree-config",
        base_oid=git_run(repository, "rev-parse", "HEAD"),
    )
    marker = tmp_path / "worktree-helper-ran"
    git_run(
        managed.path,
        "config",
        "--worktree",
        "filter.probe.clean",
        f"touch {shlex.quote(str(marker))}; cat",
    )
    (managed.path / "source.txt").write_text("changed\n")
    with pytest.raises(LlmCoordError, match="filters"):
        run_git(managed.path, ["add", "--all"])
    assert not marker.exists()


@pytest.mark.parametrize("kind", ["external", "textconv"])
def test_model_diff_never_executes_repository_diff_helpers(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    kind: str,
) -> None:
    repository = repository_factory(
        tmp_path, {".gitattributes": "*.txt diff=probe\n", "source.txt": "base\n"}
    )
    marker = tmp_path / "diff-helper-ran"
    helper = f"touch {shlex.quote(str(marker))}; cat"
    key = "diff.external" if kind == "external" else "diff.probe.textconv"
    managed = create_managed_worktree(
        repository,
        managed_root=tmp_path / "managed",
        task_id="diff-test",
        base_oid=git_run(repository, "rev-parse", "HEAD"),
    )
    git_run(repository, "config", key, helper)
    (managed.path / "source.txt").write_text("changed\n")
    result = ToolBroker(worktree=managed.path, scopes=("*",)).invoke("read_diff", {})
    assert not marker.exists()
    assert not result.is_error, result.content
    assert "+changed" in result.content


@pytest.mark.parametrize(
    "flag,key", [("--ext-diff", "diff.external"), ("--textconv", "diff.probe.textconv")]
)
def test_diff_caller_cannot_override_disabled_helpers(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    flag: str,
    key: str,
) -> None:
    repository = repository_factory(
        tmp_path, {".gitattributes": "*.txt diff=probe\n", "source.txt": "base\n"}
    )
    marker = tmp_path / "override-helper-ran"
    helper = tmp_path / "diff-helper.sh"
    helper.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 0\n")
    helper.chmod(0o700)
    git_run(repository, "config", key, shlex.quote(str(helper)))
    (repository / "source.txt").write_text("changed\n")
    with pytest.raises(LlmCoordError, match="diff helpers"):
        run_git(repository, ["diff", flag, "HEAD"])
    assert not marker.exists()


@pytest.mark.parametrize("name", ["--ext-diff", "--textconv"])
def test_diff_helper_option_names_remain_usable_as_literal_paths(
    tmp_path: Path, repository_factory: Callable[..., Path], name: str
) -> None:
    repository = repository_factory(tmp_path, {name: "base\n"})
    (repository / name).write_text("changed\n")
    result = run_git(repository, ["diff", "HEAD", "--", name])
    assert "+changed" in result.stdout


def test_git_does_not_inherit_provider_credentials_or_loader_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "CUSTOM_SERVICE_TOKEN",
        "PYTHONPATH",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
    )
    for key in keys:
        monkeypatch.setenv(key, "private-value")
    environment = sanitized_git_environment()
    assert not set(keys) & environment.keys()
    assert "PATH" in environment


def test_filter_guard_checks_effective_command_configuration(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
) -> None:
    repository = repository_factory(
        tmp_path, {".gitattributes": "*.txt filter=probe\n", "source.txt": "base\n"}
    )
    marker = tmp_path / "inline-filter-ran"
    helper = f"touch {shlex.quote(str(marker))}; cat"
    (repository / "source.txt").write_text("changed\n")
    with pytest.raises(LlmCoordError, match="filters"):
        run_git(repository, ["-c", f"filter.probe.clean={helper}", "add", "--all"])
    assert not marker.exists()
