"""Source export CLI sends only explicit frozen-source metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from llm_cli.cli import app
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths


class ExportClient:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.error: LlmCoordError | None = None

    def call(self, method: str, params: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append((method, params, kwargs))
        if self.error:
            raise self.error
        return {
            "state": "exported",
            "export_intent_id": params["export_intent_id"],
            "files": 2,
        }


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ExportClient:
    paths = AppPaths.resolve(
        "test",
        home=tmp_path,
        environ={
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
        },
    )
    value = ExportClient(paths)
    monkeypatch.setattr(app.AppPaths, "resolve", lambda *args, **kwargs: paths)
    monkeypatch.setattr(app, "DaemonClient", lambda paths: value)
    return value


@pytest.mark.parametrize("kind", [None, "proposal", "candidate", "batch"])
@pytest.mark.parametrize("format", ["patch", "bundle"])
def test_each_source_and_format_sends_one_idempotent_rpc(
    client: ExportClient,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str | None,
    format: str,
) -> None:
    destination = tmp_path / "exported-artifact"
    arguments = [
        "--json",
        "change",
        "export",
        "source-id",
        "--format",
        format,
        "--to",
        str(destination),
        "--intent",
        "stable-export",
    ]
    if kind:
        arguments.extend(["--kind", kind])
    app.main(arguments)
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "state": "exported",
        "export_intent_id": "stable-export",
        "files": 2,
    }
    assert len(client.calls) == 1
    method, params, options = client.calls[0]
    assert method == "change.export"
    assert params == {
        "identifier": "source-id",
        "kind": kind,
        "format": format,
        "destination": str(destination),
        "export_intent_id": "stable-export",
        "into_checkout": False,
    }
    assert options["idempotency_key"] == "stable-export"


def test_destination_parent_alias_is_resolved_without_following_existing_leaf(
    client: ExportClient,
    tmp_path: Path,
) -> None:
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    app.main(
        [
            "--json",
            "change",
            "export",
            "source",
            "--to",
            str(alias / "patch"),
            "--into-checkout",
        ]
    )
    params = client.calls[0][1]
    assert params["destination"] == str(physical / "patch")
    assert params["into_checkout"] is True
    assert params["export_intent_id"].startswith("export_")


@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--format", "tar"],
        ["--kind", "live"],
        ["--intent", ""],
        ["--intent", "x" * 129],
    ],
)
def test_invalid_cli_options_do_not_contact_daemon(
    client: ExportClient,
    tmp_path: Path,
    flags: list[str],
) -> None:
    arguments = ["--json", "change", "export", "source"]
    if flags:
        arguments.extend(["--to", str(tmp_path / "out"), *flags])
    with pytest.raises(SystemExit) as caught:
        app.main(arguments)
    assert caught.value.code == 2
    assert client.calls == []


def test_missing_destination_parent_is_refused_before_rpc(
    client: ExportClient, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as caught:
        app.main(
            [
                "--json",
                "change",
                "export",
                "source",
                "--to",
                str(tmp_path / "missing" / "out"),
            ]
        )
    assert caught.value.code == 2
    assert not client.calls


def test_retryable_refusal_preserves_exit_code_and_exact_intent(
    client: ExportClient,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client.error = LlmCoordError(
        ErrorCode.CHECKOUT_OWNED_BY_OTHER_PROFILE, "checkout is busy"
    )
    with pytest.raises(SystemExit) as caught:
        app.main(
            [
                "--json",
                "change",
                "export",
                "source",
                "--to",
                str(tmp_path / "out"),
                "--intent",
                "retry-same",
            ]
        )
    assert caught.value.code == 75
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["details"]["export_intent_id"] == "retry-same"
    assert "--intent retry-same" in error["message"]
