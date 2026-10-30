"""Admission measurements exercise production opens and disclose their bounds."""

from __future__ import annotations

import asyncio

from test_soak import load_script


def test_admission_measurement_reports_real_open_and_safe_points() -> None:
    script = load_script("scan_scale")
    result = asyncio.run(
        script.measure_admission(5, 1, source_max_paths=5, hydration_max_entries=20)
    )
    assert result["status"] == "complete", result
    assert result["admitted_source_paths"] == 5
    assert result["native_watched_roots"] >= 1
    assert result["isolated_open_seconds"] > 0
    assert result["peak_parent_rss_bytes"] > 0
    assert result["peak_child_rss_bytes"] > 0
    assert result["database_bytes"] > 0
    assert result["close_seconds"] > 0
    assert result["closed_workspace_state"] == "removed"
    assert result["private_root_removed"] is True
    assert result["safe_point_warm"]["result"] == {
        "applied": 0,
        "deferred": 0,
        "conflicted": 0,
    }
    assert result["safe_point_one_change"]["result"]["applied"] == 1
    assert result["hydration_budgets"]["max_metadata_bytes"] == 4 * 1024 * 1024
    assert result["hydration_budgets"]["max_seconds"] == 10
    assert result["hydration_budgets"]["max_depth"] == 64


def test_a_refused_admission_never_claims_a_successful_safe_point() -> None:
    script = load_script("scan_scale")
    result = asyncio.run(
        script.measure_admission(5, 1, source_max_paths=4, hydration_max_entries=20)
    )
    assert result["status"] == "unsupported", result
    assert "isolated.source_max_paths=4" in result["error"]
    assert "admitted_source_paths" not in result
    assert "safe_point_warm" not in result
    assert result["isolated_open_seconds"] > 0
    assert result["peak_parent_rss_bytes"] > 0
