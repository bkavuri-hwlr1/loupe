"""Invalid check files produce normal CLI errors before contacting the daemon."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("kind", ["missing", "directory", "malformed", "non_utf8"])
@pytest.mark.parametrize("as_json", [False, True])
def test_invalid_check_file_has_a_structured_cli_error(
    tmp_path: Path, kind: str, as_json: bool
) -> None:
    # Regression: QA-001 — invalid check files escaped the CLI error boundary.
    # Found by /qa on 2026-09-29.
    # Report: .gstack/qa-reports/2026-09-28/report.md
    config = tmp_path / "checks.toml"
    if kind == "directory":
        config.mkdir()
    elif kind == "malformed":
        config.write_text("[checks.test\n", encoding="utf-8")
    elif kind == "non_utf8":
        config.write_bytes(b"\xff\xfe")
    env = {
        **os.environ,
        "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
        "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
        "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
        "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
        "NO_COLOR": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "llm_cli.cli.app",
            "--json" if as_json else "--plain",
            "checks",
            "configure",
            "--file",
            str(config),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stdout + result.stderr
    if as_json:
        response = json.loads(result.stdout)
        assert response["ok"] is False
        assert response["error"]["code"] == "CONFIG_INVALID"
        assert "could not read checks configuration" in response["error"]["message"]
        assert result.stderr == ""
    else:
        assert result.stdout == ""
        assert "CONFIG_INVALID" in result.stderr
        assert "could not read checks configuration" in result.stderr
    assert not (tmp_path / "run").exists()
