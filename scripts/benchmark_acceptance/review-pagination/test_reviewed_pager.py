"""Hidden acceptance: the reviewed result must keep every item on each page."""

import importlib.util
import os
from pathlib import Path


def test_full_page_keeps_its_last_item():
    root = Path(os.environ.get("BENCHMARK_REPOSITORY", "."))
    spec = importlib.util.spec_from_file_location(
        "reviewed_pager", root / "src/pager.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.page(range(20), 0, 10) == list(range(10))
    assert module.page(range(20), 10, 10) == list(range(10, 20))
    assert module.page([1], 0, 1) == [1]
