"""Token counts and context size are shown briefly and honestly."""

from __future__ import annotations

import io

import pytest

from llm_cli.cli import session
from llm_cli.cli.composer import Composer
from llm_cli.cli.usage import context_percent, format_tokens, usage_lines


@pytest.mark.parametrize(
    ("count", "text"),
    [
        (0, "0"),
        (999, "999"),
        (1_250, "1.2k"),
        (9_999, "10.0k"),
        (63_400, "63k"),
        (999_400, "999k"),
        (1_250_000, "1.2M"),
    ],
)
def test_token_counts_are_short(count: int, text: str) -> None:
    assert format_tokens(count) == text


@pytest.mark.parametrize(
    ("tokens", "budget", "percent"),
    [
        (63_000, 240_000, 26),
        (0, 240_000, 0),
        (250_000, 240_000, 104),
        (None, 240_000, None),
        (63_000, None, None),
        (63_000, 0, None),
        (-1, 240_000, None),
        (True, 240_000, None),
    ],
)
def test_context_percent_needs_both_sizes(
    tokens: object, budget: object, percent: int | None
) -> None:
    assert context_percent(tokens, budget) == percent


def test_usage_lines_without_runs_or_context() -> None:
    assert usage_lines({"task_runs": 0, "usage": {}}) == [
        "No token usage recorded in this conversation yet.",
        "  Loupe counts tokens, not cost: prices depend on your provider and plan.",
    ]


def test_usage_lines_omit_unknown_details() -> None:
    lines = usage_lines(
        {
            "task_runs": 1,
            "usage": {"prompt_tokens": 2_000, "output_tokens": 300},
            "context_tokens": 2_300,
            "context_budget": None,
        }
    )

    assert lines[:4] == [
        "Token usage in this conversation (1 task run):",
        "  Prompt:  2.0k tokens",
        "  Output:  300 tokens",
        "  Context: 2.3k tokens.",
    ]


def test_usage_lines_show_the_explore_helpers_share() -> None:
    lines = usage_lines(
        {
            "task_runs": 1,
            "usage": {
                "prompt_tokens": 502_000,
                "output_tokens": 12_300,
                "explore_prompt_tokens": 480_000,
                "explore_output_tokens": 10_100,
            },
        }
    )

    assert lines[3] == (
        "  Of these, explore helpers used 480k prompt and 10k output tokens."
    )


def test_footer_shows_the_context_meter_only_when_known() -> None:
    composer = Composer(io.StringIO(), io.StringIO(), plain=True)
    composer.model = "gpt-6-sol"

    assert "Context" not in composer.status_text()

    composer.note_context(None, 240_000)
    assert composer.context_percent is None
    composer.note_context(120_000, 240_000)

    assert composer.status_text().endswith(" · Context: 50%")


def test_turn_and_summary_events_update_the_meter() -> None:
    composer = Composer(io.StringIO(), io.StringIO(), plain=True)

    session._note_context(
        composer,
        {
            "event_type": "model.turn.completed",
            "payload": {"context_tokens": 60_000, "context_budget": 240_000},
        },
    )
    assert composer.context_percent == 25

    session._note_context(
        composer,
        {
            "event_type": "model.context.compacted",
            "payload": {
                "context_tokens": 200_000,
                "summary_tokens": 2_400,
                "context_budget": 240_000,
            },
        },
    )
    assert composer.context_percent == 1

    # Events without size facts leave the meter as it was.
    session._note_context(
        composer, {"event_type": "model.turn.completed", "payload": {"usage": {}}}
    )
    assert composer.context_percent == 1
