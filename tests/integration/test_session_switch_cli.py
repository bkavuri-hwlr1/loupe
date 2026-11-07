"""Real CLI workspace switches preserve C13's disposition and recovery barriers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from test_cli_process_orchestration import _Cluster
from test_cli_process_orchestration import cluster as cluster


def _cli(
    cluster: _Cluster, name: str, arguments: list[str], *, exit_code: int = 0
) -> dict[str, Any]:
    child = cluster._spawn(
        name, ["-m", "llm_cli", "--profile", "test", "--json", *arguments]
    )
    assert child.wait(timeout=60) == exit_code, cluster.diagnostics()
    return json.loads((cluster.root / (name + ".log")).read_text())


def _open(cluster: _Cluster) -> dict[str, str]:
    session_id, secret = "switch-cli-session", "switch-cli-local-resume-secret"
    cluster.rpc("repo.add", path=str(cluster.repository))
    opened = cluster.rpc(
        "session.open",
        path=str(cluster.repository),
        session_id=session_id,
        resume_token_hash=hashlib.sha256(secret.encode()).hexdigest(),
        provider="process-test",
        model="scripted-no-model-calls",
        workspace="shared",
    )
    authenticated = {"session_id": session_id, "resume_secret": secret}
    cluster.rpc("session.ack", **authenticated, sequence=opened["bootstrap_sequence"])
    secret_file = cluster.paths.session_secret_file(session_id)
    secret_file.write_text(secret)
    secret_file.chmod(0o600)
    return authenticated


def _switch(
    cluster: _Cluster, name: str, session_id: str, *arguments: str
) -> dict[str, Any]:
    return _cli(cluster, name, ["session", "switch", session_id, *arguments])


@pytest.mark.parametrize(
    "disposition", ["publish", "hold", "export", "retain", "discard"]
)
def test_each_source_disposition_works_through_actual_cli(
    cluster: _Cluster, disposition: str
) -> None:
    authenticated = _open(cluster)
    session_id = authenticated["session_id"]
    isolated = _switch(cluster, "to-private", session_id, "--to", "isolated")
    assert isolated["state"] == "confirmed"
    assert isolated["session"]["workspace_binding_generation"] == 2
    private = Path(isolated["workspace"]["canonical_path"])
    (private / "docs/a.md").write_text("private CLI edit\n")
    args = ("--to", "shared", "--" + disposition, "--switch-intent-id", "to-shared")
    shared = _switch(cluster, "to-shared", session_id, *args)
    assert shared["state"] == "confirmed"
    assert shared["classification"] == "unpublished_source"
    assert shared["session"]["workspace_mode"] == "shared"
    assert shared["session"]["workspace_binding_generation"] == 3
    assert (cluster.repository / "docs/a.md").read_text() == (
        "private CLI edit\n" if disposition == "publish" else "base a\n"
    )
    repeated = _switch(cluster, "repeat-shared", session_id, *args)
    assert repeated["switch_intent_id"] == shared["switch_intent_id"]
    assert repeated["session"]["workspace_binding_generation"] == 3
    with closing(sqlite3.connect(cluster.paths.control_db)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM workspace_batches WHERE state='published'"
        ).fetchone()[0] == int(disposition == "publish")
        if disposition == "hold":
            row = connection.execute(
                "SELECT status FROM task_workflows WHERE task_id=?",
                (shared["proposal_task_id"],),
            ).fetchone()
            assert row[0] == "awaiting_review"
    if disposition == "export":
        assert "private CLI edit" in Path(shared["export_path"]).read_text()
        assert not Path(shared["export_path"]).stat().st_mode & 0o077
        assert (
            json.loads(Path(shared["export_artifact_path"]).read_text())["source"][0][
                "path"
            ]
            == "docs/a.md"
        )
    if disposition == "retain":
        assert (private / "docs/a.md").read_text() == "private CLI edit\n"
    assert (
        cluster.paths.session_secret_file(session_id).read_text()
        == authenticated["resume_secret"]
    )


@pytest.mark.parametrize("action", ["confirm", "rollback"])
def test_pending_switch_is_recovered_through_cli(
    cluster: _Cluster, action: str
) -> None:
    authenticated = _open(cluster)
    session_id = authenticated["session_id"]
    pending = cluster.rpc(
        "workspace.switch_mode",
        **authenticated,
        workspace="isolated",
        switch_intent_id="pending-cli-switch",
        disposition=None,
        approved_runtime_files=[],
    )
    assert pending["state"] == "confirming"
    refused_start = _cli(
        cluster,
        "already-pending",
        [
            "session",
            "switch",
            session_id,
            "--to",
            "isolated",
            "--switch-intent-id",
            "not-allocated",
        ],
        exit_code=2,
    )
    assert "session resume" in refused_start["error"]["message"]
    assert "--rollback not-allocated" not in refused_start["error"]["message"]
    resumed = _cli(cluster, "inspect-pending", ["session", "resume", session_id])
    assert (
        resumed["workspace_switch"]["bootstrap_sequence"]
        == pending["bootstrap_sequence"]
    )
    if action == "confirm":
        refused = _cli(
            cluster,
            "wrong-sequence",
            [
                "session",
                "switch",
                session_id,
                "--confirm",
                "pending-cli-switch",
                "--sequence",
                str(pending["bootstrap_sequence"] + 1),
            ],
            exit_code=2,
        )
        assert refused["error"]["code"] == "CONFIG_INVALID"
        assert (
            cluster.rpc("session.show", session_id=session_id)["session"][
                "workspace_mode"
            ]
            == "shared"
        )
    flags = ["--" + action, "pending-cli-switch"]
    if action == "confirm":
        flags.extend(["--sequence", str(pending["bootstrap_sequence"])])
    result = _switch(cluster, "recover-pending", session_id, *flags)
    assert result["state"] == ("confirmed" if action == "confirm" else "rolled_back")
    current = cluster.rpc("session.show", session_id=session_id)["session"]
    assert current["workspace_mode"] == (
        "isolated" if action == "confirm" else "shared"
    )
    assert current["workspace_binding_generation"] == (2 if action == "confirm" else 1)


@pytest.mark.parametrize("disposition", ["retain", "discard"])
def test_exact_runtime_consent_and_runtime_only_classification(
    cluster: _Cluster, disposition: str
) -> None:
    (cluster.repository / ".gitignore").write_text(".env\n")
    (cluster.repository / ".env").write_text("MARKER=initial\n")
    authenticated = _open(cluster)
    session_id = authenticated["session_id"]
    private = _switch(
        cluster,
        "runtime-private",
        session_id,
        "--to",
        "isolated",
        "--approve-secret",
        ".env",
    )
    root = Path(private["workspace"]["canonical_path"])
    assert (root / ".env").read_text() == "MARKER=initial\n"
    (root / ".env").write_text("MARKER=changed\n")
    result = _switch(
        cluster, "runtime-shared", session_id, "--to", "shared", "--" + disposition
    )
    assert result["classification"] == "runtime_only"
    assert "MARKER" not in repr(result)
    assert (cluster.repository / ".env").read_text() == "MARKER=initial\n"


def test_running_task_refuses_without_creating_switch_intent(cluster: _Cluster) -> None:
    authenticated = _open(cluster)
    session_id = authenticated["session_id"]
    chat = cluster.chat("busy-chat", resume=session_id)
    cluster.submit(chat, "busy-cli-switch", {"docs/a.md": "active task edit\n"})
    cluster.ready("busy-cli-switch")
    result = _cli(
        cluster,
        "refused-busy",
        [
            "session",
            "switch",
            session_id,
            "--to",
            "isolated",
            "--switch-intent-id",
            "must-not-start",
        ],
        exit_code=2,
    )
    assert result["error"]["code"] == "CONFIG_INVALID"
    assert "task" in result["error"]["message"]
    assert (
        cluster.rpc("session.show", session_id=session_id)["session"]["workspace_mode"]
        == "shared"
    )
    with closing(sqlite3.connect(cluster.paths.control_db)) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM mode_switch_intents "
                "WHERE switch_intent_id='must-not-start'"
            ).fetchone()[0]
            == 0
        )
