from __future__ import annotations

import json


class ConfigError(ValueError):
    pass
def _invalid(message):
    return ConfigError(message)

def _toml_value(value: object) -> str:
    if isinstance(value, (str, bool, int, list)):
        return json.dumps(value, ensure_ascii=False)
    raise _invalid("configuration value has an unsupported TOML type")
