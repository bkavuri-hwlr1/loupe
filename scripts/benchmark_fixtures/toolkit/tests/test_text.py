from toolkit.text import identity


def test_identity() -> None:
    assert identity("hello") == "hello"
