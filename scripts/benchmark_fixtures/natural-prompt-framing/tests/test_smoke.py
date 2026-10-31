from src import subject


def test_smoke():
    from types import SimpleNamespace

    class Stream:
        def __init__(self):
            self.data = ""
            self.closed = False

        def write(self, value):
            self.data += value

        def close(self):
            self.closed = True

    stream = Stream()
    subject.submit_prompt(
        SimpleNamespace(prompt="hello"), SimpleNamespace(stdin=stream)
    )
    assert stream.data == "hello\n" and stream.closed
