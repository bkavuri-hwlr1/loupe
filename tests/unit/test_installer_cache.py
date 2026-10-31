"""Persistent installer artifacts stay private, bounded and input-separated."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from llm_cli.agent.source_policy import contains_secret_material
from llm_cli.errors import LlmCoordError
from llm_cli.workspace.installer_cache import (
    DEFAULT_MAX_BYTES,
    CacheError,
    InstallerCache,
)
from llm_cli.workspace.rebuild import inspect_tree, prepare, read_journal


def cache(tmp_path: Path, maximum: int = DEFAULT_MAX_BYTES) -> InstallerCache:
    return InstallerCache(tmp_path / "profile" / "installer-cache", maximum)


def test_two_instances_share_artifacts_and_metadata_contains_no_private_value(
    tmp_path: Path,
) -> None:
    canary = "ordinaryCacheMarkerR4s8p2v7"
    assert not contains_secret_material(canary)
    first = cache(tmp_path)
    with first.activity((canary, "second value")) as selected:
        (selected / "wheel").write_text("verified artifact")
        assert selected.stat().st_mode & 0o077 == 0
    restarted = cache(tmp_path)
    with restarted.activity(("second value", canary)) as selected_again:
        assert selected_again == selected
        assert (selected_again / "wheel").read_text() == "verified artifact"
    assert first.root.stat().st_mode & 0o077 == 0
    for path in first.root.iterdir():
        if path.is_file():
            assert canary.encode() not in path.read_bytes()
            assert path.stat().st_mode & 0o077 == 0


def test_rotating_back_never_reuses_a_former_private_value_namespace(
    tmp_path: Path,
) -> None:
    store = cache(tmp_path)
    with store.activity(("old ordinary marker",)) as old:
        (old / "private-cached-value").write_text("old ordinary marker")
    with store.activity(("new ordinary marker",)) as changed:
        assert changed != old
        assert not (changed / "private-cached-value").exists()
    with store.activity(("old ordinary marker",)) as returned:
        assert returned not in {old, changed}
        assert not (returned / "private-cached-value").exists()
    assert (old / "private-cached-value").exists()


def test_input_change_during_rebuild_invalidates_the_selected_namespace(
    tmp_path: Path,
) -> None:
    store = cache(tmp_path)
    with store.activity(("opening value",)) as selected:
        store.invalidate(selected)
    with store.activity(("opening value",)) as after:
        assert after != selected


def test_shared_active_leases_block_prune_but_not_another_installer(
    tmp_path: Path,
) -> None:
    store = cache(tmp_path)
    barrier = Barrier(2)

    def install() -> Path:
        with cache(tmp_path).activity(()) as selected:
            barrier.wait(timeout=10)
            assert store.prune() is False
            barrier.wait(timeout=10)
            return selected

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(install), pool.submit(install)
        assert first.result(timeout=15) == second.result(timeout=15)
    assert store.prune() is True


def test_cache_growth_is_detected_and_pruned_only_after_lease_exit(
    tmp_path: Path,
) -> None:
    store = cache(tmp_path, 64 * 1024)
    with store.activity(()) as selected:
        (selected / "oversized-download").write_bytes(b"x" * (128 * 1024))
        assert store.over_limit()
        assert not store.prune()
        assert (selected / "oversized-download").exists()
    assert not selected.exists()
    assert not (store.root / "head.json").exists()
    assert not store.over_limit()


def test_namespace_count_is_bounded_even_for_empty_old_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_cli.workspace import installer_cache

    monkeypatch.setattr(installer_cache, "MAX_NAMESPACES", 2)
    store = cache(tmp_path)
    for number in range(5):
        with store.activity((f"value-{number}",)):
            pass
    assert len(list(store.root.glob("ns-*"))) == 2


def test_cache_key_alias_and_unsafe_permissions_are_never_repaired_silently(
    tmp_path: Path,
) -> None:
    store = cache(tmp_path)
    with store.activity(()):
        pass
    target = tmp_path / "outside"
    target.write_text("unchanged outside")
    key = store.root / "namespace.key"
    key.unlink()
    key.symlink_to(target)
    with pytest.raises((OSError, CacheError)), store.activity(()):
        pytest.fail("unsafe key admitted")
    assert target.read_text() == "unchanged outside"
    key.unlink()
    store.root.chmod(0o755)
    with pytest.raises(CacheError, match="private"), store.activity(()):
        pytest.fail("unsafe storage admitted")
    assert store.root.stat().st_mode & 0o077


def test_oversized_cache_cleanup_never_follows_an_artifact_symlink(
    tmp_path: Path,
) -> None:
    store = cache(tmp_path, 64 * 1024)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_bytes(b"outside bytes")
    with store.activity(()) as selected:
        (selected / "alias").symlink_to(outside, target_is_directory=True)
        (selected / "oversized").write_bytes(b"x" * 128 * 1024)
    assert not selected.exists()
    assert sentinel.read_bytes() == b"outside bytes"


def test_verified_crash_scratch_is_removed_at_next_idle_lease(tmp_path: Path) -> None:
    store = cache(tmp_path)
    with store.activity(()):
        pass
    scratch = store.root / (".cache-" + "a" * 32)
    scratch.write_bytes(b"incomplete private metadata")
    scratch.chmod(0o600)
    with store.activity(()):
        assert not scratch.exists()


def test_cache_enabled_dependencies_reject_external_hardlinks(tmp_path: Path) -> None:
    private = tmp_path / "private"
    (private / ".venv").mkdir(parents=True)
    artifact = tmp_path / "cached-wheel-file"
    artifact.write_text("shared library bytes")
    os.link(artifact, private / ".venv" / "library.py")
    assert inspect_tree(private, ".venv", validate=True)["entries"] == 2
    with pytest.raises(LlmCoordError, match="hardlink"):
        inspect_tree(private, ".venv", validate=True, reject_hardlinks=True)
    assert artifact.read_text() == "shared library bytes"


def test_rebuild_journal_freezes_cache_consent_and_rejects_corrupt_bounds(
    tmp_path: Path,
) -> None:
    prepare(tmp_path, "owned", (), cache=True, cache_max_bytes=123_456)
    journal = read_journal(tmp_path, "owned")
    assert journal is not None
    assert journal["cache"] == {"enabled": True, "max_bytes": 123_456}
    journal["cache"]["max_bytes"] = True
    (tmp_path / "loupe-rebuild.json").write_text(json.dumps(journal))
    with pytest.raises(LlmCoordError, match="cache journal"):
        read_journal(tmp_path, "owned")
