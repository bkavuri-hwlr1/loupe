from src import subject


def test_smoke(tmp_path):
    assert subject.timeout(10) == 10
