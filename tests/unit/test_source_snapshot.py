"""Source snapshots pin parents without crossing links or nested Git boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from llm_cli.execution.checks import source_snapshot
from llm_cli.workspace import isolated
from llm_cli.workspace.identity import read_identified_path


def test_source_snapshot_retains_exact_modes_links_binary_and_missing_files(
    tmp_path: Path, repository_factory: Any
) -> None:
    root = repository_factory(tmp_path, {"dir/file": "old", "removed": "gone"})
    (root / "removed").unlink()
    (root / "dir/file").write_bytes(b"binary\0data")
    (root / "dir/file").chmod(0o755)
    (root / "link").symlink_to("dir/file")
    modes: dict[str, str] = {}
    first, bodies = source_snapshot(root, contents=True, _modes=modes)
    assert bodies == {"dir/file": b"binary\0data", "link": b"dir/file", "removed": None}
    assert modes == {name: read_identified_path(root / name)[0].mode for name in bodies}
    assert source_snapshot(root)[0] == first


@pytest.mark.parametrize("mutation", ["link", "replacement", "nested-git"])
def test_source_snapshot_rejects_parent_replacement_during_read(
    tmp_path: Path,
    repository_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    root = repository_factory(tmp_path, {"src/file": "safe"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("outside private data")
    original = isolated._read
    changed = False

    def replace_parent(descriptor: int, limit: int) -> bytes:
        nonlocal changed
        body = original(descriptor, limit)
        if not changed:
            changed = True
            if mutation == "nested-git":
                (root / "src/.git").write_text("gitdir: outside")
            else:
                (root / "src").rename(root / "old-source")
                if mutation == "link":
                    (root / "src").symlink_to(outside, target_is_directory=True)
                else:
                    (root / "src").mkdir()
                    (root / "src/file").write_text("replacement")
        return body

    monkeypatch.setattr(isolated, "_read", replace_parent)
    with pytest.raises(ValueError, match=r"parent|nested repository"):
        source_snapshot(root, contents=True)
    assert (outside / "file").read_text() == "outside private data"


def test_source_snapshot_bounds_open_descriptors_across_many_parents(
    tmp_path: Path, repository_factory: Any
) -> None:
    root = repository_factory(tmp_path, {f"dir{i}/source": str(i) for i in range(160)})
    _, bodies = source_snapshot(root, contents=True)
    assert len(bodies) == 160
    assert bodies["dir159/source"] == b"159"
    assert not any(".git" in name for name in bodies)


def test_source_classification_cache_is_bound_to_exact_content(
    tmp_path: Path, repository_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.execution import checks

    root = repository_factory(tmp_path, {"fixture.py": "ordinary source\n"})
    checks._SOURCE_SCREENING.clear()
    observed: list[bytes] = []
    original = checks.contains_secret_material

    def screen(body: bytes) -> bool:
        observed.append(body)
        return original(body)

    monkeypatch.setattr(checks, "contains_secret_material", screen)
    screened: set[str] = set()
    source_snapshot(root, _screened=screened)
    source_snapshot(root, _screened=screened)
    assert not screened and len(observed) == 1
    (root / "fixture.py").write_text("AKIA" + "A" * 16)
    source_snapshot(root, _screened=screened)
    assert screened == {"fixture.py"} and len(observed) == 2
