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

    class Handle:
        def __init__(self, fail):
            self.closed = False
            self.fail = fail

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def read(self):
            if self.fail:
                raise ValueError("read failed")
            return " label "

        def close(self):
            self.closed = True

    class File:
        def __init__(self, fail):
            self.handle = Handle(fail)

        def open(self):
            return self.handle

    for fail in (False, True):
        file = File(fail)
        try:
            label = subject.read_label(file)
        except ValueError:
            assert fail
        else:
            assert not fail
            assert label == "label"
        assert file.handle.closed
