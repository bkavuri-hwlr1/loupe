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
    text = subject.html_to_text(
        '<h2>Usage<a href="#usage">¶</a></h2><h3>More<a href="#more">#</a></h3>'
        '<p>See <a href="#top">top</a> note<a href="#fn1">1</a>!</p>'
    )
    assert text == "## Usage\n\n### More\n\nSee top note1!"
    assert (
        subject.html_to_text('<p><a href="https://docs.example.invalid/x">link</a></p>')
        == "link (https://docs.example.invalid/x)"
    )
    assert (
        subject.html_to_text("<script>private script</script><p>Visible ¶</p>")
        == "Visible ¶"
    )
    assert (
        subject.html_to_text('<h2>A<a href="#a"><svg><path/></svg></a></h2>') == "## A"
    )
