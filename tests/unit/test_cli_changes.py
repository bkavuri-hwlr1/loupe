"""Publication notices must survive the terminal's event filtering and cursor."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, cast

import pytest

from llm_cli.build import code_identity
from llm_cli.cli.render import checkout_event_text, render_event
from llm_cli.cli.session import run_session
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient


def test_success_without_file_changes_is_not_reported_as_a_publication() -> None:
    assert (
        render_event(
            {"event_type": "execution.published", "payload": {"outcome": "no_changes"}}
        )
        is None
    )
    assert (
        render_event(
            {"event_type": "execution.published", "payload": {"workspace_revision": 5}}
        )
        == "  ✓ published"
    )


@pytest.mark.parametrize(
    ("kind", "paths"),
    [
        ("workspace.batch_published", {"paths": ["docs/guide.md"]}),
        ("workspace.change_published", {"path": "docs/guide.md"}),
    ],
)
def test_peer_publication_shows_paths_without_internal_identifiers(
    kind: str, paths: dict[str, Any]
) -> None:
    event = {
        "event_type": kind,
        "caused_by_session_id": "peer-session",
        "payload": {**paths, "workspace_revision": 4},
    }
    assert checkout_event_text(event, own_session_id="reader-session") == (
        "  Another session updated docs/guide.md."
    )
    assert checkout_event_text(event, own_session_id="peer-session") is None


def test_publication_notice_bounds_and_sanitizes_untrusted_paths() -> None:
    event = {
        "event_type": "workspace.batch_published",
        "caused_by_session_id": "peer\x1b\n-session",
        "payload": {
            "paths": ["docs/\x1b\n" + "x" * 1000] * 5,
            "workspace_revision": "\x1b[2J",
        },
    }
    line = checkout_event_text(event, own_session_id="reader-session")
    assert line is not None
    assert "\x1b" not in line and "\n" not in line
    assert len(line) < 1100
    assert "…" in line
    assert "revision" not in line


@pytest.mark.parametrize("kind", ["intent.set", "intent.cleared", "session.opened"])
@pytest.mark.parametrize("source", ["own", "peer"])
def test_routine_session_events_stay_out_of_the_conversation(
    kind: str, source: str
) -> None:
    assert (
        checkout_event_text(
            {
                "event_type": kind,
                "caused_by_session_id": source,
                "payload": {"paths": ["*"]},
            },
            own_session_id="own",
        )
        is None
    )


class _EventClient:
    def __init__(self, root: Path, events: list[dict[str, Any]]) -> None:
        self.paths = AppPaths.resolve(
            "test",
            environ={
                "LLM_COORD_CONFIG_HOME": str(root / "config"),
                "LLM_COORD_DATA_HOME": str(root / "data"),
                "LLM_COORD_STATE_HOME": str(root / "state"),
                "LLM_COORD_RUNTIME_DIR": str(root / "run"),
            },
            home=root,
        )
        self.events = events
        self.acknowledged: list[int] = []
        self.requests: list[int] = []
        self.session_id = ""

    def call(
        self, method: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        del kwargs
        params = params or {}
        if method == "system.ping":
            return {"code_fingerprint": code_identity()["fingerprint"]}
        if method == "task.list":
            return []
        if method == "session.open":
            self.session_id = params["session_id"]
            return {
                "session": {
                    "provider": "test",
                    "model": "test-model",
                    "agent_mode": params.get("mode", "auto"),
                },
                "bootstrap_sequence": 1,
            }
        if method == "session.ack":
            self.acknowledged.append(params["sequence"])
            return {"cursor": {"transport_received_sequence": params["sequence"]}}
        if method == "session.events":
            self.requests.append(params["after"])
            return [
                event for event in self.events if event["sequence"] > params["after"]
            ][: params["limit"]]
        if method in {"repo.status", "session.set_intent", "session.close"}:
            return {}
        raise AssertionError(method)


@pytest.mark.parametrize("skipped_events", [0, 100])
def test_changes_command_displays_publication_once_then_reports_no_new_changes(
    tmp_path: Path, skipped_events: int
) -> None:
    events = [
        {"sequence": sequence, "event_type": "session.activated"}
        for sequence in range(2, skipped_events + 2)
    ]
    published_sequence = skipped_events + 2
    events.append(
        {
            "sequence": published_sequence,
            "event_type": "workspace.batch_published",
            "caused_by_session_id": "peer-session",
            "payload": {"paths": ["docs/guide.md"], "workspace_revision": 4},
        }
    )
    client = _EventClient(tmp_path, events)
    output = io.StringIO()
    assert (
        run_session(
            cast(DaemonClient, client),
            repository=tmp_path,
            scopes=["docs/"],
            provider="test",
            input_stream=io.StringIO("/changes\n/changes\n/exit\n"),
            output=output,
        )
        == 0
    )
    text = output.getvalue()
    assert text.count("Another session updated docs/guide.md.") == 1
    assert text.count("No new checkout changes.") == 1
    assert client.acknowledged == [1, published_sequence]
    assert client.requests[-1] == published_sequence


def test_changes_command_with_no_events_is_not_silent(tmp_path: Path) -> None:
    client = _EventClient(tmp_path, [])
    output = io.StringIO()
    run_session(
        cast(DaemonClient, client),
        repository=tmp_path,
        scopes=["docs/"],
        provider="test",
        input_stream=io.StringIO("/changes\n/exit\n"),
        output=output,
    )
    assert "No new checkout changes." in output.getvalue()
    assert client.acknowledged == [1]


def test_automatic_refresh_stays_quiet_when_no_events_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _EventClient(tmp_path, [])
    output = io.StringIO()
    monkeypatch.setattr("llm_cli.cli.session._run_one", lambda *args, **kwargs: None)
    run_session(
        cast(DaemonClient, client),
        repository=tmp_path,
        scopes=["docs/"],
        provider="test",
        input_stream=io.StringIO("Read docs/guide.md\n/exit\n"),
        output=output,
    )
    assert "No new checkout changes." not in output.getvalue()
