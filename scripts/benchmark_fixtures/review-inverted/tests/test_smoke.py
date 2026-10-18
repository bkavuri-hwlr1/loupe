from src import subject


def test_smoke(tmp_path):
    assert subject.eligible(12, False) is False
