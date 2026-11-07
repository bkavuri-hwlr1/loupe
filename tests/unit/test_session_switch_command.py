"""The standalone workspace switch validates before contacting the daemon."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from llm_cli.cli import app, session
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths


class SwitchDaemon:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.target = "isolated"
        self.failure: LlmCoordError | None = None
        self.confirmed_generation: int | None = 2
        self.prepared_state: Any = "confirming"
        self.prepared_sequence: Any = 7
        self.confirmed_mode: Any = None
        self.rollback_state = "rolled_back"

    def call(self, method: str, params: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append((method, params, kwargs))
        if self.failure:
            raise self.failure
        if method == "workspace.switch_mode":
            self.target = params["workspace"]
            return {
                "switch_intent_id": params["switch_intent_id"],
                "state": self.prepared_state,
                "workspace": {"mode": self.target},
                "bootstrap_sequence": self.prepared_sequence,
                "classification": "unpublished_source",
                "runtime_summary": "Runtime remains private.",
                "proposal_task_id": "held-source",
                "export_path": "/private/local.patch",
            }
        if method == "workspace.confirm_switch":
            current: dict[str, Any] = {
                "workspace_mode": self.target
                if self.confirmed_mode is None
                else self.confirmed_mode
            }
            if self.confirmed_generation is not None:
                current["workspace_binding_generation"] = self.confirmed_generation
            return {
                "switch_intent_id": params["switch_intent_id"],
                "state": "confirmed",
                "session": current,
                "workspace": {"mode": self.target},
                "classification": "unpublished_source",
                "runtime_summary": "Runtime remains private.",
                "proposal_task_id": "held-source",
                "export_path": "/private/local.patch",
            }
        if method == "workspace.rollback_switch":
            return {
                "state": self.rollback_state,
                "switch_intent_id": params["switch_intent_id"],
            }
        raise AssertionError(method)


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SwitchDaemon:
    paths = AppPaths.resolve(
        "switch-test",
        home=tmp_path,
        environ={
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
        },
    )
    paths.ensure()
    secret = paths.session_secret_file("saved")
    secret.write_text("local-only-test-secret")
    secret.chmod(0o600)
    value = SwitchDaemon(paths)
    monkeypatch.setattr(app.AppPaths, "resolve", lambda *args, **kwargs: paths)
    monkeypatch.setattr(app, "DaemonClient", lambda paths: value)
    return value


def run(*arguments: str) -> None:
    app.main(["--json", "session", "switch", "saved", *arguments])


@pytest.mark.parametrize(
    "disposition", ["publish", "hold", "export", "retain", "discard"]
)
def test_each_shared_disposition_confirms_and_reports_classification(
    daemon: SwitchDaemon,
    capsys: pytest.CaptureFixture[str],
    disposition: str,
) -> None:
    run("--to", "shared", "--" + disposition, "--switch-intent-id", "stable-intent")
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "confirmed"
    assert result["classification"] == "unpublished_source"
    assert result["runtime_summary"] == "Runtime remains private."
    assert result["proposal_task_id"] == "held-source"
    assert result["export_path"] == "/private/local.patch"
    assert [call[0] for call in daemon.calls] == [
        "workspace.switch_mode",
        "workspace.confirm_switch",
    ]
    request = daemon.calls[0]
    assert request[1]["disposition"] == disposition
    assert request[1]["resume_secret"] == "local-only-test-secret"
    assert request[2]["idempotency_key"] == "stable-intent"
    assert daemon.calls[1][1]["sequence"] == 7
    assert "resume_secret" not in repr(result)


def test_isolated_consent_is_exact_and_deduplicated(daemon: SwitchDaemon) -> None:
    run("--to", "isolated", "--approve-secret", ".env", "--approve-secret", ".env")
    request = daemon.calls[0][1]
    assert request["approved_runtime_files"] == [".env"]
    assert request["disposition"] is None
    assert request["switch_intent_id"].startswith("switch_")


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("--to", "shared"),
        ("--to", "shared", "--unknown"),
        ("--to", "shared", "--hold", "--retain"),
        ("--to", "isolated", "--discard"),
        ("--to", "shared", "--retain", "--approve-secret", ".env"),
        ("--to", "isolated", "--approve-secret", "../outside"),
        ("--to", "isolated", "--approve-secret", "/outside"),
        ("--to", "isolated", "--sequence", "7"),
        ("--confirm", "pending"),
        ("--confirm", "pending", "--sequence", "-1"),
        ("--confirm", "pending", "--sequence", "7", "--discard"),
        ("--confirm", "pending", "--sequence", "7", "--approve-secret", ".env"),
        ("--confirm", "pending", "--sequence", "7", "--switch-intent-id", "new"),
        ("--rollback", "pending", "--sequence", "7"),
        ("--rollback", "pending", "--retain"),
        ("--to", "isolated", "--switch-intent-id", ""),
        ("--to", "isolated", "--switch-intent-id", "x" * 129),
    ],
)
def test_invalid_requests_never_send_any_rpc(
    daemon: SwitchDaemon, arguments: tuple[str, ...]
) -> None:
    with pytest.raises(SystemExit) as caught:
        run(*arguments)
    assert caught.value.code == 2
    assert daemon.calls == []


def test_pending_switch_confirmation_acknowledges_exact_bootstrap(
    daemon: SwitchDaemon,
) -> None:
    run("--confirm", "pending", "--sequence", "42")
    assert len(daemon.calls) == 1
    method, params, _ = daemon.calls[0]
    assert method == "workspace.confirm_switch"
    assert params["switch_intent_id"] == "pending"
    assert params["sequence"] == 42
    assert params["resume_secret"] == "local-only-test-secret"


def test_pending_switch_rollback_never_confirms(
    daemon: SwitchDaemon, capsys: pytest.CaptureFixture[str]
) -> None:
    run("--rollback", "pending")
    assert json.loads(capsys.readouterr().out)["state"] == "rolled_back"
    assert [call[0] for call in daemon.calls] == ["workspace.rollback_switch"]


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.TARGET_MOVED,
        ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE,
        ErrorCode.GIT_OPERATION_ACTIVE,
    ],
)
def test_retryable_rpc_refusals_keep_the_usual_exit_75(
    daemon: SwitchDaemon,
    capsys: pytest.CaptureFixture[str],
    code: ErrorCode,
) -> None:
    daemon.failure = LlmCoordError(code, "try again after current work finishes")
    with pytest.raises(SystemExit) as caught:
        run("--to", "isolated", "--switch-intent-id", "retry")
    assert caught.value.code == 75
    assert json.loads(capsys.readouterr().out)["error"]["code"] == code.value
    assert len(daemon.calls) == 1


def test_incomplete_confirmation_cannot_claim_success(daemon: SwitchDaemon) -> None:
    daemon.confirmed_generation = None
    with pytest.raises(SystemExit) as caught:
        run("--to", "isolated")
    assert caught.value.code == 76


def test_failed_request_names_exact_cli_recovery(
    daemon: SwitchDaemon, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon.failure = LlmCoordError(ErrorCode.DAEMON_UNAVAILABLE, "response interrupted")
    with pytest.raises(SystemExit) as caught:
        run("--to", "isolated", "--switch-intent-id", "uncertain")
    assert caught.value.code == 69
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["details"]["switch_intent_id"] == "uncertain"
    assert "session switch saved --rollback uncertain" in error["message"]


def test_publish_retains_existing_long_check_window(
    daemon: SwitchDaemon,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(
        session, "_patient", lambda value, timeout: waits.append(timeout) or value
    )
    run("--to", "shared", "--publish")
    assert session.APPLY_TIMEOUT_SECONDS in waits


@pytest.mark.parametrize("sequence", [True, None, "7", -1])
def test_malformed_bootstrap_never_sends_confirmation(
    daemon: SwitchDaemon,
    sequence: Any,
) -> None:
    daemon.prepared_sequence = sequence
    with pytest.raises(SystemExit) as caught:
        run("--to", "isolated")
    assert caught.value.code == 76
    assert [call[0] for call in daemon.calls] == ["workspace.switch_mode"]


@pytest.mark.parametrize("mode", ["other", [], {}])
def test_malformed_confirmation_is_a_protocol_error(
    daemon: SwitchDaemon,
    mode: Any,
) -> None:
    daemon.confirmed_mode = mode
    with pytest.raises(SystemExit) as caught:
        run("--to", "isolated")
    assert caught.value.code == 76


def test_rollback_pending_is_reported_without_claiming_completion(
    daemon: SwitchDaemon,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon.rollback_state = "rollback_pending"
    run("--rollback", "running-check")
    assert json.loads(capsys.readouterr().out)["state"] == "rollback_pending"


def test_plain_output_shows_source_classification_and_retained_work(
    daemon: SwitchDaemon,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app.main(["--plain", "session", "switch", "saved", "--to", "shared", "--hold"])
    output = capsys.readouterr().out
    assert "classification: unpublished_source" in output
    assert "proposal_task_id: held-source" in output
    assert "local-only-test-secret" not in output


def test_refused_new_request_does_not_recommend_rolling_back_the_wrong_intent(
    daemon: SwitchDaemon,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon.failure = LlmCoordError(
        ErrorCode.CONFIG_INVALID, "workspace switch earlier-intent needs confirmation"
    )
    with pytest.raises(SystemExit) as caught:
        run("--to", "isolated", "--switch-intent-id", "not-allocated")
    assert caught.value.code == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert "session resume saved" in error["message"]
    assert "--rollback not-allocated" not in error["message"]
    assert error["details"]["requested_switch_intent_id"] == "not-allocated"
