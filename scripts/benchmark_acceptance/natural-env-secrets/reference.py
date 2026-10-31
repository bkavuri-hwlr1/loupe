from __future__ import annotations

import re
from contextlib import suppress
from urllib.parse import unquote, urlsplit

_UNNAMED_MINIMUM = 8
def url_credentials(text: str, *, minimum: int = 1) -> tuple[str, ...]:
    """Extract URL userinfo without exposing it in inspection or parser errors."""
    values: set[str] = set()
    with suppress(ValueError):
        parsed = urlsplit(text)
        if parsed.scheme and parsed.hostname:
            for raw, floor in (
                (parsed.password or "", minimum),
                (parsed.username or "", max(minimum, _UNNAMED_MINIMUM)),
            ):
                for value in (raw, unquote(raw)):
                    for candidate in (value.strip(), *value.splitlines()):
                        if len(candidate.strip()) >= floor:
                            values.add(candidate.strip())
    return tuple(sorted(values, key=len, reverse=True))
_SECRET_NAME = re.compile(
    r"secret|password|passwd|token|api_?key|credential|authorization|private_?key|"
    r"access_?key|(?:database|db|redis|mongo(?:db)?|postgres|mysql|amqp)_(?:url|uri|dsn)|"
    r"(?:^|_)(?:pass|pwd|dsn|webhook)(?:_|$)|_(?:key|sk)$|^docker_auth_config$",
    re.I,
)


def _screening_values(value: str) -> set[str]:
    # The supervisor screens complete output lines before emitting them. A
    # multiline value therefore also needs its nonempty logical lines screened;
    # never split into arbitrary read-sized prefixes or alter the child value.
    return {part for part in (value, *value.splitlines()) if part}


def _sensitive_values(name: str, value: str) -> set[str]:
    credentials: set[str] = set()
    # Extra package indexes can contain several whitespace-separated URLs.
    for component in (value, *value.split()):
        extracted = url_credentials(component)
        if extracted:
            credentials.update(extracted)
            credentials.update(_screening_values(component))
    if _SECRET_NAME.search(name) or credentials:
        credentials.update(_screening_values(value))
    return credentials
