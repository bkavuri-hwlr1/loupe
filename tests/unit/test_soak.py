"""The non-live soak must detect lost writes and exercise real crash recovery."""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def load_script(name: str) -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"loupe_{name}_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def soak() -> ModuleType:
    return load_script("soak")


def test_default_duration_and_seed_are_reproducible(soak: ModuleType) -> None:
    args = soak.parser().parse_args([])
    assert args.duration == 1800
    assert args.seed == 20261022
    assert args.restart_interval == 120
    assert not args.inject_lost_write


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_duration_must_be_finite_and_positive(soak: ModuleType, value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        soak.positive(value)


def test_ledger_detects_later_loss_and_accepts_explicit_undo(
    soak: ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "published.txt"
    expected = {target.name: "accepted bytes\n"}
    target.write_text(expected[target.name])
    soak.verify_files(tmp_path, expected)
    target.write_text("an outside writer replaced the published change\n")
    with pytest.raises(soak.InvariantFailure, match="published change lost"):
        soak.verify_files(tmp_path, expected)
    target.unlink()
    with pytest.raises(soak.InvariantFailure, match="published change lost"):
        soak.verify_files(tmp_path, expected)
    soak.verify_files(tmp_path, {target.name: None})


@pytest.mark.parametrize("metric", list(asdict(load_script("soak").Bounds())))
def test_every_recorded_resource_limit_is_enforced(
    soak: ModuleType, metric: str
) -> None:
    bounds = soak.Bounds()
    maximum = asdict(bounds)[metric]
    with pytest.raises(soak.InvariantFailure, match=metric):
        soak.check_bounds({**asdict(bounds), metric: maximum + 1}, bounds)


def options(soak: ModuleType, tmp_path: Path, *extra: str) -> Any:
    return soak.parser().parse_args(
        [
            "--duration",
            "0.01",
            "--restart-interval",
            "0.01",
            "--cycle-interval",
            "0.01",
            "--output",
            str(tmp_path / "result.json"),
            *extra,
        ]
    )


def test_scripted_mixed_sessions_survive_kill_and_cleanup(
    soak: ModuleType, tmp_path: Path
) -> None:
    result = soak.run(options(soak, tmp_path))
    assert result["status"] == "passed", result
    assert result["cycles"] == 1
    assert result["restarts"] == 1
    assert result["disposable_root_removed"]
    assert not Path(result["root"]).exists()
    # Every sample contains exactly the declared bound names.
    assert result["samples"]
    assert all(set(sample) == set(result["bounds"]) for sample in result["samples"])
    assert result["rpc_latency_ms"]["count"] > 20


def test_injected_lost_publication_fails_the_real_non_live_soak(
    soak: ModuleType, tmp_path: Path
) -> None:
    result = soak.run(options(soak, tmp_path, "--inject-lost-write"))
    try:
        assert result["status"] == "failed"
        assert "published change lost or corrupted" in result["failure"]
        assert result["restarts"] == 1
        assert result["cycles"] == 0
    finally:
        shutil.rmtree(result["root"])


def test_missing_resource_measurement_fails_closed(soak: ModuleType) -> None:
    bounds = soak.Bounds()
    sample = asdict(bounds)
    del sample["open_files"]
    with pytest.raises(soak.InvariantFailure, match="metric missing: open_files"):
        soak.check_bounds(sample, bounds)


def test_scan_scale_measures_actual_scanners_without_a_provider_request() -> None:
    import asyncio

    scan = load_script("scan_scale")
    result = asyncio.run(scan.measure(20, 1))
    assert result["outside_observed_paths"] == 20
    assert result["outside_cold"]["result"] == 0
    assert result["outside_one_change"]["result"] == 1
    assert result["isolated_capture_cold"]["result"] == {
        "captured_paths": 20,
        "deferred_paths": 0,
    }
    assert result["isolated"] == {"status": "complete", "scanned_paths": 20}
    assert result["isolated_one_change"]["result"]["applied"] == 1


def test_scan_scale_growth_uses_real_post_admission_source_changes() -> None:
    import asyncio

    scan = load_script("scan_scale")
    result = asyncio.run(scan.measure_growth(20, 1, initial_files=2))
    assert result["status"] == "complete", result
    assert result["growth_steps"] == 1
    assert result["accepted_manifest_paths"] == 20
    assert result["accepted_manifest_entries"] >= 20
    assert result["full_safe_point_warm"]["result"]["applied"] == 0
    assert result["full_safe_point_one_change"]["result"]["applied"] == 1
