"""Runtime generations and physical checkout authority recover together."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_runtime_refresh import (
    test_crash_each_refresh_step_recovers_one_complete_generation as _crash_case,
)
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env


@pytest.mark.parametrize(
    "stage",
    [
        "runtime_staging",
        "runtime_prepared",
        "runtime_removed:a.local",
        "runtime_installed:a.local",
        "runtime_removed:b.local",
        "runtime_installed:b.local",
        "runtime_committed",
        "runtime_cleaned",
        "runtime_authority_confirmed",
    ],
)
def test_runtime_refresh_crash_keeps_checkout_authority_recoverable(
    batch_env: BatchEnvironment, tmp_path: Path, stage: str
) -> None:
    PhysicalAuthority = pytest.importorskip(
        "llm_cli.workspace.authority"
    ).PhysicalAuthority
    env = batch_env
    authority = PhysicalAuthority(
        env.store,
        "runtime-owner",
        "runtime-boot",
        tmp_path / "daemon.sock",
        root=tmp_path / "authority" / "v1",
    )
    env.store.authority = authority
    try:
        _crash_case(env, tmp_path, stage)
        assert authority.status(env.repository)["state"] == "owned"
        with authority.pin(env.repository):
            authority.validate_current()
    finally:
        authority.close()
