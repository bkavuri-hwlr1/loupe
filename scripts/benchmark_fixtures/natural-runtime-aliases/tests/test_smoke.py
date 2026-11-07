from src import subject


def test_smoke():
    root = subject.Path(".").resolve()
    subject.register_runtime_paths(root, ("private",))
    assert subject.runtime_path(root, "private/file")
    assert not subject.runtime_path(root, "elsewhere")
