"""Build the Git tree of an explicit source export, without moving a ref."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.git.environment import run_git
from llm_cli.git.worktrees import resolve_exact_commit
from llm_cli.workspace.batches import BatchFile
from llm_cli.workspace.exports import check_export_privacy
from llm_cli.workspace.identity import DIRECTORY_MODE, ObjectKind
from llm_cli.workspace.patches import export_manifest


def _blob_oid(body: bytes, parent: str) -> str:
    algorithm = "sha1" if len(parent) == 40 else "sha256"
    digest = hashlib.new(algorithm, f"blob {len(body)}\0".encode())
    digest.update(body)
    return digest.hexdigest()


def _tree_entries(
    repository: Path, parent: str, changed: set[str]
) -> dict[str, tuple[str, str]]:
    names = {parent.as_posix() for path in changed for parent in Path(path).parents}
    names.discard(".")
    names.update(changed)
    # ls-tree with -t -r would also return every descendant of a selected
    # directory. Query same-depth components together instead, so each
    # response is bounded by the number of requested names.
    levels: dict[int, list[str]] = {}
    for name in sorted(names):
        levels.setdefault(len(Path(name).parts), []).append(":(literal)" + name)
    entries: dict[str, tuple[str, str]] = {}
    for level in levels.values():
        # Normalized paths can be up to 1000 characters. Bound argv on macOS.
        for start in range(0, len(level), 32):
            result = run_git(
                repository,
                [
                    "ls-tree",
                    "--full-tree",
                    "-z",
                    parent,
                    "--",
                    *level[start : start + 32],
                ],
                text=False,
            )
            assert isinstance(result.stdout, bytes)
            for record in result.stdout.split(b"\0"):
                if record:
                    header, path_bytes = record.split(b"\t", 1)
                    mode, _, oid = header.decode("ascii").split()
                    entries[os.fsdecode(path_bytes)] = (mode, oid)
    return entries


def _check_base(repository: Path, parent: str, files: tuple[BatchFile, ...]) -> None:
    changed = {item.relative_path for item in files}
    entries = _tree_entries(repository, parent, changed)
    mismatches = []
    for item in files:
        path = item.relative_path
        if item.base.kind == ObjectKind.DIRECTORY_MARKER or item.mode == DIRECTORY_MODE:
            raise LlmCoordError(
                ErrorCode.CONFIG_INVALID,
                "Git cannot export an explicit directory marker",
                {"path": path},
            )
        expected = None
        if not item.base.absent:
            if item.original is None:
                raise LlmCoordError(
                    ErrorCode.CONFIG_INVALID,
                    "exact before bytes are unavailable for commit export",
                    {"path": path},
                )
            expected = (item.base.mode, _blob_oid(item.original, parent))
        if entries.get(path) != expected:
            mismatches.append(path)
            continue
        # Adding a child through a file (or gitlink) would silently replace
        # that parent. The same ambiguity can appear within the change itself.
        for ancestor in Path(path).parents:
            name = ancestor.as_posix()
            if name in changed or (name in entries and entries[name][0] != "040000"):
                mismatches.append(path)
                break
    if mismatches:
        raise LlmCoordError(
            ErrorCode.TARGET_MOVED,
            "export base differs from the parent; select a matching --onto "
            "commit or export a patch",
            {"paths": sorted(set(mismatches))},
        )


def _check_attributes(repository: Path, paths: list[str], index: Path) -> None:
    if not paths:
        return
    # Check the live attributes and the private index. On --onto, these may
    # differ. Refuse conversions without running any repository filter driver.
    attributes = ["filter", "text", "crlf", "eol", "working-tree-encoding", "ident"]
    for options in ([], ["--cached"]):
        result = run_git(
            repository,
            ["check-attr", *options, "--stdin", "-z", *attributes],
            private_index=index,
            input_data=b"\0".join(map(os.fsencode, paths)) + b"\0",
            text=False,
        )
        assert isinstance(result.stdout, bytes)
        fields = result.stdout.removesuffix(b"\0").split(b"\0")
        for path, _, value in zip(fields[::3], fields[1::3], fields[2::3], strict=True):
            if value not in (b"unspecified", b"unset"):
                raise LlmCoordError(
                    ErrorCode.REPOSITORY_UNSAFE,
                    "commit export requires paths without Git content conversions",
                    {"path": os.fsdecode(path)},
                )
    autocrlf = run_git(repository, ["config", "--get", "core.autocrlf"], check=False)
    if autocrlf.returncode != 1 and (
        autocrlf.returncode != 0 or str(autocrlf.stdout).strip().lower() != "false"
    ):
        raise LlmCoordError(
            ErrorCode.REPOSITORY_UNSAFE,
            "commit export requires paths without automatic line-ending conversion",
        )


def build_export_tree(
    repository: Path,
    parent_oid: str,
    files: tuple[BatchFile, ...],
    *,
    source_root: Path,
    runtime_paths: tuple[str, ...],
) -> str:
    """Return an unreachable tree for a previously frozen, screened change.

    The caller supplies the exact parent recorded in the export intent.
    This operation can be replayed without creating a commit or reference.
    """
    if not source_root.is_dir():
        raise LlmCoordError(ErrorCode.REPOSITORY_UNSAFE, "export source is unavailable")
    export_manifest(files)
    check_export_privacy(source_root, files, runtime_paths)
    parent = resolve_exact_commit(repository, parent_oid)
    _check_base(repository, parent, files)
    regular = [
        item.relative_path
        for item in files
        if item.base.kind == ObjectKind.REGULAR or item.mode in ("100644", "100755")
    ]
    with TemporaryDirectory(prefix="loupe-export-index-") as directory:
        index = Path(directory) / "index"
        run_git(repository, ["read-tree", parent], private_index=index)
        _check_attributes(repository, regular, index)
        updates = bytearray()
        for item in files:
            mode, oid = "0", "0" * len(parent)
            if item.content is not None:
                result = run_git(
                    repository,
                    ["hash-object", "--no-filters", "-w", "--stdin"],
                    text=False,
                    input_data=item.content,
                )
                assert isinstance(result.stdout, bytes)
                mode, oid = item.mode, result.stdout.decode("ascii").strip()
            updates.extend(
                f"{mode} {oid}\t".encode() + os.fsencode(item.relative_path) + b"\0"
            )
        run_git(
            repository,
            ["-c", "core.ignoreCase=false", "update-index", "-z", "--index-info"],
            private_index=index,
            text=False,
            input_data=bytes(updates),
        )
        if any(Path(item.relative_path).name == ".gitattributes" for item in files):
            _check_attributes(repository, regular, index)
        tree = run_git(repository, ["write-tree"], private_index=index)
        assert isinstance(tree.stdout, str)
        return tree.stdout.strip()
