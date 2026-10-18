"""A small application component with a prescribed review proposal."""


def parse_port(text):
    try:
        value = int(text)
    except ValueError:
        return None
    return value if 1 <= value <= 65535 else None
