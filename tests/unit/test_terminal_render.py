"""Compact transcripts preserve answers while hiding internal activity payloads."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from llm_cli.cli.render import EventRenderer, question_text, render_event
from llm_cli.cli.terminal import TerminalUI, TextSanitizer, safe_text


def _event(kind: str, **payload: Any) -> dict[str, Any]:
    return {"event_type": kind, "payload": payload}


def test_streamed_answer_is_immediate_and_not_repeated_by_completions() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(_event("model.turn.started", turn_id="turn-1"))
    renderer.render(_event("model.text.delta", text="The full ", block_id="a"))
    assert "The full " in output.getvalue()
    renderer.render(_event("model.text.delta", text="answer.", block_id="a"))
    renderer.render(_event("model.said", text="The full answer."))
    renderer.render(_event("model.finished", answer="The full answer.", tool_calls=0))
    assert "✓ Done" not in output.getvalue()
    renderer.render(_event("publication.confirmed"))
    assert "✓ Done" not in output.getvalue()
    renderer.render(_event("execution.published"))
    renderer.render(_event("execution.published"))
    renderer.finish()
    assert output.getvalue().count("The full answer.") == 1
    assert "\x1b" not in output.getvalue()
    assert output.getvalue().count("✓ Changes applied") == 1
    assert "published" not in output.getvalue()


def test_authoritative_completion_appends_missing_stream_suffix() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(_event("model.text.delta", text="The", block_id="a"))
    renderer.render(_event("model.said", text="The entire response."))
    assert "The entire response." in output.getvalue()
    assert output.getvalue().count("Loupe") == 1


def test_completed_answer_hides_summary_and_read_only_bookkeeping() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    answer = "Loupe is a terminal coding agent with durable shared workspaces."
    renderer.render(
        _event(
            "model.finished",
            answer=answer,
            summary="Prepared a repository overview.",
            message_id="task:1:answer",
            outcome="completed",
        )
    )
    renderer.render(_event("workflow.verification", status="Not verified"))
    renderer.render(_event("execution.published", outcome="no_changes"))
    renderer.finish()
    text = output.getvalue()
    assert answer in text
    for bookkeeping in ("Prepared", "Verification", "Not verified", "Done", "no file"):
        assert bookkeeping not in text


def test_chunked_answer_remains_complete_and_message_id_prevents_replay() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    answer = "Repository architecture.\n" * 400 + "FINAL ANSWER DETAIL"
    chunks = [answer[index : index + 4096] for index in range(0, len(answer), 4096)]
    events = [
        _event(
            "model.finished",
            answer=chunk,
            summary="Operational summary only.",
            message_id="task:1:answer",
            part=index,
            final=index == len(chunks) - 1,
        )
        for index, chunk in enumerate(chunks)
    ]
    for event in events + events:
        renderer.render(event)
    renderer.finish()
    assert output.getvalue().count(answer) == 1
    assert "Operational summary" not in output.getvalue()


def test_unknown_internal_events_never_interrupt_a_public_message() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(_event("model.text.delta", text="An actual "))
    renderer.render(_event("internal.future_event", text="PRIVATE EVENT PAYLOAD"))
    renderer.render(_event("model.text.delta", text="answer."))
    renderer.render(_event("model.finished", answer="An actual answer."))
    renderer.finish()
    assert output.getvalue().count("An actual answer.") == 1
    assert "internal.future_event" not in output.getvalue()
    assert "PRIVATE EVENT PAYLOAD" not in output.getvalue()
    assert render_event(_event("internal.future_event")) is None


def test_legacy_summary_is_identified_without_being_claimed_as_an_answer() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(_event("model.finished", summary="Inspected the repository."))
    renderer.finish()
    assert "Task summary" in output.getvalue()
    assert "Inspected the repository." in output.getvalue()
    assert "Loupe" not in output.getvalue()


def test_edit_completion_retains_verification_and_review_actions() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(_event("workflow.verification", status="Passed"))
    renderer.render(_event("execution.published", outcome="published"))
    renderer.render(_event("workflow.awaiting_review"))
    renderer.finish()
    assert "Changes applied · checks passed" in output.getvalue()
    assert (
        "Changes ready for review. Use /diff, /checks, then /apply."
        in output.getvalue()
    )


def test_review_settlement_hides_internal_failure_and_labels_partial_work() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(_event("execution.failed", failure_code="REVIEW_REQUIRED"))
    renderer.render(
        _event(
            "workflow.awaiting_review",
            completion_outcome="partial",
            file_count=2,
            verification="failed",
        )
    )
    renderer.finish()
    text = output.getvalue()
    assert "2 files retained from an incomplete run · checks failed" in text
    assert "Review /diff and /checks before /apply" in text
    assert "REVIEW_REQUIRED" not in text
    assert "✗ failed" not in text
    assert "ready for review" not in text


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            "REVIEW_REQUIRED",
            "Changes were retained for review. Use /diff and /checks before /apply.",
        ),
        ("CANCELLED", "Stopped. Use /diff to inspect any retained edits."),
    ],
)
def test_settlement_failure_has_a_fallback_when_companion_event_is_missing(
    failure: str, expected: str
) -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(_event("execution.failed", failure_code=failure))
    assert expected not in output.getvalue()
    renderer.finish()
    assert expected in output.getvalue()
    assert failure not in output.getvalue()
    rendered = render_event(_event("execution.failed", failure_code=failure))
    assert expected in str(rendered)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("MODEL_BLOCKED", "The task is blocked. No changes were applied."),
        (
            "RESPONSE_INCOMPLETE",
            "The response ended before the task was complete. No changes were applied.",
        ),
    ],
)
def test_incomplete_outcomes_are_human_readable(failure: str, expected: str) -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(_event("execution.failed", failure_code=failure))
    renderer.finish()
    assert expected in output.getvalue()
    assert failure not in output.getvalue()


def test_identical_answers_from_consecutive_tasks_each_render_once() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    answer = "The same valid answer."
    for task_id in ("first", "second"):
        event = _event(
            "model.finished",
            answer=answer,
            message_id=f"{task_id}:1:answer",
            outcome="completed",
        )
        event["task_id"] = task_id
        renderer.render(event)
    renderer.finish()
    assert output.getvalue().count(answer) == 2


def test_partial_response_has_an_explicit_heading() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(
        _event("model.partial", text="The provider disconnected mid-answer.")
    )
    renderer.finish()
    assert "Partial response" in output.getvalue()
    assert "The provider disconnected mid-answer." in output.getvalue()


def test_mcp_activity_and_server_events_name_the_server() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output, plain=True)
    activity: list[str | None] = []
    renderer.ui.activity = activity.append  # type: ignore[method-assign,assignment]
    renderer.render(_event("tool.called", tool="mcp__docs__search", call=1))
    assert activity[-1] == "Using MCP server docs…"
    renderer.render(_event("mcp.server.started", server="docs", tools=3))
    renderer.render(
        _event("mcp.server.failed", server="git", reason="it could not start")
    )
    renderer.finish()
    text = output.getvalue()
    assert "using MCP server docs (3 tools)" in text
    assert "! an MCP server is unavailable (git: it could not start)" in text


def test_a_retry_is_announced_and_drops_the_failed_draft() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output, plain=True)
    previews: list[str | None] = []
    renderer.ui.preview = previews.append  # type: ignore[method-assign,assignment]
    renderer.render(_event("model.turn.started", turn_id="first"))
    renderer.render(_event("model.answer.preview", text="Half an ans", block_id="1"))
    renderer.render(
        _event(
            "model.retrying",
            reason="the connection to the model provider was lost",
            retry=1,
            retries=3,
            delay=2.0,
        )
    )

    assert previews == ["Half an ans", None]
    renderer.finish()
    text = output.getvalue()
    assert (
        "  ! retrying the model request (the connection to the model provider "
        "was lost; retry 1 of 3 in 2s)\n"
    ) in text
    assert "Half an ans" not in text


def test_reasoning_is_hidden_and_explicit_messages_remain_visible() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    for turn in ("first", "second"):
        renderer.render(_event("model.turn.started", turn_id=turn))
        renderer.render(
            _event("model.reasoning.delta", text="Inspect files.", block_id="r")
        )
        renderer.render(_event("model.reasoning", text="Inspect files."))
        renderer.render(_event("model.said", text="I checked the files."))
        renderer.render(_event("model.turn.completed", usage={"output_tokens": 8}))
    renderer.finish()
    text = output.getvalue()
    assert "Inspect files." not in text
    assert text.count("I checked the files.") == 2
    assert "model.turn.completed" not in text


def test_multiblock_stream_matches_complete_response() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(_event("model.text.delta", text="First paragraph", block_id="a"))
    renderer.render(_event("model.text.delta", text="Second paragraph", block_id="b"))
    renderer.render(_event("model.said", text="First paragraph\nSecond paragraph"))
    renderer.finish()
    assert output.getvalue().count("First paragraph") == 1
    assert output.getvalue().count("Second paragraph") == 1


def test_chunked_response_is_complete_and_successful_tool_results_stay_hidden() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    response = "A detailed response.\n" * 800 + "THE FINAL SENTENCE"
    for index, chunk in enumerate((response[:4096], response[4096:])):
        renderer.render(_event("model.said", text=chunk, part=index, final=index == 1))
    result = "line of output\n" * 900 + "LAST TOOL LINE"
    for index, chunk in enumerate((result[:4096], result[4096:])):
        renderer.render(
            _event(
                "model.tool_result",
                content=chunk,
                call_id="c",
                part=index,
                final=index == 1,
            )
        )
    renderer.finish()
    assert response in output.getvalue()
    assert "line of output" not in output.getvalue()
    assert "LAST TOOL LINE" not in output.getvalue()
    assert "truncated" not in output.getvalue()


@pytest.mark.parametrize("tool", ["read_file", "write_file"])
def test_streaming_tools_never_dump_arguments_or_successful_results(tool: str) -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    code = "def example():\n    return 42\n"
    arguments = {"path": "example.py", "content": code}
    renderer.render(
        _event(
            "model.tool.delta",
            tool=tool,
            call_id="a",
            arguments_delta=json.dumps(arguments),
        )
    )
    renderer.render(
        _event("model.tool_call", tool=tool, call_id="a", arguments=arguments)
    )
    renderer.render(_event("tool.called", tool=tool, call=1))
    renderer.render(_event("model.tool_result", tool=tool, call_id="a", content=code))
    renderer.finish()
    assert f"→ {tool}" not in output.getvalue()
    assert "def example" not in output.getvalue()
    assert "return 42" not in output.getvalue()
    assert '"content"' not in output.getvalue()


def test_chunked_tool_arguments_and_finish_summary() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    patch = "@@ -1 +1 @@\n-old\n+new\n" * 400
    arguments = json.dumps({"patch": patch})
    for index, chunk in enumerate((arguments[:4096], arguments[4096:])):
        renderer.render(
            _event(
                "model.tool_call",
                tool="apply_patch",
                call_id="a",
                arguments_text=chunk,
                part=index,
                final=index == 1,
            )
        )
    for index, chunk in enumerate(("All requested ", "changes are complete.")):
        renderer.render(
            _event(
                "model.finished",
                summary=chunk,
                part=index,
                final=index == 1,
                tool_calls=1,
            )
        )
    renderer.finish()
    assert "@@ -1 +1 @@" not in output.getvalue()
    assert "All requested changes are complete." in output.getvalue()
    assert "✓ Done" not in output.getvalue()
    renderer.render(_event("execution.published", outcome="no_changes"))
    assert "Task summary" in output.getvalue()
    assert "✓ Done" not in output.getvalue()
    assert "no file changes" not in output.getvalue()


def test_provider_failure_ends_partial_line_and_retry_has_new_block() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(_event("model.turn.started", turn_id="failed"))
    renderer.render(_event("model.text.delta", text="Partial response"))
    renderer.render(_event("model.finished", summary="Partial response"))
    renderer.render(_event("execution.failed", failure_code="PROVIDER_UNAVAILABLE"))
    renderer.render(_event("model.turn.started", turn_id="retry"))
    renderer.render(_event("model.text.delta", text="New response"))
    renderer.render(_event("model.said", text="New response"))
    renderer.finish()
    assert "Partial response\n  ✗ failed" in output.getvalue()
    assert "Done " not in output.getvalue()
    assert output.getvalue().count("New response") == 1


def test_interruption_preserves_partial_answer_without_leaking_tool_arguments() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(
        _event(
            "model.tool.delta",
            call_id="a",
            tool="write_file",
            arguments_delta='{"path": "',
        )
    )
    renderer.render(
        _event("model.said", text="Partial completion", part=0, final=False)
    )
    renderer.finish()
    assert '{"path": "' not in output.getvalue()
    assert "Partial completion" in output.getvalue()
    before = output.getvalue()
    renderer.finish()
    assert output.getvalue() == before


def test_interrupted_final_answer_chunk_is_labeled_incomplete() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output, plain=True)
    renderer.render(
        _event(
            "model.finished",
            answer="The answer stopped before the final chunk.",
            message_id="unfinished-answer",
            part=0,
            final=False,
        )
    )
    renderer.finish()

    text = output.getvalue()
    assert "Partial response" in text
    assert "The answer stopped before the final chunk." in text
    assert "\nLoupe\n" not in text


def test_interrupted_final_chunk_marks_already_streamed_text_incomplete() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output, plain=True)
    renderer.render(
        _event(
            "model.text.delta",
            turn_id="same-turn",
            block_id="body",
            text="The partial answer",
        )
    )
    renderer.render(
        _event(
            "model.finished",
            turn_id="same-turn",
            answer="The partial",
            message_id="unfinished-answer",
            part=0,
            final=False,
        )
    )
    renderer.finish()

    text = output.getvalue()
    assert text.count("The partial answer") == 1
    assert "Partial response (stream interrupted)" in text


@pytest.mark.parametrize(
    ("kind", "field"), [("model.said", "text"), ("model.finished", "summary")]
)
@pytest.mark.parametrize(
    ("streamed", "partial", "expected"),
    [
        ("Already streamed answer.", "Already streamed", "Already streamed answer."),
        (
            "Already streamed answer.",
            "Already streamed answer.",
            "Already streamed answer.",
        ),
        ("Answer", "Answer completed.", "Answer completed."),
    ],
)
def test_interrupted_authoritative_chunk_does_not_repeat_streamed_answer(
    kind: str, field: str, streamed: str, partial: str, expected: str
) -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(_event("model.text.delta", text=streamed))
    renderer.render(_event(kind, **{field: partial}, part=0, final=False))
    renderer.finish()
    assert output.getvalue().count(expected) == 1
    shared_prefix = min((streamed, partial), key=len)
    assert output.getvalue().count(shared_prefix) == 1
    before = output.getvalue()
    renderer.finish()
    assert output.getvalue() == before


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        ("model.reasoning", "text"),
        ("model.tool_call", "arguments_text"),
        ("model.tool_result", "content"),
    ],
)
def test_interrupted_hidden_chunks_do_not_leak_when_renderer_finishes(
    kind: str, field: str
) -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(
        _event(
            kind,
            **{field: "INTERNAL PAYLOAD MUST STAY HIDDEN"},
            call_id="pending",
            tool="read_file",
            part=0,
            final=False,
        )
    )
    renderer.finish()
    renderer.render(_event("model.turn.started", turn_id="next"))
    renderer.render(_event("model.said", text="Here is the useful answer."))
    renderer.finish()
    assert "INTERNAL PAYLOAD" not in output.getvalue()
    assert "Partial reasoning" not in output.getvalue()
    assert "Partial tool" not in output.getvalue()
    assert output.getvalue().count("Here is the useful answer.") == 1


@pytest.mark.parametrize("kind", ["model.tool_result", "tool.result", "tool.completed"])
def test_completion_corrections_stay_in_activity_until_answer_arrives(
    kind: str,
) -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(
        _event(
            kind,
            tool="finish_task",
            is_error=True,
            content="provide the complete user-facing answer in 'answer'",
        )
    )
    renderer.render(_event("model.finished", answer="The detailed findings."))
    renderer.finish()
    text = output.getvalue()
    assert "finish_task" not in text
    assert "provide the complete user-facing answer" not in text
    assert text.count("The detailed findings.") == 1


@pytest.mark.parametrize("kind", ["model.tool_result", "tool.result", "tool.completed"])
def test_tool_errors_keep_one_bounded_line_without_dumping_output(kind: str) -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(
        _event(
            kind,
            tool="read_file",
            is_error=True,
            content="\n\nPermission denied: " + "x" * 400 + "\nUNNEEDED TRACEBACK",
        )
    )
    renderer.finish()
    lines = [line for line in output.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    assert "✗ read_file: Permission denied:" in lines[0]
    assert len(lines[0]) <= 220
    assert "UNNEEDED TRACEBACK" not in output.getvalue()


def test_chunked_tool_error_is_reported_once_after_assembly() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    event = _event(
        "model.tool_result",
        tool="read_file",
        call_id="error",
        is_error=True,
        content="\nFile is ",
        part=0,
        final=False,
    )
    renderer.render(event)
    assert "✗" not in output.getvalue()
    renderer.render(
        _event(
            "model.tool_result",
            tool="read_file",
            call_id="error",
            is_error=True,
            content="missing\nextra debugging details",
            part=1,
            final=True,
        )
    )
    renderer.finish()
    assert output.getvalue().count("✗ read_file: File is missing") == 1
    assert "extra debugging details" not in output.getvalue()
    # Rendering must not modify the durable event payload supplied by the caller.
    assert event["payload"]["content"] == "\nFile is "


def test_interrupted_chunked_tool_error_keeps_a_short_failure_on_finish() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(
        _event(
            "model.tool_result",
            tool="read_file",
            call_id="interrupted-error",
            is_error=True,
            content="\nPermission denied\nTRACE DETAILS\n" * 500,
            part=0,
            final=False,
        )
    )
    renderer.finish()
    renderer.finish()
    assert output.getvalue().count("✗ read_file: Permission denied") == 1
    assert "TRACE DETAILS" not in output.getvalue()


@pytest.mark.parametrize("state", ["passed", "failed", "timed_out", "source_mutated"])
def test_check_logs_stay_hidden_but_check_outcome_is_visible(state: str) -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(_event("check.started", run_id="check", name="unit tests"))
    renderer.render(
        _event("check.output", run_id="check", text="VERBOSE CHECK LOG\n" * 500)
    )
    renderer.render(
        _event(
            "check.finished",
            run_id="check",
            name="unit tests",
            state=state,
            exit_code=0 if state == "passed" else 1,
        )
    )
    renderer.finish()
    text = output.getvalue()
    assert "VERBOSE CHECK LOG" not in text
    if state == "passed":
        assert "Checks passed" in text
    else:
        assert "unit tests" in text and state in text
        assert "/checks" in text


@pytest.mark.parametrize(
    "control",
    [
        "\x1b[2J",
        "\x1b]0;fake title\x07",
        "\x1b]52;c;clipboard\x1b\\",
        "\x1bPdevice command\x1b\\",
        "\x9b2J",
        "\x9d0;fake title\x9c",
        "\r\x08\x00\x7f\u202e",
    ],
)
def test_control_sequences_are_removed_even_across_delta_boundaries(
    control: str,
) -> None:
    for split in range(len(control) + 1):
        sanitizer = TextSanitizer()
        first = sanitizer.feed("visible " + control[:split])
        second = sanitizer.feed(control[split:] + "response\n\tline")
        assert first + second == "visible response\n\tline"


def test_model_text_and_visible_tool_errors_cannot_emit_control_sequences() -> None:
    output = io.StringIO()
    renderer = EventRenderer(output)
    renderer.render(_event("model.text.delta", text="Hello \x1b]52;"))
    renderer.render(_event("model.text.delta", text="c;malicious\x07there"))
    renderer.render(_event("model.said", text="Hello \x1b]52;c;malicious\x07there"))
    renderer.render(
        _event(
            "model.tool_result",
            tool="read_file",
            content="\x1b[2JTool error\r\x08",
            is_error=True,
        )
    )
    renderer.finish()
    assert "Hello there" in output.getvalue()
    assert "Tool error" in output.getvalue()
    assert "malicious" not in output.getvalue()
    assert "\x1b" not in output.getvalue()
    assert "\r" not in output.getvalue()


def test_stateless_text_and_questions_are_not_truncated() -> None:
    text = "A useful long response\n" * 300
    assert render_event(_event("model.said", text=text)) == text
    assert question_text(_event("question.asked", question=text)) == text
    assert safe_text("Emoji 👩‍💻 and Unicode 漢字") == "Emoji 👩‍💻 and Unicode 漢字"


class _TerminalStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_narrow_terminal_keeps_all_code_and_long_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "28")
    monkeypatch.setenv("NO_COLOR", "1")
    output = _TerminalStream()
    ui = TerminalUI(output)
    code = "abcdefghijklmnopqrstuvwxyz" * 8
    ui.code(code, language="python")
    ui.body("```python\n" + code + "\n```", markdown=True)
    compact = "".join(safe_text(output.getvalue()).split())
    assert compact.count(code) == 2
    assert "…" not in output.getvalue()
    assert "\x1b" not in output.getvalue()


def test_banner_has_brand_commands_and_whole_repository_scope() -> None:
    output = io.StringIO()
    ui = TerminalUI(output)
    ui.banner(
        repository="/project",
        scopes=["*"],
        provider="openai",
        model="example",
        session_id="session_a",
    )
    ui.help()
    text = output.getvalue()
    assert "Loupe" in text
    assert "whole repository" in text
    assert "/attach [TASK_ID]" in text
    assert "/history" in text
    assert "\x1b" not in text


def test_wide_markdown_table_does_not_ellipsize_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    output = _TerminalStream()
    ui = TerminalUI(output)
    ui.console.width = 80
    cells = [f"column_{index}_with_unique_tail_{index}" for index in range(10)]
    table = (
        " | ".join(cells) + "\n" + " | ".join(["---"] * 10) + "\n" + " | ".join(cells)
    )
    ui.body(table, markdown=True)
    compact = "".join(safe_text(output.getvalue()).split())
    assert all(compact.count(cell) == 2 for cell in cells)
    assert "…" not in output.getvalue()


def test_context_and_instruction_events_render_as_short_notices() -> None:
    assert (
        render_event(
            _event("model.instructions.loaded", paths=["AGENTS.md", "src/AGENTS.md"])
        )
        == "    using repository instructions (AGENTS.md, src/AGENTS.md)"
    )
    assert (
        render_event(_event("model.context.compacted", context_tokens=182_400))
        == "    summarized earlier conversation to stay in context"
        " (was about 182k tokens)"
    )
    assert (
        render_event(
            _event("model.context.compaction_failed", reason="provider offline")
        )
        == "  ! could not summarize earlier conversation (provider offline)"
    )
