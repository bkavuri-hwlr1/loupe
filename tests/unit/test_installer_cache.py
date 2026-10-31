"""Persistent installer artifacts stay private, bounded and input-separated."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, current_thread

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


def test_same_input_installers_serialize_but_different_inputs_can_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = cache(tmp_path)
    entered, release, other_entered, selected_same = Event(), Event(), Event(), Event()
    select = InstallerCache._select

    def selected(store: InstallerCache, values: tuple[str, ...]) -> Path:
        result = select(store, values)
        if not values and current_thread().name != "MainThread":
            selected_same.set()
        return result

    monkeypatch.setattr(InstallerCache, "_select", selected)

    def install(values: tuple[str, ...]) -> Path:
        with cache(tmp_path).activity(values) as selected:
            if values == ():
                entered.set()
                assert release.wait(10)
            else:
                other_entered.set()
            return selected

    with ThreadPoolExecutor(max_workers=2) as pool, store.activity(()) as initial:
        same = pool.submit(install, ())
        assert selected_same.wait(10)
        different = pool.submit(install, ("changed input",))
        try:
            assert other_entered.wait(10), "different namespace was serialized"
            assert not entered.is_set(), "same namespace admitted two live writers"
            assert not store.prune()
        finally:
            release.set()
    assert same.result(timeout=10) != different.result(timeout=10)
    assert different.result(timeout=10) != initial
    assert store.prune()


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


def test_preexisting_cache_hardlink_is_refused_before_installer_can_write(
    tmp_path: Path,
) -> None:
    store = cache(tmp_path)
    with store.activity(()) as selected:
        pass
    outside = tmp_path / "outside-sentinel"
    outside.write_text("external original")
    os.link(outside, selected / "aliased-wheel")
    with pytest.raises(CacheError, match="hardlink"), store.activity(()):
        (selected / "aliased-wheel").write_text("must never run")
    assert outside.read_text() == "external original"
    assert store.prune(all_artifacts=True)
    assert outside.read_text() == "external original"
    with store.activity(()) as recovered:
        assert recovered != selected


def test_installer_created_cache_hardlink_fails_settled_handback(
    tmp_path: Path,
) -> None:
    store = cache(tmp_path)
    outside = tmp_path / "outside-sentinel"
    outside.write_text("unchanged")
    with pytest.raises(CacheError, match="hardlink"), store.activity(()) as selected:
        os.link(outside, selected / "bad-download")
    assert outside.read_text() == "unchanged"


def test_same_namespace_wait_has_positive_bound_and_operator_guidance(
    tmp_path: Path,
) -> None:
    store = cache(tmp_path)
    with store.activity(()), ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(lambda: _brief_lease(store))
        with pytest.raises(CacheError, match=r"retry.*disable"):
            waiting.result(timeout=5)


def _brief_lease(store: InstallerCache) -> None:
    with store.lease((), timeout=0.05):
        pytest.fail("same-namespace lease ignored its lock")


@pytest.mark.parametrize("corrupt", ["head.json", "namespace.key"])
def test_explicit_all_purge_recovers_corrupt_metadata_and_preserves_lock_inodes(
    tmp_path: Path, corrupt: str
) -> None:
    from llm_cli.workspace.installer_cache import requires_guard

    store = cache(tmp_path)
    with store.activity(()) as selected:
        (selected / "private-artifact").write_text("cached body")
    locks = {
        name: (store.root / name).stat().st_ino
        for name in ("leases.lock", "metadata.lock")
    }
    (store.root / corrupt).write_bytes(b"malformed")
    assert requires_guard(store.root)
    assert store.prune(all_artifacts=True)
    assert not selected.exists()
    assert not requires_guard(store.root)
    assert {name: (store.root / name).stat().st_ino for name in locks} == locks
    with store.activity(()) as recovered:
        assert recovered != selected
    assert requires_guard(store.root)


def test_partial_purge_retains_durable_protection_until_complete_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm_cli.workspace import installer_cache

    store = cache(tmp_path)
    for value in ("first", "second"):
        with store.activity((value,)) as selected:
            (selected / "artifact").write_text(value)
    remove = installer_cache._remove
    deleted = []

    def interrupted(root: Path, name: str) -> None:
        if deleted:
            raise OSError("simulated interrupted cleanup")
        remove(root, name)
        deleted.append(name)

    monkeypatch.setattr(installer_cache, "_remove", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        store.prune(all_artifacts=True)
    assert deleted
    assert (store.root / "protection.required").read_bytes() == b"1\n"
    assert installer_cache.requires_guard(store.root)
    monkeypatch.setattr(installer_cache, "_remove", remove)
    assert store.prune(all_artifacts=True)
    assert not installer_cache.requires_guard(store.root)


@pytest.mark.parametrize(
    "shape", ["marker", "dangling", "unknown", "legacy", "unsafe-lock"]
)
def test_unknown_or_malformed_cache_state_requires_guard(
    tmp_path: Path, shape: str
) -> None:
    from llm_cli.workspace.installer_cache import requires_guard

    store = cache(tmp_path)
    assert not requires_guard(store.root)
    store.root.mkdir(parents=True, mode=0o700)
    assert not requires_guard(store.root)
    if shape == "marker":
        (store.root / "protection.required").write_text("malformed")
    elif shape == "dangling":
        (store.root / "protection.required").symlink_to(tmp_path / "missing")
    elif shape == "unknown":
        (store.root / "unknown").mkdir()
    elif shape == "legacy":
        (store.root / "namespace.key").write_bytes(b"old state")
    else:
        (store.root / "leases.lock").symlink_to(tmp_path / "missing")
    assert requires_guard(store.root)


def test_prune_absent_cache_does_not_create_profile_state(tmp_path: Path) -> None:
    from llm_cli.workspace.installer_cache import prune_profile_cache

    store = cache(tmp_path)
    assert prune_profile_cache(store.root, DEFAULT_MAX_BYTES, True) == {
        "state": "empty",
        "pruned": True,
        "all": True,
    }
    assert not store.root.exists()


def test_enabled_service_purge_keeps_checks_guarded_until_explicit_disabled_reset(
    tmp_path: Path,
) -> None:
    from llm_cli.workspace.installer_cache import register_guard, requires_guard

    store = cache(tmp_path)
    release = register_guard(store.root, enabled=True)
    try:
        assert requires_guard(store.root)
        assert store.prune(all_artifacts=True)
        assert (store.root / "protection.required").exists()
        assert requires_guard(store.root)
        with store.activity(()) as selected:
            (selected / "artifact").write_text("new cache after purge")
        assert requires_guard(store.root)
        assert store.prune(all_artifacts=True)
    finally:
        release()
    # Restart with caching disabled observes prior protection. Explicit purge
    # can now clear the durable marker, but this daemon stays guarded until its
    # own lifetime ends; registrations cannot release another service's guard.
    disabled = register_guard(store.root, enabled=False)
    second = register_guard(store.root, enabled=False)
    assert store.prune(all_artifacts=True)
    assert not (store.root / "protection.required").exists()
    assert requires_guard(store.root)
    disabled()
    disabled()
    assert requires_guard(store.root)
    second()
    assert not requires_guard(store.root)
    never_enabled = register_guard(store.root, enabled=False)
    assert not requires_guard(store.root)
    never_enabled()


def test_waiting_installer_reselects_after_prior_writer_invalidates_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = cache(tmp_path)
    waiting = Event()
    select = InstallerCache._select

    def selected(store: InstallerCache, values: tuple[str, ...]) -> Path:
        result = select(store, values)
        if current_thread().name != "MainThread":
            waiting.set()
        return result

    monkeypatch.setattr(InstallerCache, "_select", selected)

    def waiter() -> Path:
        with store.lease(("opening value",), timeout=2) as lease:
            assert not (lease.path / "contaminated").exists()
            return lease.path

    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        store.activity(("opening value",)) as old,
    ):
        (old / "contaminated").write_text("changed private input during prior install")
        queued = pool.submit(waiter)
        assert waiting.wait(5)
        store.invalidate(old)
    assert queued.result(timeout=5) != old
