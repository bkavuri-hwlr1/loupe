import importlib.util
import os
import sys
import uuid
from pathlib import Path


def load_subject():
    path = Path(os.environ["BENCHMARK_REPOSITORY"]) / "src" / "subject.py"
    name = "natural_subject_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(path.parents[1]))
    spec.loader.exec_module(module)
    return module


def test_contract():
    subject = load_subject()
    root = subject.Path(".").resolve()
    subject.register_runtime_paths(root, ("Runtime/Café", "private"))
    for path in (
        "runtime/cafe\u0301",
        "RUNTIME/CAFÉ/file.txt",
        "PRIVATE",
        "PrIvAtE/sub/file",
    ):
        assert subject.runtime_path(root, path), path
    for path in ("private-other", "privatefile", "Runtime/Café-extra", "other/private"):
        assert not subject.runtime_path(root, path), path
    assert not subject.runtime_path(root / "other", "private")
