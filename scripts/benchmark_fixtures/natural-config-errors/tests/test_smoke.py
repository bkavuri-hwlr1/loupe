from src import subject


def test_smoke():
    assert subject.json.loads(subject._toml_value("hello")) == "hello"
