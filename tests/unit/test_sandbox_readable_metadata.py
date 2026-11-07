"""Readable runtimes may inspect ancestor metadata without exposing siblings."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from llm_cli.execution.sandbox import SEATBELT, SandboxPolicy, available_sandbox, wrap


def _framework_python() -> Path:
    candidates = (
        Path(getattr(sys, "_base_executable", sys.executable)),
        Path(sys.base_prefix) / "bin/python3",
        Path(
            "/Library/Developer/CommandLineTools/Library/Frameworks/"
            "Python3.framework/Versions/Current/bin/python3"
        ),
        Path("/usr/bin/python3"),
    )
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file():
            continue
        result = subprocess.run(
            [
                str(candidate),
                "-I",
                "-c",
                "import sysconfig; "
                "print(bool(sysconfig.get_config_var('PYTHONFRAMEWORK')))",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip() == "True":
            return candidate
    pytest.skip("requires a local macOS Framework Python; no download is attempted")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Framework launcher")
def test_framework_runtime_reads_ancestor_metadata_but_no_sibling_data(
    tmp_path: Path,
) -> None:
    if available_sandbox() != SEATBELT:
        pytest.skip("requires the native macOS sandbox")
    parent = tmp_path / "protected"
    runtime = parent / "runtime"
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(
        [str(_framework_python()), "-I", "-m", "venv", "--without-pip", str(runtime)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    allowed = runtime / "readable.txt"
    allowed.write_text("runtime input")
    sibling = parent / "private.txt"
    sibling.write_text("private sibling must stay hidden")
    program = f"""
import os
from pathlib import Path
assert Path({str(allowed)!r}).resolve(strict=True).read_text() == 'runtime input'
assert Path({str(parent)!r}).stat().st_mode
try:
    Path({str(sibling)!r}).read_text()
except PermissionError:
    pass
else:
    raise AssertionError('sibling file became readable')
try:
    os.listdir({str(parent)!r})
except PermissionError:
    pass
else:
    raise AssertionError('protected directory became listable')
print('runtime readable; sibling data and listing denied')
"""
    result = subprocess.run(
        wrap(
            SEATBELT,
            SandboxPolicy(writable=(work,), protected=(parent,), readable=(runtime,)),
            [str(runtime / "bin/python"), "-I", "-c", program],
            cwd=work,
        ),
        cwd=work,
        env={"PATH": os.defpath, "HOME": str(work)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "runtime readable; sibling data and listing denied"
