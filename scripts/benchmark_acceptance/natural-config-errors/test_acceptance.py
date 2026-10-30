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
    from datetime import UTC, date, datetime, time

    for value in (
        [date(1979, 5, 27)],
        [time(7, 32)],
        [datetime(1979, 5, 27)],
        [[datetime(1979, 5, 27, tzinfo=UTC)]],
        [{"x": date(1979, 5, 27)}],
    ):
        try:
            subject._toml_value(value)
        except subject.ConfigError:
            pass
        else:
            raise AssertionError("unsupported value was accepted")
    assert subject.json.loads(subject._toml_value(["hello", True, 4])) == [
        "hello",
        True,
        4,
    ]
