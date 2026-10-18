from src import subject


def test_smoke(tmp_path):
    assert subject.recent([{"at": 1}], 1) == [{"at": 1}]
