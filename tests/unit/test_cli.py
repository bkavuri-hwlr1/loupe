from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from llm_cli import __version__
from llm_cli.cli.app import build_parser, dispatch
from llm_cli.cli.session import open_or_resume_session
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.client import DaemonClient


def test_version_is_available_without_daemon(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_run_requires_explicit_scope() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(["run", "change parser"])
    assert caught.value.code == 2


def test_run_keeps_multiple_explicit_scopes() -> None:
    arguments = build_parser().parse_args(
        ["run", "change parser", "--scope", "src/", "--scope", "tests/"]
    )
    assert arguments.scope == ["src/", "tests/"]


def test_run_accepts_a_provider_and_model() -> None:
    arguments = build_parser().parse_args(
        [
            "run",
            "change parser",
            "--scope",
            "src/",
            "--provider",
            "example",
            "--model",
            "example-code",
        ]
    )
    assert arguments.provider == "example"
    assert arguments.model == "example-code"


def test_run_keeps_fixture_write_values() -> None:
    arguments = build_parser().parse_args(
        [
            "run",
            "write documentation",
            "--scope",
            "docs/",
            "--fixture-write",
            "docs/guide.md=hello",
            "--fixture-write",
            "docs/next.md=world",
        ]
    )
    assert arguments.fixture_write == ["docs/guide.md=hello", "docs/next.md=world"]


def test_task_events_accepts_an_event_cursor() -> None:
    arguments = build_parser().parse_args(
        ["task", "events", "task_123", "--after", "8", "--limit", "25"]
    )
    assert arguments.task_id == "task_123"
    assert arguments.after == 8
    assert arguments.limit == 25


class _WorkspaceClient:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(self, method: str, params: dict[str, object], **kwargs: object) -> object:
        del kwargs
        self.calls.append((method, params))
        if method == "workspace.read_file":
            return {
                "identity": {
                    "kind": "regular",
                    "mode": "100644",
                    "digest": "a" * 64,
                    "size": 5,
                }
            }
        if method == "workspace.stage_file":
            return {"candidate": {"candidate_id": params["candidate_id"]}}
        raise AssertionError(method)


def test_workspace_stage_reads_a_base_then_sends_one_private_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.resolve(
        "test",
        environ={
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home=tmp_path,
    )
    content = tmp_path / "candidate.txt"
    content.write_text("replacement\n", encoding="utf-8")
    client = _WorkspaceClient(paths)
    monkeypatch.setattr(
        "llm_cli.cli.app.session_resume_secret", lambda _paths, _session: "secret"
    )
    arguments = build_parser().parse_args(
        [
            "workspace",
            "stage",
            "session_1",
            "docs/guide.md",
            "--content-file",
            str(content),
            "--candidate-id",
            "candidate_1",
        ]
    )

    result = dispatch(arguments, cast(DaemonClient, client))

    assert result == {"candidate": {"candidate_id": "candidate_1"}}
    assert client.calls == [
        (
            "workspace.read_file",
            {
                "session_id": "session_1",
                "resume_secret": "secret",
                "relative_path": "docs/guide.md",
            },
        ),
        (
            "workspace.stage_file",
            {
                "session_id": "session_1",
                "resume_secret": "secret",
                "candidate_id": "candidate_1",
                "relative_path": "docs/guide.md",
                "base": {
                    "kind": "regular",
                    "mode": "100644",
                    "digest": "a" * 64,
                    "size": 5,
                },
                "content": "replacement\n",
            },
        ),
    ]


class _RefusingClient:
    """A client whose session.open always fails with one error code."""

    def __init__(self, paths: AppPaths, code: ErrorCode) -> None:
        self.paths = paths
        self._code = code

    def call(self, method: str, params: object = None, **kwargs: object) -> object:
        del method, params, kwargs
        raise LlmCoordError(self._code, "refused")


def test_a_validated_refusal_does_not_advertise_a_session_to_resume(
    tmp_path: Path,
) -> None:
    paths = AppPaths.resolve(
        "test",
        environ={
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home=tmp_path,
    )
    paths.ensure()
    client = cast(DaemonClient, _RefusingClient(paths, ErrorCode.CONFIG_INVALID))

    with pytest.raises(LlmCoordError) as failure:
        open_or_resume_session(
            client,
            repository=tmp_path,
            provider=None,
            model=None,
            resume_session_id=None,
        )

    # Nothing was created, so pointing the operator at --resume would send them
    # after a session that does not exist, and the secret must not linger.
    assert "--resume" not in failure.value.message
    assert failure.value.message == "refused"
    sessions = paths.state_dir / "sessions"
    assert not sessions.exists() or not list(sessions.glob("*"))


def test_an_indeterminate_failure_still_advertises_the_resume_path(
    tmp_path: Path,
) -> None:
    paths = AppPaths.resolve(
        "test",
        environ={
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home=tmp_path,
    )
    paths.ensure()
    client = cast(DaemonClient, _RefusingClient(paths, ErrorCode.INTERNAL_RECOVERABLE))

    with pytest.raises(LlmCoordError) as failure:
        open_or_resume_session(
            client,
            repository=tmp_path,
            provider=None,
            model=None,
            resume_session_id=None,
        )

    # The daemon may have committed the open before the response was lost, so
    # the only resume secret must be kept and named.
    assert "--resume" in failure.value.message
