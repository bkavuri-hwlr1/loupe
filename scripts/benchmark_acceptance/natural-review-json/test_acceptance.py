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
    valid = {"path": "a.py", "line": 2, "message": " fix this "}
    bad = {"path": "a.py", "line": 99, "message": "outside file"}
    context = {"line_counts": {"a.py": 2}}
    assert subject._parse(
        "```json\n" + subject.json.dumps({"findings": [bad, valid]}) + "\n```", context
    ).findings == (("a.py", 2, "fix this"),)
    assert subject._parse(
        subject.json.dumps({"findings": [None, valid]}), context
    ).findings == (("a.py", 2, "fix this"),)
    for report in ({"findings": [bad]}, {"findings": [dict(valid, line=True)]}, {}):
        try:
            subject._parse(subject.json.dumps(report), context)
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError("invalid report accepted")
    assert subject._parse('{"findings": []}', context).findings == ()
    assert subject._parse("x" * 100001, context).failure is not None
