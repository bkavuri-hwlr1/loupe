"""Clarification choices preserve custom answers and reject unusable prompts."""

from pathlib import Path

import pytest

from llm_cli.agent.tools import MAX_QUESTION_CHARACTERS, ToolBroker, tool_schemas


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("1", "JSON"), (" 2 ", "YAML"), ("TOML, please", "TOML, please"), ("42", "42")],
)
def test_suggested_answers_are_optional_shortcuts(
    tmp_path: Path, answer: str, expected: str
) -> None:
    questions: list[str] = []

    def ask(question: str) -> str:
        questions.append(question)
        return answer

    broker = ToolBroker(worktree=tmp_path, scopes=(".",), asker=ask)
    result = broker.invoke(
        "ask_user", {"question": "Which format?", "options": ["JSON", "YAML"]}
    )

    assert not result.is_error
    assert result.content == expected
    assert questions == [
        "Which format?\n\n1. JSON\n2. YAML\n\nReply with a number or your own answer."
    ]
    assert broker.usage.writes == 0
    assert not broker.usage.finished


def test_question_without_options_preserves_numeric_answer(tmp_path: Path) -> None:
    broker = ToolBroker(worktree=tmp_path, scopes=(".",), asker=lambda _: "42")
    assert broker.invoke("ask_user", {"question": "How many?"}).content == "42"


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"question": 1},
        {"question": " \n\t"},
        {"question": "q" * (MAX_QUESTION_CHARACTERS + 1)},
        {"question": "Which?", "options": None},
        {"question": "Which?", "options": "ab"},
        {"question": "Which?", "options": []},
        {"question": "Which?", "options": ["a"]},
        {"question": "Which?", "options": list("abcdef")},
        {"question": "Which?", "options": ["a", 2]},
        {"question": "Which?", "options": ["a", " "]},
        {"question": "Which?", "options": ["a", " a "]},
        {"question": "Which?", "options": ["a", "b\nc"]},
        {"question": "Which?", "options": ["a", "b\rc"]},
        {"question": "Which?", "options": ["a", "b" * 201]},
        {"question": "q" * MAX_QUESTION_CHARACTERS, "options": ["a", "b"]},
    ],
)
def test_invalid_question_does_not_wait_for_operator(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    def unexpected_question(_: str) -> str:
        pytest.fail("malformed questions must return a correctable tool error")

    broker = ToolBroker(worktree=tmp_path, scopes=(".",), asker=unexpected_question)
    assert broker.invoke("ask_user", arguments).is_error


def test_question_schema_keeps_free_text_questions_compatible() -> None:
    schema = tool_schemas(("ask_user",))[0]["input_schema"]
    assert isinstance(schema, dict)
    assert schema["required"] == ["question"]
    assert "options" in schema["properties"]
