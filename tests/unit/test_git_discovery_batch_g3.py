"""Fresh batched discovery preserves real Git layouts and stable target refusals."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git import environment
from llm_cli.git.inspect import RepositoryInfo, inspect_repository


def _counted_inspect(
    monkeypatch: pytest.MonkeyPatch, path: Path, **kwargs: Any
) -> tuple[RepositoryInfo, list[tuple[str, ...]]]:
    actual_run = environment.subprocess.run
    commands: list[tuple[str, ...]] = []

    def run(argv: Any, *args: Any, **options: Any) -> Any:
        commands.append(tuple(argv))
        return actual_run(argv, *args, **options)

    with monkeypatch.context() as patch:
        patch.setattr(environment.subprocess, "run", run)
        info = inspect_repository(path, profile_id="test", **kwargs)
    # Count actual processes, including the filter preflight hidden by run_git.
    assert len(commands) == 6, commands
    assert all(command[0] == "git" for command in commands)
    assert all(
        flag in commands[0]
        for flag in ("--show-toplevel", "--git-common-dir", "--show-object-format")
    )
    assert sum("--get-regexp" in command for command in commands) == 1
    return info, commands


def _assert_identity(
    info: RepositoryInfo,
    *,
    root: Path,
    main: Path,
    common: Path,
    target: str,
    oid: str,
) -> None:
    assert info.worktree == root.resolve()
    assert info.main_worktree == main.resolve()
    assert info.common_git_dir == common.resolve()
    assert info.display_name == root.name
    assert info.target_ref == target
    assert info.base_oid == oid
    assert info.object_format == "sha1"
    assert info.path_case_insensitive is False
    assert info.integration_adapter == "local_git"
    assert info.normalized_remote is None
    assert len(info.repo_key) == 64


@pytest.mark.parametrize("kind", ["ordinary", "linked", "separate", "submodule"])
def test_six_process_discovery_preserves_real_checkout_layouts(
    kind: str,
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = repository_factory(
        tmp_path / "source with spaces", {"docs/source.txt": "base\n"}
    )
    root, main, target = source, source, "refs/heads/main"
    if kind == "linked":
        root = tmp_path / "linked checkout"
        git_run(source, "worktree", "add", "-b", "linked-branch", str(root), "main")
        target = "refs/heads/linked-branch"
    elif kind == "separate":
        root = tmp_path / "separate checkout"
        git_run(
            source,
            "clone",
            "--separate-git-dir=" + str(tmp_path / "standalone admin.git"),
            str(source),
            str(root),
        )
        main = root
    elif kind == "submodule":
        parent = repository_factory(tmp_path / "superproject", {"parent.txt": "base\n"})
        git_run(
            parent,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(source),
            "sub",
        )
        root = main = parent / "sub"
    git_run(root, "config", "core.ignorecase", "false")
    common = Path(
        git_run(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    )
    oid = git_run(root, "rev-parse", target)
    # Gitfile repositories may report their administration directory here;
    # retain Git's first worktree result, as the pre-batch inspector did.
    first_worktree = git_run(root, "worktree", "list", "--porcelain").splitlines()[0]
    assert first_worktree.startswith("worktree ")
    main = Path(first_worktree.removeprefix("worktree "))
    info, _ = _counted_inspect(monkeypatch, root / "docs")
    _assert_identity(info, root=root, main=main, common=common, target=target, oid=oid)


@pytest.mark.parametrize("target", ["main", "refs/heads/main", "HEAD", ""])
def test_explicit_current_ref_or_empty_default_preserves_identity_from_subdirectory(
    target: str,
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"docs/source.txt": "base\n"})
    git_run(root, "config", "core.ignorecase", "false")
    oid = git_run(root, "rev-parse", "HEAD")
    info, _ = _counted_inspect(monkeypatch, root / "docs", target=target)
    _assert_identity(
        info,
        root=root,
        main=root,
        common=root / ".git",
        target="refs/heads/main",
        oid=oid,
    )


@pytest.mark.parametrize("head", ["another-branch", "detached"])
def test_explicit_ref_uses_its_own_commit_independently_of_current_head(
    head: str,
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "base\n"})
    git_run(root, "checkout", "-b", "selected")
    (root / "source.txt").write_text("selected commit\n")
    git_run(root, "commit", "-am", "selected")
    selected = git_run(root, "rev-parse", "HEAD")
    git_run(
        root,
        "checkout",
        *(["main"] if head == "another-branch" else ["--detach", "main"]),
    )
    assert git_run(root, "rev-parse", "HEAD") != selected
    git_run(root, "config", "core.ignorecase", "false")
    info, _ = _counted_inspect(monkeypatch, root, target="selected")
    _assert_identity(
        info,
        root=root,
        main=root,
        common=root / ".git",
        target="refs/heads/selected",
        oid=selected,
    )


@pytest.mark.parametrize("target", [None, ""])
def test_detached_head_without_a_named_target_retains_its_stable_refusal(
    target: str | None,
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "base\n"})
    git_run(root, "checkout", "--detach", "HEAD")
    with pytest.raises(LlmCoordError) as refused:
        inspect_repository(root, profile_id="test", target=target)
    assert refused.value.code is ErrorCode.REPOSITORY_UNSAFE
    assert refused.value.message == (
        "a target ref is required when the repository HEAD is detached"
    )


@pytest.mark.parametrize("kind", ["raw-oid", "ambiguous-short-ref", "detached-HEAD"])
def test_noncanonical_ref_result_retains_the_named_reference_refusal(
    kind: str,
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "base\n"})
    target = git_run(root, "rev-parse", "HEAD")
    if kind == "ambiguous-short-ref":
        git_run(root, "branch", "collision")
        git_run(root, "tag", "collision")
        target = "collision"
    elif kind == "detached-HEAD":
        git_run(root, "checkout", "--detach", "HEAD")
        target = "HEAD"
    with pytest.raises(LlmCoordError) as refused:
        inspect_repository(root, profile_id="test", target=target)
    assert refused.value.code is ErrorCode.REPOSITORY_UNSAFE
    assert refused.value.message == "target must resolve to a named Git reference"


@pytest.mark.parametrize("target", [None, "main"])
def test_unborn_head_keeps_a_stable_unsafe_repository_error(
    target: str | None, tmp_path: Path, git_run: Callable[..., str]
) -> None:
    root = tmp_path / "unborn"
    root.mkdir()
    git_run(root, "init", "-b", "main")
    with pytest.raises(LlmCoordError) as refused:
        inspect_repository(root, profile_id="test", target=target)
    assert refused.value.code is ErrorCode.REPOSITORY_UNSAFE
    assert git_run(root, "symbolic-ref", "--short", "HEAD") == "main"
    assert git_run(root, "status", "--porcelain") == ""


def test_newlines_in_root_and_common_directory_keep_unambiguous_identity(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
) -> None:
    root = repository_factory(
        tmp_path / "parent\nwith newline", {"source.txt": "base\n"}
    )
    git_run(root, "config", "core.ignorecase", "false")
    oid = git_run(root, "rev-parse", "HEAD")
    info = inspect_repository(root, profile_id="test")
    _assert_identity(
        info,
        root=root,
        main=root,
        common=root / ".git",
        target="refs/heads/main",
        oid=oid,
    )
