"""A small application component with a prescribed review proposal."""


def contained(root, name):
    base = root.resolve()
    target = (base / name).resolve()
    if target != base and base not in target.parents:
        raise ValueError("path escapes root")
    return target
