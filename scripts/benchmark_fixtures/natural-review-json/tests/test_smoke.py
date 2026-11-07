from src import subject


def test_smoke():
    assert (
        subject._parse('{"findings": []}', {"line_counts": {"a.py": 2}}).findings == ()
    )
