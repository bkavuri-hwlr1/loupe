"""Exact-base commit export plumbing against real Git indexes and trees."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.workspace.batches import BatchFile
from llm_cli.workspace.commit_exports import build_export_tree
from llm_cli.workspace.identity import ABSENT, REGULAR_MODE, SYMLINK_MODE, identify_path
from llm_cli.workspace.patches import portable_patch


def test_export_tree_matches_patch_and_preserves_dirty_checkout(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
) -> None:
    root = repository_factory(
        tmp_path,
        {"edit": "base\n", "delete": "bye\n", "exec": "run\n", "untouched": "old\n"},
    )
    (root / "link").symlink_to("before")
    git_run(root, "add", "link")
    git_run(root, "commit", "-m", "symlink")
    parent = git_run(root, "rev-parse", "HEAD")
    files = (
        BatchFile(
            "edit", identify_path(root / "edit"), b"after\n", REGULAR_MODE, b"base\n"
        ),
        BatchFile("delete", identify_path(root / "delete"), None, "", b"bye\n"),
        BatchFile("exec", identify_path(root / "exec"), b"run\n", "100755", b"run\n"),
        BatchFile("nested/binary", ABSENT, b"a\0\xff\0", REGULAR_MODE, binary=True),
        BatchFile(
            "link", identify_path(root / "link"), b"after", SYMLINK_MODE, b"before"
        ),
    )
    # Checkout and index both disagree with the parent, in different ways.
    (root / "edit").write_text("actual working copy\n")
    (root / "untouched").write_text("already staged\n")
    git_run(root, "add", "untouched")
    saved_index = (root / ".git/index").read_bytes()
    saved_refs = git_run(root, "show-ref")
    tree = build_export_tree(root, parent, files, source_root=root, runtime_paths=())
    assert (
        build_export_tree(root, parent, files, source_root=root, runtime_paths=())
        == tree
    )
    assert (root / ".git/index").read_bytes() == saved_index
    assert git_run(root, "show-ref") == saved_refs
    assert (root / "edit").read_text() == "actual working copy\n"
    assert (root / "delete").read_text() == "bye\n"
    assert os.readlink(root / "link") == "before"
    assert not (root / "nested").exists()
    # Independent oracle: apply C22's public binary patch in a fresh clone.
    oracle = tmp_path / "oracle"
    git_run(tmp_path, "clone", "--no-local", str(root), str(oracle))
    run_git(
        oracle,
        ["apply", "--index", "--binary", "-"],
        input_data=portable_patch(files),
        text=False,
    )
    assert git_run(oracle, "write-tree") == tree


def test_stale_base_reports_paths_and_allows_matching_parent(
    tmp_path: Path, repository_factory: Callable[..., Path], git_run: Callable[..., str]
) -> None:
    repo = repository_factory(
        tmp_path, {"changed": "before", "exe": "same", "sibling": "keep"}
    )
    first = git_run(repo, "rev-parse", "HEAD")
    files = (
        BatchFile(
            "changed", identify_path(repo / "changed"), b"new", REGULAR_MODE, b"before"
        ),
        BatchFile("exe", identify_path(repo / "exe"), None, "", b"same"),
        BatchFile("added", ABSENT, b"after", REGULAR_MODE),
    )
    (repo / "changed").write_text("drift")
    (repo / "exe").chmod(0o755)
    (repo / "added").write_text("collision")
    git_run(repo, "add", "-A")
    git_run(repo, "commit", "-m", "different base")
    head = git_run(repo, "rev-parse", "HEAD")
    with pytest.raises(LlmCoordError, match="--onto") as error:
        build_export_tree(repo, head, files, source_root=repo, runtime_paths=())
    assert error.value.code == ErrorCode.TARGET_MOVED
    assert error.value.details == {"paths": ["added", "changed", "exe"]}
    tree = build_export_tree(repo, first, files, source_root=repo, runtime_paths=())
    assert git_run(repo, "show", f"{tree}:changed") == "new"
    assert git_run(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("path", ["file/child", "folder"])
def test_refuses_tree_collisions(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    path: str,
) -> None:
    repo = repository_factory(tmp_path, {"file": "keep", "folder/file": "keep"})
    with pytest.raises(LlmCoordError, match="--onto"):
        build_export_tree(
            repo,
            git_run(repo, "rev-parse", "HEAD"),
            (BatchFile(path, ABSENT, b"body", REGULAR_MODE),),
            source_root=repo,
            runtime_paths=(),
        )


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("private/runtime", b"opaque data"),
        ("candidate", b"-----BEGIN PRIVATE KEY-----"),
    ],
)
def test_refuses_private_content_before_writing_objects(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    name: str,
    body: bytes,
) -> None:
    repo = repository_factory(tmp_path, {"tracked": "keep"})
    oid = run_git(repo, ["hash-object", "--stdin"], text=False, input_data=body).stdout
    assert isinstance(oid, bytes)
    with pytest.raises(LlmCoordError) as error:
        build_export_tree(
            repo,
            git_run(repo, "rev-parse", "HEAD"),
            (BatchFile(name, ABSENT, body, REGULAR_MODE),),
            source_root=repo,
            runtime_paths=("private",),
        )
    assert error.value.details == {"path": name}
    assert body.decode() not in str(error.value)
    assert (
        run_git(repo, ["cat-file", "-e", oid.decode().strip()], check=False).returncode
        != 0
    )


@pytest.mark.parametrize("source", ["parent", "live", "result"])
def test_rejects_git_content_conversions(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    source: str,
) -> None:
    attrs = "changed text eol=crlf\n"
    repo = repository_factory(
        tmp_path,
        {"tracked": "keep", ".gitattributes": attrs if source == "parent" else ""},
    )
    files = (BatchFile("changed", ABSENT, b"line\r\n", REGULAR_MODE),)
    if source == "parent":
        (repo / ".gitattributes").write_text("")
    elif source == "live":
        (repo / ".gitattributes").write_text(attrs)
    else:
        files += (
            BatchFile(
                ".gitattributes",
                identify_path(repo / ".gitattributes"),
                attrs.encode(),
                REGULAR_MODE,
                b"",
            ),
        )
    with pytest.raises(LlmCoordError, match="conversions") as error:
        build_export_tree(
            repo,
            git_run(repo, "rev-parse", "HEAD"),
            files,
            source_root=repo,
            runtime_paths=(),
        )
    assert error.value.details == {"path": "changed"}
