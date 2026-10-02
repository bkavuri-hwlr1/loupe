"""Answers stream while they are written, without committing unaccepted drafts."""

from __future__ import annotations

import io
import json
from typing import Any

from llm_cli.cli.render import EventRenderer
from llm_cli.cli.terminal import TerminalUI


def _event(kind: str, **payload: Any) -> dict[str, Any]:
    return {"event_type": kind, "task_id": "task-1", "payload": payload}


class PreviewSpy(TerminalUI):
    def __init__(self, stream: io.StringIO) -> None:
        super().__init__(stream, plain=True)
        self.previews: list[str | None] = []

    def preview(self, text: str | None) -> None:
        self.previews.append(text)


def _renderer() -> tuple[EventRenderer, io.StringIO, PreviewSpy]:
    output = io.StringIO()
    renderer = EventRenderer(output, plain=True)
    spy = PreviewSpy(output)
    renderer.ui = spy
    return renderer, output, spy


def _finish_deltas(call: str, arguments: dict[str, object], size: int = 9) -> list:
    encoded = json.dumps(arguments)
    return [
        _event(
            "model.tool.delta",
            tool="finish_task",
            call_id=call,
            turn_id="t-" + call,
            arguments_delta=encoded[start : start + size],
        )
        for start in range(0, len(encoded), size)
    ]


def _finish(renderer: EventRenderer, call: str, answer: str, **extra: object) -> None:
    """The durable sequence a provider produces for one finish_task call."""

    renderer.render(_event("model.turn.started", turn_id="t-" + call))
    arguments = {"answer": answer, **extra}
    for event in _finish_deltas(call, arguments):
        renderer.render(event)
    renderer.render(_event("model.turn.completed", turn_id="t-" + call))
    renderer.render(
        _event("model.tool_call", tool="finish_task", call_id=call, arguments=arguments)
    )
    renderer.render(_event("tool.called", tool="finish_task", call=1))


