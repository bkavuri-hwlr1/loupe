"""Incrementally read one string field from streamed JSON tool arguments.

Providers stream ``finish_task`` arguments as arbitrary JSON fragments. A chunk
can end inside a key, between an escape's backslash and its letter, or between
the two halves of a surrogate pair. Only text that is already unambiguous is
returned, so the caller can display it immediately and append later chunks.
"""

from __future__ import annotations

_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_HEX = frozenset("0123456789abcdefABCDEF")
_WHITESPACE = frozenset(" \t\r\n")
_REPLACEMENT = "�"


class AnswerFieldDecoder:
    """Decode the top-level string ``field`` of a JSON object as it streams.

    Other values, including nested objects and strings that merely contain the
    field name, are skipped. Malformed input stops decoding without raising;
    the authoritative parsed arguments still arrive with the completed call.
    """

    def __init__(self, field: str = "answer") -> None:
        self.field = field
        self._decoded: list[str] = []
        self._depth = 0
        # At depth 1: "key", "colon", "value", "scalar" or "comma".
        self._expect = "key"
        self._string: str | None = None  # "key", "target" or "skip"
        self._escape = ""
        self._high: int | None = None
        self._key: list[str] = []
        self._current_key = ""
        self._seen = False
        self.complete = False
        self.failed = False

    @property
    def text(self) -> str:
        return "".join(self._decoded)

    def feed(self, chunk: str) -> str:
        """Consume a fragment and return newly decoded field text."""

        start = len(self._decoded)
        for character in chunk:
            if self.failed or (self._depth == 0 and self._seen):
                break
            self._consume(character)
        return "".join(self._decoded[start:])

    def _consume(self, character: str) -> None:
        if self._string is not None:
            self._string_character(character)
            return
        if self._depth == 0:
            if character == "{":
                self._depth = 1
                self._seen = True
                self._expect = "key"
            elif character not in _WHITESPACE:
                self.failed = True
            return
        if self._depth > 1:
            if character == '"':
                self._string = "skip"
            elif character in "{[":
                self._depth += 1
            elif character in "}]":
                self._depth -= 1
                if self._depth == 1:
                    self._expect = "comma"
            return
        if character in _WHITESPACE and self._expect != "scalar":
            return
        if self._expect == "key":
            if character == '"':
                self._string = "key"
                self._key = []
            elif character == "}":
                self._depth = 0
            else:
                self.failed = True
        elif self._expect == "colon":
            if character == ":":
                self._expect = "value"
            else:
                self.failed = True
        elif self._expect == "value":
            if character == '"':
                target = self._current_key == self.field and not self.complete
                self._string = "target" if target else "skip"
            elif character in "{[":
                self._depth += 1
            else:
                self._expect = "scalar"
        elif self._expect == "scalar":
            if character == ",":
                self._expect = "key"
            elif character == "}":
                self._depth = 0
        elif self._expect == "comma":
            if character == ",":
                self._expect = "key"
            elif character == "}":
                self._depth = 0
            else:
                self.failed = True

    def _string_character(self, character: str) -> None:
        if self._escape:
            self._escape += character
            if self._escape[1] != "u":
                decoded = _SIMPLE_ESCAPES.get(character)
                self._escape = ""
                if decoded is None:
                    self.failed = True
                else:
                    self._emit(ord(decoded))
                return
            if len(self._escape) > 2 and character not in _HEX:
                self.failed = True
                return
            if len(self._escape) == 6:
                code = int(self._escape[2:], 16)
                self._escape = ""
                self._emit(code)
            return
        if character == "\\":
            self._escape = "\\"
            return
        if character == '"':
            self._close_string()
            return
        self._emit(ord(character))

    def _emit(self, code: int) -> None:
        """Append one UTF-16 code unit or code point, pairing surrogates."""

        if 0xD800 <= code <= 0xDBFF:
            if self._high is not None:
                self._append(_REPLACEMENT)
            self._high = code
            return
        if 0xDC00 <= code <= 0xDFFF:
            if self._high is None:
                self._append(_REPLACEMENT)
                return
            high, self._high = self._high, None
            self._append(chr(0x10000 + ((high - 0xD800) << 10) + (code - 0xDC00)))
            return
        if self._high is not None:
            self._high = None
            self._append(_REPLACEMENT)
        self._append(chr(code))

    def _append(self, text: str) -> None:
        if self._string == "target":
            self._decoded.append(text)
        elif self._string == "key":
            self._key.append(text)

    def _close_string(self) -> None:
        if self._high is not None:
            self._high = None
            self._append(_REPLACEMENT)
        kind, self._string = self._string, None
        if kind == "key":
            self._current_key = "".join(self._key)
            self._expect = "colon"
        elif self._depth == 1:
            if kind == "target":
                self.complete = True
            self._expect = "comma"


__all__ = ["AnswerFieldDecoder"]
