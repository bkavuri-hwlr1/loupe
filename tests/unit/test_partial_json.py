"""Streamed finish_task arguments reveal their answer exactly and incrementally."""

from __future__ import annotations

import itertools
import json

from hypothesis import given
from hypothesis import strategies as st

from llm_cli.cli.partial_json import AnswerFieldDecoder


def _decode(chunks: list[str]) -> tuple[str, AnswerFieldDecoder]:
    decoder = AnswerFieldDecoder()
    pieces = [decoder.feed(chunk) for chunk in chunks]
    assert "".join(pieces) == decoder.text
    return decoder.text, decoder


def _split(text: str, cuts: list[int]) -> list[str]:
    points = sorted({0, len(text), *(cut % (len(text) + 1) for cut in cuts)})
    return [text[start:end] for start, end in itertools.pairwise(points)]


@given(
    answer=st.text(),
    summary=st.text(),
    answer_first=st.booleans(),
    ascii_only=st.booleans(),
    cuts=st.lists(st.integers(min_value=0), max_size=40),
)
def test_any_answer_split_anywhere_decodes_exactly(
    answer: str,
    summary: str,
    answer_first: bool,
    ascii_only: bool,
    cuts: list[int],
) -> None:
    fields = [("answer", answer), ("summary", summary)]
    if not answer_first:
        fields.reverse()
    encoded = json.dumps(dict(fields), ensure_ascii=ascii_only)
    decoded, decoder = _decode(_split(encoded, cuts))
    assert decoded == answer
    assert decoder.complete and not decoder.failed


def test_text_appears_before_the_arguments_finish() -> None:
    decoder = AnswerFieldDecoder()
    assert decoder.feed('{"answer":"Hello') == "Hello"
    assert decoder.feed(" wor") == " wor"
    assert not decoder.complete
    assert decoder.feed('ld","outcome":"completed"}') == "ld"
    assert decoder.complete


def test_escapes_and_surrogate_pairs_split_across_chunks_wait_for_completion() -> None:
    decoder = AnswerFieldDecoder()
    assert decoder.feed('{"answer":"a\\') == "a"
    assert decoder.feed("n") == "\n"
    assert decoder.feed("\\ud83d") == ""
    assert decoder.feed("\\ude") == ""
    assert decoder.feed('00"}') == "😀"


def test_the_field_name_inside_other_values_is_not_the_answer() -> None:
    arguments = {
        "summary": 'the "answer": nothing',
        "notes": {"answer": "nested", "list": ["answer", {"answer": "x"}]},
        "count": 3,
        "flag": True,
        "answer": "real",
    }
    decoded, _ = _decode(list(json.dumps(arguments, indent=1)))
    assert decoded == "real"


def test_missing_or_malformed_answer_yields_nothing() -> None:
    assert _decode(['{"summary":"only a report"}'])[0] == ""
    decoded, decoder = _decode(['{"answer":"ok \\q then more"}'])
    assert decoded == "ok "
    assert decoder.failed
    assert _decode(["not json"])[1].failed


def test_lone_surrogates_become_replacement_characters() -> None:
    assert _decode(['{"answer":"x\\ud83dy\\ude00"}'])[0] == "x�y�"
