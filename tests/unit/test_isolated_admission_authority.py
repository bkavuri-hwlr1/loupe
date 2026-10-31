"""Configured source counts survive complete authority observations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_physical_authority import _owner

from llm_cli.errors import LlmCoordError
from llm_cli.workspace import isolated
from llm_cli.workspace.authority import PhysicalAuthority
from llm_cli.workspace.external import ExternalWorkspaceScanner
from llm_cli.workspace.regime import CutoverManager


def test_unconfigured_source_observers_keep_the_existing_count_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = b"\0".join(f"{n:05}.py".encode() for n in range(20_001)) + b"\0"
    monkeypatch.setattr(isolated, "_passive", lambda *args: paths)
    with pytest.raises(LlmCoordError, match=r"isolated\.source_max_paths=20000"):
        isolated._source_paths(tmp_path)
    assert len(isolated._source_paths(tmp_path, 25_000)) == 20_001


def test_large_source_count_survives_clean_reacquire_restart_and_checkpoint(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {f"{n:05}.py": "pass\n" for n in range(20_001)})
    owner = _owner(tmp_path, root, "large")
    owner.source_max_paths = 25_000
    restarted: PhysicalAuthority | None = None
    try:
        with owner.pin(root):
            pass
        owner.release_idle(force=True)
        assert not owner.is_owned(root)
        with owner.pin(root):
            pass
        owner.close()
        restarted = PhysicalAuthority(
            owner.store,
            owner.profile,
            "large-restarted",
            owner.socket,
            root=owner.root,
            source_max_paths=25_000,
        )
        owner.store.authority = restarted
        with restarted.pin(root):
            pass
        checkpoint = CutoverManager(owner.store, restarted).preview(root)
        assert checkpoint["checkpoint"] and checkpoint["checkpoint_observed"]
        with owner.store.connection() as connection:
            handoffs = connection.execute(
                "SELECT count(*) FROM checkout_events "
                "WHERE event_type='checkout.authority_handoff'"
            ).fetchone()[0]
        assert handoffs == 2
        # The upper end of the newly admitted set must still be observed. A
        # changed path beyond20k cannot disappear through a truncated handoff.
        restarted.release_idle(force=True)
        monkeypatch.setattr(
            owner.store,
            "list_sessions",
            lambda **kwargs: [SimpleNamespace(session_id="changed-tail")],
        )

        def changed_tail(self: ExternalWorkspaceScanner, session_id: str) -> int:
            (root / "20000.py").write_text("outside edit\n")
            return 0

        monkeypatch.setattr(
            ExternalWorkspaceScanner, "_scan_session_owned", changed_tail
        )
        with (
            pytest.raises(LlmCoordError, match="source changed during clean handoff"),
            restarted.pin(root),
        ):
            pass
    finally:
        if restarted is not None:
            restarted.close()
        owner.close()


def test_configured_count_refusal_remains_actionable_for_authority(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository_factory(tmp_path, {"a.py": "pass\n", "b.py": "pass\n"})
    owner = _owner(tmp_path, root, "small")
    owner.source_max_paths = 1
    try:
        with owner.pin(root):
            pass
        owner.release_idle(force=True)
        with (
            pytest.raises(LlmCoordError, match=r"isolated\.source_max_paths=1"),
            owner.pin(root),
        ):
            pass
        with pytest.raises(LlmCoordError, match=r"isolated\.source_max_paths=1"):
            CutoverManager(owner.store, owner).preview(root)
    finally:
        owner.close()
