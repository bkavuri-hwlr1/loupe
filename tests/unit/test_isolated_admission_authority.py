"""Admission, cutover and clean handoff have explicit source-count bounds."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from test_physical_authority import _owner

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.workspace import authority, isolated, regime
from llm_cli.workspace.authority import PhysicalAuthority, checkpoint_source_paths
from llm_cli.workspace.regime import CutoverManager


@pytest.mark.parametrize("count", [20_001, 50_000, 100_000])
def test_clean_handoff_accepts_every_configured_isolated_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    paths = b"\0".join(f"{n:05}.py".encode() for n in range(count)) + b"\0"
    monkeypatch.setattr(isolated, "_passive", lambda *args: paths)
    assert isolated._source_paths(tmp_path, count) == checkpoint_source_paths(
        tmp_path, max_paths=authority.HANDOFF_MAX_PATHS, operation="clean handoff"
    )


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
        paths = (
            b"\0".join(
                f"{n:05}.py".encode() for n in range(regime.CUTOVER_MAX_PATHS + 1)
            )
            + b"\0"
        )
        monkeypatch.setattr(isolated, "_passive", lambda *args: paths)
        with pytest.raises(
            LlmCoordError, match=r"50001 source paths.*50000-path limit"
        ) as error:
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
        monkeypatch.setattr(authority, "HANDOFF_MAX_PATHS", 2)
        at_limit = b"a.py\0b.py\0"
        over_limit = at_limit + b"c.py\0"
        observations = 0

        def source_listing(*args: object) -> bytes:
            nonlocal observations
            observations += 1
            return at_limit if listing_growth and observations == 1 else over_limit

        monkeypatch.setattr(isolated, "_passive", source_listing)
        with (
            pytest.raises(
                LlmCoordError,
                match=r"clean handoff observed 3 source paths.*2-path limit",
            ) as error,
            current.pin(root),
        ):
            pytest.fail("a source checkpoint above its measured limit was admitted")
        # Startup recovery retries this code instead of parking work.
        assert error.value.code == ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
        assert error.value.details is not None
        assert error.value.details["source_paths"] == 3
        assert error.value.details["limit"] == 2
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


@pytest.mark.parametrize(
    ("budget", "limit", "message"),
    [
        ("CUTOVER_MAX_FILE_BYTES", 3, "5-byte file, exceeding its 3-byte"),
        ("CUTOVER_MAX_TOTAL_BYTES", 8, "10 total source bytes, exceeding its 8-byte"),
        ("CUTOVER_MAX_SECONDS", 0, "cutover source checkpoint took"),
    ],
)
def test_cutover_reports_the_budget_that_stopped_it(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    budget: str,
    limit: int,
    message: str,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "pass\n", "b.py": "pass\n"})
    owner = _owner(tmp_path, root, "cutover")
    try:
        monkeypatch.setattr(regime, budget, limit)
        with pytest.raises(LlmCoordError, match=message) as refused:
            CutoverManager(owner.store, owner).preview(root)
        assert refused.value.code is ErrorCode.CUTOVER_BLOCKED
    finally:
        owner.close()


@pytest.mark.admission_scale
def test_second_profile_can_take_a_large_clean_checkout_but_not_a_dirty_one(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    root = repository_factory(
        tmp_path,
        {
            f"src/{n // 1000:03}/{n:05}.py": "pass\n"
            for n in range(authority.SHARED_ADMISSION_MAX_PATHS)
        },
    )
    first = _owner(tmp_path, root, "first")
    second = _owner(tmp_path, root, "second")
    try:
        with first.pin(root):
            pass
        first.release_idle(force=True)
        with second.pin(root):
            assert second.is_owned(root)
        # A simulated shutdown before confirmation leaves the owner journal dirty.
        with second.mutation(root, "manual", "unfinished"):
            pass
        second.close()
        with pytest.raises(LlmCoordError) as refusal, first.pin(root):
            pytest.fail("a dirty checkout was admitted to a second profile")
        assert refusal.value.code == ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
        assert (refusal.value.details or {})["owner_profile"] == "second"
    finally:
        second.close()
        first.close()
