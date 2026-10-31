from src import subject


def test_smoke():
    assert callable(subject._filesystem)
