from src import subject


def test_smoke():
    assert subject.html_to_text("<h2>Title</h2><p>Hello</p>") == "## Title\n\nHello"
