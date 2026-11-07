"""Admission counts do not expand authority's measured source checkpoints."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from test_physical_authority import _owner

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.workspace import isolated
from llm_cli.workspace.authority import PhysicalAuthority, checkpoint_source_paths
from llm_cli.workspace.regime import CutoverManager


@pytest.mark.parametrize("count", [20_000, 20_001])
def test_checkpoint_source_count_is_separate_from_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    paths = b"\0".join(f"{n:05}.py".encode() for n in range(count)) + b"\0"
    monkeypatch.setattr(isolated, "_passive", lambda *args: paths)
    assert len(isolated._source_paths(tmp_path, 50_000)) == count
    if count == 20_000:
        assert len(checkpoint_source_paths(tmp_path)) == count
    else:
        with pytest.raises(LlmCoordError, match="20000") as error:
            checkpoint_source_paths(tmp_path)
        assert error.value.code == ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
        assert "isolated.source_max_paths" not in str(error.value)


@pytest.mark.parametrize("operation", ["preview", "apply"])
def test_cutover_source_count_refuses_before_creating_an_intent(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "pass\n", "b.py": "pass\n"})
    owner = _owner(tmp_path, root, "cutover")
    manager = CutoverManager(owner.store, owner)
    try:
        plan = manager.preview(root)
        # Keep authority ownership stable so apply reaches its own source scan.
        with owner.pin(root):
            pass
        with owner.store.connection() as connection:
            events_before = connection.execute(
                "SELECT count(*) FROM checkout_events"
            ).fetchone()[0]
        paths = b"\0".join(f"{n:05}.py".encode() for n in range(20_001)) + b"\0"
        monkeypatch.setattr(isolated, "_passive", lambda *args: paths)
        with pytest.raises(LlmCoordError, match="20000-path limit") as error:
            if operation == "preview":
                manager.preview(root)
            else:
                manager.begin(root, plan["checkpoint"])
        assert error.value.code == ErrorCode.CUTOVER_BLOCKED
        assert "isolated.source_max_paths" not in str(error.value)
        with owner.store.connection() as connection:
            assert (
                connection.execute("SELECT count(*) FROM cutover_intents").fetchone()[0]
                == 0
            )
            assert (
                connection.execute("SELECT count(*) FROM checkout_events").fetchone()[0]
                == events_before
            )
        assert owner.regime(root)["regime"] == "legacy"
    finally:
        owner.close()


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("listing_growth", [False, True])
def test_clean_handoff_source_count_refuses_without_advancing_observations(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    restart: bool,
    listing_growth: bool,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "pass\n", "b.py": "pass\n"})
    owner = _owner(tmp_path, root, "handoff")
    current = owner
    try:
        with owner.pin(root):
            pass
        with owner.store.connection() as connection:
            epoch_before = connection.execute(
                "SELECT workspace_epoch FROM workspaces"
            ).fetchone()[0]
            events_before = connection.execute(
                "SELECT count(*) FROM checkout_events"
            ).fetchone()[0]
        owner.release_idle(force=True)
        if restart:
            owner.close()
            current = PhysicalAuthority(
                owner.store,
                owner.profile,
                "handoff-restarted",
                owner.socket,
                root=owner.root,
            )
            owner.store.authority = current
        at_limit = b"\0".join(f"{n:05}.py".encode() for n in range(20_000)) + b"\0"
        over_limit = at_limit + b"20000.py\0"
        observations = 0

        def source_listing(*args: object) -> bytes:
            nonlocal observations
            observations += 1
            return at_limit if listing_growth and observations == 1 else over_limit

        monkeypatch.setattr(isolated, "_passive", source_listing)
        with (
            pytest.raises(LlmCoordError, match="20000") as error,
            current.pin(root),
        ):
            pytest.fail("a source checkpoint above its measured limit was admitted")
        assert error.value.code == ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
        assert "isolated.source_max_paths" not in str(error.value)
        assert observations == (2 if listing_growth else 1)
        assert not current.is_owned(root)
        with owner.store.connection() as connection:
            assert (
                connection.execute("SELECT workspace_epoch FROM workspaces").fetchone()[
                    0
                ]
                == epoch_before
            )
            assert (
                connection.execute("SELECT count(*) FROM checkout_events").fetchone()[0]
                == events_before
            )
    finally:
        if current is not owner:
            current.close()
        owner.close()
