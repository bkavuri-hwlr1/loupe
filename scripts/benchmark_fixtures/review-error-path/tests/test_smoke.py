from src import subject


def test_smoke(tmp_path):
    assert subject.parse_port("8080") == 8080
