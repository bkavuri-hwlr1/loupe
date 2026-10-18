"""Real medium-repository baseline builds stay inside the public time budget."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from test_knowledge_index import _index
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env


@pytest.mark.parametrize("file_count", [1_000, 10_000])
def test_medium_repository_build_meets_budget(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Callable[..., str],
    file_count: int,
) -> None:
    source = batch_env.repository / "synthetic"
    source.mkdir()
    for number in range(file_count):
        # Forty-line Python modules with distinct blob IDs, not one blob reused
        # thousands of times. Fixture creation and Git commit are not timed.
        text = f"def fixture_{number}(value):\n    result = value\n"
        text += "".join(f"    result += {line}\n" for line in range(37))
        text += "    return result\n"
        (source / f"module_{number:05}.py").write_text(text)
    git_run(batch_env.repository, "add", "synthetic")
    git_run(batch_env.repository, "commit", "-m", "synthetic scale fixture")
    index, checkout = _index(batch_env, tmp_path)
    started = time.monotonic()
    built = index.build(checkout)
    elapsed = time.monotonic() - started
    assert built["state"] == "ready", built
    assert built["documents"] == file_count + 2
    assert elapsed < 30, f"{file_count} files took {elapsed:.3f}s"
    results = index.search_lexical(checkout, f"fixture_{file_count - 1}")["results"]
    assert results and results[0]["path"] == f"synthetic/module_{file_count - 1:05}.py"
    print(f"lexical baseline: {file_count} files in {elapsed:.3f}s")
