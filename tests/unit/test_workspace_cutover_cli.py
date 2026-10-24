"""Explicit cutover CLI boundaries, without migrating a user checkout."""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path
from typing import Any, cast

import pytest

from llm_cli.cli.app import build_parser, dispatch
from llm_cli.cli.exit_codes import EXIT_BY_ERROR
from llm_cli.cli.output import emit
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.protocol.capabilities import WORKSPACE_CAPABILITIES
from llm_cli.protocol.client import DaemonClient, _error_code


class _Client:
    def __init__(self, result: Any = None) -> None:
        self.result = result if result is not None else {"regime": "legacy"}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.paths = cast(Any, None)

    def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        self.calls.append((method, params))
        return self.result


@pytest.mark.parametrize("action", ["plan", "status"])
def test_cutover_observation_sends_only_the_selected_path(
    action: str,
    tmp_path: Path,
) -> None:
    client = _Client()
    args = build_parser().parse_args(
        ["--json", "workspace", "cutover", action, str(tmp_path)]
    )
    assert dispatch(args, cast(DaemonClient, client)) == {"regime": "legacy"}
    assert client.calls == [(f"workspace.cutover.{action}", {"path": str(tmp_path)})]


@pytest.mark.parametrize("action", ["apply", "resume"])
def test_cutover_mutation_requires_a_reviewed_checkpoint(action: str) -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["workspace", "cutover", action])
    assert caught.value.code == 2


def test_resume_requires_the_same_explicit_intent() -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(
            ["workspace", "cutover", "resume", "--checkpoint", "reviewed"]
        )
    assert caught.value.code == 2


@pytest.mark.parametrize("action", ["apply", "resume"])
def test_cutover_sends_exact_checkpoint_and_intent(
    action: str,
    tmp_path: Path,
) -> None:
    client = _Client({"state": "transition", "intent_id": "intent-original"})
    args = build_parser().parse_args(
        [
            "workspace",
            "cutover",
            action,
            str(tmp_path),
            "--checkpoint",
            "checkpoint-original",
            "--intent",
            "intent-original",
        ]
    )
    result = dispatch(args, cast(DaemonClient, client))
    assert result["state"] == "transition"
    params: dict[str, Any] = {
        "path": str(tmp_path),
        "checkpoint": "checkpoint-original",
        "intent_id": "intent-original",
    }
    if action == "apply":
        params["dispositions"] = {}
    assert client.calls == [(f"workspace.cutover.{action}", params)]


def test_apply_defaults_to_retaining_existing_work(tmp_path: Path) -> None:
    client = _Client()
    args = build_parser().parse_args(
        [
            "workspace",
            "cutover",
            "apply",
            str(tmp_path),
            "--checkpoint",
            "reviewed",
        ]
    )
    dispatch(args, cast(DaemonClient, client))
    assert client.calls[0][1] == {
        "path": str(tmp_path),
        "checkpoint": "reviewed",
        "dispositions": {},
    }


def test_legacy_flag_cannot_bypass_a_checkout_regime() -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(
            [
                "run",
                "change source",
                "--scope",
                "src/",
                "--legacy",
            ]
        )
    assert caught.value.code == 2


def test_export_is_an_explicit_new_private_metadata_file(tmp_path: Path) -> None:
    value = {
        "format": "loupe-cutover-history-v1",
        "metadata_only": True,
        "tasks": [{"task_id": "old-task", "state": "completed"}],
        "refs": ["refs/llm-coord/tasks/old-task"],
    }
    client = _Client(value)
    destination = tmp_path / "history.json"
    args = build_parser().parse_args(
        [
            "workspace",
            "cutover",
            "export",
            str(tmp_path),
            "--out",
            str(destination),
        ]
    )
    result = dispatch(args, cast(DaemonClient, client))
    assert result == {"exported_to": str(destination), "metadata_only": True}
    assert client.calls == [("workspace.cutover.export", {"path": str(tmp_path)})]
    assert json.loads(destination.read_text()) == value
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".loupe-cutover-*"))


