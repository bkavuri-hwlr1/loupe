from __future__ import annotations

import json


class ConfigError(ValueError):
    pass
def _invalid(message):
    return ConfigError(message)

def _toml_value(value: object) -> str:
    if isinstance(value, (str, bool, int, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise _invalid(
                "configuration value has an unsupported TOML type"
            ) from error
    raise _invalid("configuration value has an unsupported TOML type")
