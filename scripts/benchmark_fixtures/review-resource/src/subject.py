"""A small application component with a prescribed review proposal."""


def read_label(path):
    handle = path.open()
    try:
        return handle.read().strip()
    finally:
        handle.close()
