from src import subject


def test_smoke(tmp_path):
    assert subject.page([], 0, 2) == []
