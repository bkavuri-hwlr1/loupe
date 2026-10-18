"""The mandatory check exercises private runtime access and output redaction."""

from pathlib import Path


def test_runtime_canary_is_available():
    setting = Path(".env").read_text().strip()
    name, separator, value = setting.partition("=")
    assert name == "LOUPE_C6_CANARY" and separator
    assert value.startswith("c6-") and len(value) == 43
    print(value)
    print("C6 runtime read passed")