def test_read_only_answer_is_visible_before_it_finishes_and_printed_once() -> None:
    renderer, output, _ = _renderer()
    answer = 'Loupe is a "terminal" coding agent.\n\nIt coordinates sessions. 🦊'
    renderer.render(_event("model.turn.started", turn_id="t-a"))
    deltas = _finish_deltas("a", {"answer": answer, "outcome": "completed"})
    for event in deltas[: len(deltas) // 2]:
        renderer.render(event)
    assert 'Loupe is a "terminal"' in output.getvalue()
    for event in deltas[len(deltas) // 2 :]:
        renderer.render(event)
    renderer.render(_event("model.turn.completed", turn_id="t-a"))
    renderer.render(
        _event("model.tool_call", tool="finish_task", call_id="a", arguments={})
    )
    renderer.render(
        _event("model.tool_result", tool="finish_task", call_id="a", is_error=False)
    )
    renderer.render(_event("model.finished", answer=answer, outcome="completed"))
    renderer.render(_event("execution.published", outcome="no_changes"))
    renderer.finish()
    text = output.getvalue()
    assert text.count("It coordinates sessions. 🦊") == 1
    assert text.count("Loupe --") == 1
    assert '"answer"' not in text and "outcome" not in text


def test_answer_from_a_task_with_edits_is_previewed_until_accepted() -> None:
    renderer, output, spy = _renderer()
    renderer.render(
        _event(
            "model.tool_call",
            tool="write_file",
            call_id="w",
            arguments={"path": "a.py", "content": "x"},
        )
    )
    _finish(renderer, "a", "Updated a.py to return x.")
    assert "Updated a.py" not in output.getvalue()
    assert any(text and "Updated a.py" in text for text in spy.previews)
    renderer.render(_event("model.finished", answer="Updated a.py to return x."))
    renderer.finish()
    assert output.getvalue().count("Updated a.py to return x.") == 1
    assert spy.previews[-1] is None


def test_rejected_streamed_draft_is_marked_and_the_revision_is_labeled() -> None:
    renderer, output, _ = _renderer()
    _finish(renderer, "a", "First draft.")
    renderer.render(
        _event(
            "model.tool_result",
            tool="finish_task",
            call_id="a",
            is_error=True,
            content="answer exceeds the limit",
        )
    )
    _finish(renderer, "b", "Shorter final answer.")
    renderer.render(_event("model.finished", answer="Shorter final answer."))
    renderer.finish()
    text = output.getvalue()
    assert text.count("First draft.") == 1
    assert "Loupe is revising this answer" in text
    assert "Loupe — revised answer" in text
    assert text.count("Shorter final answer.") == 1
    assert "exceeds the limit" not in text


def test_outcome_known_only_after_streaming_is_still_reported() -> None:
    renderer, output, _ = _renderer()
    _finish(renderer, "a", "I could not finish the migration.", outcome="blocked")
    renderer.render(
        _event(
            "model.finished",
            answer="I could not finish the migration.",
            outcome="blocked",
        )
    )
    renderer.finish()
    text = output.getvalue()
    assert text.count("I could not finish the migration.") == 1
    assert "marked this answer as blocked" in text


def test_plain_text_preview_is_transient_and_the_answer_commits_once() -> None:
    renderer, output, spy = _renderer()
    renderer.render(_event("model.turn.started", turn_id="t"))
    for word in ("Narration ", "before tools."):
        renderer.render(_event("model.answer.preview", text=word, turn_id="t"))
    renderer.render(_event("model.turn.completed", turn_id="t"))
    assert "Narration" not in output.getvalue()
    assert "Narration before tools." in spy.previews
    assert spy.previews[-1] is None
    renderer.render(_event("model.turn.started", turn_id="u"))
    renderer.render(_event("model.answer.preview", text="The answer.", turn_id="u"))
    renderer.render(_event("model.turn.completed", turn_id="u"))
    renderer.render(_event("model.finished", answer="The answer."))
    renderer.finish()
    assert "Narration" not in output.getvalue()
    assert output.getvalue().count("The answer.") == 1


def test_recorded_codex_pattern_with_split_escapes_replays_identically() -> None:
    """Mirror a real run: the answer only ever exists inside finish_task."""

    chunks = [
        '{"',
        'answer":"This repo implements **Lou',
        "pe**. It uses ChatGPT/C",
        "odex credentials.\\",
        "n\\nIts distinguishing feature",
        ' is coordination.","out',
        'come":"completed"}',
    ]
    answer = json.loads("".join(chunks))["answer"]

    def replay(events: list[dict[str, Any]]) -> str:
        renderer, output, _ = _renderer()
        for event in events:
            renderer.render(event)
        renderer.finish()
        # Progress lines ("Thinking…") are transient status, not transcript.
        return "\n".join(
            line for line in output.getvalue().split("\n") if not line.endswith("…")
        )

    events = [
        _event("model.started", model="gpt-6-sol"),
        _event("model.turn.started", turn_id="t"),
        *(
            _event(
                "model.tool.delta",
                tool="finish_task",
                call_id="c",
                turn_id="t",
                arguments_delta=chunk,
            )
            for chunk in chunks
        ),
        _event("model.turn.completed", turn_id="t", stop_reason="tool_use"),
        _event("model.tool_call", tool="finish_task", call_id="c", arguments={}),
        _event("tool.called", tool="finish_task", call=1),
        _event("model.tool_result", tool="finish_task", call_id="c", is_error=False),
        _event("model.finished", answer=answer, outcome="completed"),
        _event("execution.published", outcome="no_changes"),
    ]
    live = replay(events)
    without_deltas = replay(
        [event for event in events if event["event_type"] != "model.tool.delta"]
    )
    assert live.count("Its distinguishing feature is coordination.") == 1
    assert "\\n" not in live
    assert live == without_deltas