@pytest.mark.parametrize("symlink", [False, True])
def test_export_never_overwrites_an_existing_path(
    symlink: bool,
    tmp_path: Path,
) -> None:
    original = tmp_path / "original.json"
    original.write_text("retained evidence")
    destination = tmp_path / "history.json"
    if symlink:
        destination.symlink_to(original)
    else:
        destination.write_text("existing export")
    client = _Client({"metadata_only": True})
    args = build_parser().parse_args(
        [
            "workspace",
            "cutover",
            "export",
            str(tmp_path),
            "--out",
            str(destination),
        ]
    )
    with pytest.raises(LlmCoordError) as caught:
        dispatch(args, cast(DaemonClient, client))
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert original.read_text() == "retained evidence"
    if not symlink:
        assert destination.read_text() == "existing export"
    assert not list(tmp_path.glob(".loupe-cutover-*"))


def test_export_refuses_a_nonobject_response_without_creating_a_file(
    tmp_path: Path,
) -> None:
    client = _Client(["not a metadata envelope"])
    destination = tmp_path / "history.json"
    args = build_parser().parse_args(
        [
            "workspace",
            "cutover",
            "export",
            str(tmp_path),
            "--out",
            str(destination),
        ]
    )
    with pytest.raises(LlmCoordError) as caught:
        dispatch(args, cast(DaemonClient, client))
    assert caught.value.code is ErrorCode.PROTOCOL_MISMATCH
    assert not destination.exists()


def test_cutover_capabilities_and_errors_are_additive() -> None:
    assert set(WORKSPACE_CAPABILITIES) == {
        "workspace.authority.maintenance.v1",
        "workspace.cutover.recovery.v1",
        "workspace.cutover.v1",
        "workspace.execution.claim_free.v1",
        "legacy.history.read.v1",
    }
    for code, exit_code in (
        (ErrorCode.CUTOVER_CHECKPOINT_STALE, 75),
        (ErrorCode.CUTOVER_BLOCKED, 75),
        (ErrorCode.EXECUTION_CONTRACT_UNSUPPORTED, 65),
    ):
        assert _error_code(code.value) is code
        assert EXIT_BY_ERROR[code] == exit_code


def test_claim_free_task_response_can_be_rendered_and_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = {"task": {"task_id": "workspace-task"}, "claim": None}
    client = _Client(value)
    followed: list[str] = []

    def watch(_client: object, task_id: str, *_args: object) -> object:
        followed.append(task_id)
        return value

    monkeypatch.setattr("llm_cli.cli.app._watch", watch)
    monkeypatch.setattr("llm_cli.cli.app.session_environment_params", lambda _p: {})
    args = build_parser().parse_args(
        [
            "run",
            "change source",
            "--scope",
            "src/",
            "--follow",
        ]
    )
    assert dispatch(args, cast(DaemonClient, client)) == value
    assert followed == ["workspace-task"]
    output = io.StringIO()
    emit(value, as_json=True, stream=output)
    assert json.loads(output.getvalue())["claim"] is None


@pytest.mark.parametrize("count", [0, 2, 65])
def test_preview_human_output_lists_every_action_without_raw_evidence(
    count: int,
    tmp_path: Path,
) -> None:
    value = {
        "regime": "legacy",
        "generation": 7,
        "checkpoint": "exact-reviewed-token",
        "ready": False,
        "mappings": [
            {
                "repo_key": f"repository-{i}",
                "target_ref": f"refs/heads/target-{i}",
                "integration_adapter": "git",
                "canonical_path": f"/checkout-{i}",
                "provenance": [{"provenance": "historical", "detail": "hidden"}],
            }
            for i in range(count)
        ],
        "dispositions": [
            {
                "kind": "claim",
                "id": f"old-claim-{i}",
                "state": "queued",
                "default": "retain_legacy",
                "allowed": ["cancel"],
            }
            for i in range(count)
        ],
        "blockers": [
            {"kind": "mapping", "id": f"mapping-{i}", "reason": f"ambiguous-clone-{i}"}
            for i in range(count)
        ],
        "tasks": [{"title": "never dump raw tasks"}],
        "executions": [{"summary": "never dump raw executions"}],
    }
    client = _Client(value)
    args = build_parser().parse_args(["workspace", "cutover", "plan", str(tmp_path)])
    text = dispatch(args, cast(DaemonClient, client))
    assert "read-only" in text and "irreversible" in text
    assert "Regime: legacy; generation: 7; ready: False" in text
    assert "Checkpoint: exact-reviewed-token" in text
    for group in ("Mappings", "Dispositions", "Blockers"):
        assert f"{group} ({count}):" in text
    for i in range(count):
        assert f"repository-{i}: target=refs/heads/target-{i}; adapter=git" in text
        assert f"path=/checkout-{i}; provenance=1 registration records" in text
        assert (
            f"claim old-claim-{i}: state=queued; default=retain_legacy; allowed=cancel"
            in text
        )
        assert f"mapping mapping-{i}: ambiguous-clone-{i}" in text
    assert "never dump raw" not in text and "detail" not in text
    assert "--json workspace cutover plan PATH for complete evidence" in text
    args.as_json = True
    assert dispatch(args, cast(DaemonClient, client)) == value


