from src import subject


def test_smoke(tmp_path):
    path = tmp_path / "label.txt"
    path.write_text("label\n")
    assert subject.read_label(path) == "label"
