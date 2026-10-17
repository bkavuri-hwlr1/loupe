"""The mandatory check proves private runtime is available without revealing it."""

from pathlib import Path


def test_runtime_canary_is_available():
    setting = Path(".env").read_text().strip()
    name, separator, value = setting.partition("=")
    assert name == "LOUPE_C6_CANARY" and separator
    assert value.startswith("sk-") and len(value) == 43
    print("C6 runtime read passed")