def test_preview_refuses_an_incomplete_action_inventory(tmp_path: Path) -> None:
    client = _Client({"mappings": [], "dispositions": [], "blockers": "truncated"})
    args = build_parser().parse_args(["workspace", "cutover", "plan", str(tmp_path)])
    with pytest.raises(LlmCoordError) as caught:
        dispatch(args, cast(DaemonClient, client))
    assert caught.value.code is ErrorCode.PROTOCOL_MISMATCH


def test_only_explicit_selected_claims_are_cancelled(tmp_path: Path) -> None:
    client = _Client()
    args = build_parser().parse_args(
        [
            "workspace",
            "cutover",
            "apply",
            str(tmp_path),
            "--checkpoint",
            "reviewed",
            "--cancel-claim",
            "queued-a",
            "--cancel-claim",
            "queued-b",
        ]
    )
    dispatch(args, cast(DaemonClient, client))
    assert client.calls[0][1]["dispositions"] == {
        "queued-a": "cancel",
        "queued-b": "cancel",
    }


def test_sessionless_coding_forwards_only_the_filtered_terminal_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client({"task": {"task_id": "workspace-task"}, "claim": None})
    captured: list[object] = []

    def environment(paths: object) -> dict[str, object]:
        captured.append(paths)
        return {"command_environment": {"PROJECT_FLAG": "enabled"}}

    monkeypatch.setattr("llm_cli.cli.app.session_environment_params", environment)
    args = build_parser().parse_args(["run", "change source", "--scope", "src/"])
    dispatch(args, cast(DaemonClient, client))
    assert captured == [client.paths]
    assert client.calls[0][1]["command_environment"] == {"PROJECT_FLAG": "enabled"}


@pytest.mark.parametrize(
    "legacy_flags",
    [
        ["--claim-only"],
        ["--fixture-write", "docs/a.md=fixture"],
    ],
)
def test_explicit_legacy_work_does_not_capture_a_terminal_environment(
    legacy_flags: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()

    def forbidden(_paths: object) -> dict[str, object]:
        raise AssertionError("legacy work must not capture the terminal")

    monkeypatch.setattr("llm_cli.cli.app.session_environment_params", forbidden)
    args = build_parser().parse_args(
        [
            "run",
            "legacy fixture",
            "--scope",
            "docs/",
            *legacy_flags,
        ]
    )
    dispatch(args, cast(DaemonClient, client))
    assert "command_environment" not in client.calls[0][1]


@pytest.mark.parametrize(
    "argv",
    [["claim", "--help"], ["claim", "release", "--help"], ["claim", "renew", "--help"]],
)
def test_legacy_claim_help_names_deprecation_and_explicit_migration(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(argv)
    assert caught.value.code == 0
    text = capsys.readouterr().out
    assert "deprecated for one compatibility release" in text
    assert "require a legacy checkout" in text
    assert "loupe workspace cutover plan PATH" in text


@pytest.mark.parametrize(
    "argv",
    [
        ["claim", "release", "claim-old", "--reason", "cancelled"],
        ["claim", "renew", "claim-old", "--task", "task-old", "--fence", "7"],
    ],
)
@pytest.mark.parametrize("as_json", [False, True])
def test_successful_legacy_claim_mutation_explains_migration_in_human_output(
    argv: list[str], as_json: bool
) -> None:
    value = {"claim_id": "claim-old", "state": "released"}
    client = _Client(value)
    args = build_parser().parse_args((["--json"] if as_json else []) + argv)
    result = dispatch(args, cast(DaemonClient, client))
    if as_json:
        assert result == value
    else:
        rendered = io.StringIO()
        emit(result, as_json=False, plain=True, stream=rendered)
        assert "deprecated for one compatibility release" in rendered.getvalue()
        assert "loupe workspace cutover plan PATH" in rendered.getvalue()
    assert "notice" not in value
