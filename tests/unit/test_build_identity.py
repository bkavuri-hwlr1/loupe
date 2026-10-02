"""A process can tell whether another process runs the same Loupe sources."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from llm_cli import build
from llm_cli.cli import app
from llm_cli.config.models import Settings
from llm_cli.daemon.service import DaemonService
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "llm_cli"
    (root / "cli").mkdir(parents=True)
    (root / "__init__.py").write_text("VERSION = 1\n")
    (root / "cli" / "app.py").write_text("print('app')\n")
    (root / "cli" / "notes.txt").write_text("not code\n")
    monkeypatch.setattr(build, "_PACKAGE", root)
    build.code_identity.cache_clear()
    yield root
    build.code_identity.cache_clear()


def _fresh() -> dict[str, str]:
    build.code_identity.cache_clear()
    return build.code_identity()


def test_fingerprint_tracks_source_content_and_layout(package: Path) -> None:
    original = _fresh()
    assert original["path"] == str(package)
    assert _fresh() == original
    (package / "cli" / "notes.txt").write_text("changed, but not source\n")
    assert _fresh() == original
    (package / "cli" / "app.py").write_text("print('changed')\n")
    edited = _fresh()
    assert edited["fingerprint"] != original["fingerprint"]
    (package / "cli" / "app.py").rename(package / "cli" / "main.py")
    assert _fresh()["fingerprint"] != edited["fingerprint"]


def test_identity_is_a_snapshot_for_the_life_of_the_process(package: Path) -> None:
    first = _fresh()
    (package / "cli" / "app.py").write_text("print('edited after start')\n")
    assert build.code_identity() == first


@pytest.mark.parametrize("same", [True, False])
def test_daemon_status_says_whether_it_matches_this_cli(same: bool) -> None:
    fingerprint = build.code_identity()["fingerprint"] if same else "0" * 64

    class Client:
        def call(self, method: str, params: object = None, **kwargs: Any) -> Any:
            assert method == "system.ping"
            return {"ready": True, "code_fingerprint": fingerprint}

    args = app.build_parser().parse_args(["daemon", "status"])
    status = app.dispatch(args, cast(DaemonClient, Client()))
    assert status["matches_this_cli"] is same
    assert status["ready"] is True


def test_daemon_ping_reports_the_code_it_runs(tmp_path: Path) -> None:
    paths = AppPaths.resolve(
        "test",
        environ={
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home=tmp_path,
    )
    service = DaemonService(
        paths, Settings(profile_id="test"), asyncio.Event(), boot_id="boot-ping"
    )
    try:
        ping = service._ping()
    finally:
        service.close()
    identity = build.code_identity()
    assert ping["code_fingerprint"] == identity["fingerprint"]
    assert ping["code_path"] == identity["path"]
