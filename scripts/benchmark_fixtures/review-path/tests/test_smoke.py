from src import subject


def test_smoke(tmp_path):
    assert subject.contained(tmp_path, "child") == tmp_path / "child"
