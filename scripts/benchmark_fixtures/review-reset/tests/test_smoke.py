from src import subject


def test_smoke(tmp_path):
    assert subject.Counter().add(2) == 2
