import importlib.util
import os
from pathlib import Path


def load_subject():
    root = Path(os.environ.get("BENCHMARK_REPOSITORY", "."))
    spec = importlib.util.spec_from_file_location(
        "review_subject", root / "src/subject.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contract(tmp_path):
    subject = load_subject()
    assert subject.timeout() == 30
    assert subject.timeout(0) == 0
