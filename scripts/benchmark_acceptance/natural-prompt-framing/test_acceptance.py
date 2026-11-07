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
    from types import SimpleNamespace

    class Stream:
        def __init__(self):
            self.data = ""
            self.closed = False

        def write(self, value):
            self.data += value

        def close(self):
            self.closed = True

    for prompt, expected in (
        ("a\nb", "a b\n"),
        ("a\r\nb", "a b\n"),
        ("a\rb", "a b\n"),
        ("a\r\nb\nc\rd", "a b c d\n"),
        ("café", "café\n"),
    ):
        stream = Stream()
        subject.submit_prompt(
            SimpleNamespace(prompt=prompt), SimpleNamespace(stdin=stream)
        )
        assert stream.data == expected
        assert stream.closed
