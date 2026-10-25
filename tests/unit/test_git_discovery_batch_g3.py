"""Fresh batched discovery preserves real Git layouts and stable target refusals."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

from llm_cli.coordination.identity import normalize_remote_identity
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git import environment
from llm_cli.git import inspect as discovery
from llm_cli.git.inspect import RepositoryInfo, inspect_repository


def _counted_inspect(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    *,
    expected_processes: int = 4,
    **kwargs: Any,
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
    assert len(commands) == expected_processes, commands
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
def test_four_process_discovery_preserves_real_checkout_layouts(
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


@pytest.mark.parametrize(
    "value,expected",
    [
        ("false", False),
        ("no", False),
        ("off", False),
        ("0", False),
        ("", False),
        ("FALSE", False),
        ("No", False),
        ("OFF", False),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("1", True),
        ("TrUe", True),
    ],
)
def test_batched_boolean_forms_keep_exact_four_process_budget(
    value: str,
    expected: bool,
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "base\n"})
    git_run(root, "config", "core.ignorecase", value)
    info, commands = _counted_inspect(monkeypatch, root)
    assert info.path_case_insensitive is expected
    config = next(command for command in commands if "--get-regexp" in command)
    assert config[-1] == r"^(core\.ignorecase|remote\.origin\.url)$"
    assert "--null" in config


@pytest.mark.parametrize("kind", ["unset", "valueless"])
def test_unset_and_valueless_ignorecase_remain_fail_closed_without_extra_git(
    kind: str,
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "base\n"})
    git_run(root, "config", "core.ignorecase", "false")
    git_run(root, "config", "--unset-all", "core.ignorecase")
    if kind == "valueless":
        with (root / ".git/config").open("a") as handle:
            handle.write("\n[core]\nignorecase\n")
    info, _ = _counted_inspect(monkeypatch, root)
    assert info.path_case_insensitive is True


@pytest.mark.parametrize("value", ["-1", "2", "+0", "  false  ", "FALSE ", "invalid"])
def test_uncommon_boolean_uses_git_typed_fallback(
    value: str,
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "base\n"})
    git_run(root, "config", "core.ignorecase", value)
    expected = discovery._path_case_insensitive(root)
    actual = discovery.run_git
    commands: list[list[str]] = []

    def counted(path: Path, arguments: list[str], **kwargs: Any) -> Any:
        commands.append(arguments)
        return actual(path, arguments, **kwargs)

    monkeypatch.setattr(discovery, "run_git", counted)
    insensitive, remote = discovery._discovery_config(root)
    assert insensitive is expected
    assert remote is None
    assert len(commands) == 2
    assert "--get-regexp" in commands[0]
    assert "--type=bool" in commands[1]


@pytest.mark.parametrize(
    "remote",
    [
        "  git@Example.invalid:Team/répo.git \n",
        "https://Example.invalid/Team/project.git",
        "",
        "git@Example.invalid:Team/repo\nname.git",
        "git@Example.invalid:Team/repo\rname.git",
        "git@Example.invalid:Team/repo\r\nname.git",
    ],
)
def test_remote_last_value_preserves_text_stripping_and_unicode(
    remote: str,
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "base\n"})
    git_run(root, "config", "core.ignorecase", "true")
    git_run(root, "config", "--add", "core.ignorecase", "false")
    git_run(root, "config", "remote.origin.url", "https://first.invalid/old.git")
    git_run(root, "config", "--add", "remote.origin.url", remote)
    expected = normalize_remote_identity(
        git_run(root, "config", "--get", "remote.origin.url")
    )
    info, _ = _counted_inspect(monkeypatch, root)
    assert info.path_case_insensitive is False
    assert info.normalized_remote == expected


def test_valueless_remote_and_subsection_case_preserve_old_query_semantics(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "base\n"})
    git_run(root, "config", "core.ignorecase", "false")
    git_run(root, "config", "remote.Origin.url", "https://case.invalid/ignored.git")
    first, _ = _counted_inspect(monkeypatch, root)
    assert first.normalized_remote is None
    with (root / ".git/config").open("a") as handle:
        handle.write('\n[remote "origin"]\nurl\n')
    second, _ = _counted_inspect(monkeypatch, root)
    assert second.normalized_remote is None


def test_each_inspection_sees_current_refs_config_includes_and_remote(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "base\n"})
    git_run(root, "config", "core.ignorecase", "true")
    git_run(root, "config", "remote.origin.url", "https://base.invalid/branch.git")
    included = tmp_path / "included config"
    included.write_text(
        '[core]\nignorecase = false\n[remote "origin"]\nurl = https://included.invalid/main.git\n'
    )
    git_run(root, "config", "includeIf.onbranch:main.path", str(included))
    first, _ = _counted_inspect(monkeypatch, root)
    assert first.path_case_insensitive is False
    assert first.normalized_remote == normalize_remote_identity(
        "https://included.invalid/main.git"
    )
    git_run(root, "checkout", "-b", "another")
    (root / "source.txt").write_text("current commit\n")
    git_run(root, "commit", "-am", "current")
    second, _ = _counted_inspect(monkeypatch, root)
    assert second.path_case_insensitive is True
    assert second.normalized_remote == normalize_remote_identity(
        "https://base.invalid/branch.git"
    )
    assert second.target_ref == "refs/heads/another"
    assert second.base_oid != first.base_oid
    git_run(root, "config", "core.ignorecase", "false")
    git_run(root, "config", "remote.origin.url", "https://changed.invalid/current.git")
    third, _ = _counted_inspect(monkeypatch, root)
    assert third.path_case_insensitive is False
    assert third.normalized_remote == normalize_remote_identity(
        "https://changed.invalid/current.git"
    )


@pytest.mark.parametrize(
    "raw",
    ["missing-final-nul", "unexpected.key\nvalue\0", "\0", "", b"unexpected-type\0"],
)
def test_malformed_batch_output_uses_old_fresh_queries_safely(
    raw: str | bytes,
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "base\n"})
    git_run(root, "config", "core.ignorecase", "false")
    git_run(root, "config", "remote.origin.url", "https://fallback.invalid/current.git")
    actual = discovery.run_git

    def malformed(path: Path, args: list[str], **kwargs: Any) -> Any:
        if "--get-regexp" in args:
            return CompletedProcess(args, 0, stdout=raw, stderr=b"")
        return actual(path, args, **kwargs)

    monkeypatch.setattr(discovery, "run_git", malformed)
    info = inspect_repository(root, profile_id="test")
    assert info.path_case_insensitive is False
    assert info.normalized_remote == normalize_remote_identity(
        "https://fallback.invalid/current.git"
    )


@pytest.mark.parametrize("value", ["  false  ", "FALSE ", "invalid"])
def test_invalid_boolean_keeps_existing_git_discovery_refusal(
    value: str,
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "base\n"})
    git_run(root, "config", "core.ignorecase", value)
    with pytest.raises(LlmCoordError) as error:
        inspect_repository(root, profile_id="test")
    assert error.value.code is ErrorCode.REPOSITORY_UNSAFE


@pytest.mark.parametrize(
    "remote",
    [
        "  git@Example.invalid:Team/naïve.git ",
        "git@Example.invalid:Team/a\nb.git",
        "git@Example.invalid:Team/a\rb.git",
        "git@Example.invalid:Team/a\r\nb.git",
    ],
)
def test_config_batch_uses_native_text_decoding_and_universal_newlines(
    remote: str,
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"source.txt": "base\n"})
    git_run(root, "config", "core.ignorecase", "false")
    git_run(root, "config", "remote.origin.url", remote)
    # Use precisely the former subprocess text path as an oracle; batching must
    # not choose a different encoding or retain CR bytes the old query folded.
    legacy = environment.run_git(
        root, ["config", "--get", "remote.origin.url"], check=False
    )
    assert isinstance(legacy.stdout, str)
    actual = discovery.run_git
    text_modes: list[object] = []

    def checked(path: Path, args: list[str], **kwargs: Any) -> Any:
        if "--get-regexp" in args:
            text_modes.append(kwargs.get("text", True))
        return actual(path, args, **kwargs)

    monkeypatch.setattr(discovery, "run_git", checked)
    folded, current = discovery._discovery_config(root)
    assert folded is False
    assert current == legacy.stdout.strip()
    assert text_modes == [True]
