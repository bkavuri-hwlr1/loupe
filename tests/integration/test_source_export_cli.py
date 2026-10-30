"""Actual source-export RPCs preserve privacy and do not stall unrelated clients."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from test_cli_process_orchestration import _Cluster, _wait
from test_cli_process_orchestration import cluster as cluster
from test_session_switch_cli import _cli, _open, _switch

from llm_cli.agent.source_policy import contains_secret_material
from llm_cli.protocol.client import DaemonClient
from llm_cli.workspace.exports import SourceExporter
from llm_cli.workspace.workflow import decode_files, encode_files

_CANARY = "privateExportCanaryV7t9w2k4"


def _held(
    cluster: _Cluster, *, with_runtime: bool = False
) -> tuple[str, Path, dict[str, str]]:
    if with_runtime:
        (cluster.repository / ".gitignore").write_text(".env\n")
        (cluster.repository / ".env").write_text("SERVICE_PASSWORD=" + _CANARY + "\n")
    authenticated = _open(cluster)
    flags = ["--to", "isolated"]
    if with_runtime:
        flags.extend(["--approve-secret", ".env"])
    isolated = _switch(cluster, "isolate-source", authenticated["session_id"], *flags)
    private = Path(isolated["workspace"]["canonical_path"])
    (private / "docs/a.md").chmod(0o755)
    (private / "docs/blob.bin").write_bytes(b"\x00\xffsource binary\x00")
    (private / "docs/link").symlink_to("a.md")
    shared = _switch(
        cluster, "hold-source", authenticated["session_id"], "--to", "shared", "--hold"
    )
    return shared["proposal_task_id"], private, authenticated


@pytest.mark.parametrize("format", ["patch", "bundle"])
def test_real_cli_exports_typed_held_source_without_body_output(
    cluster: _Cluster,
    format: str,
    git_run: Any,
) -> None:
    identifier, _, _ = _held(cluster)
    destination = cluster.root / ("review." + format)
    arguments = [
        "change",
        "export",
        identifier,
        "--format",
        format,
        "--to",
        str(destination),
        "--intent",
        "cli-export",
    ]
    result = _cli(cluster, "export-source", arguments)
    assert result["state"] == "exported" and result["files"] == 3
    assert result["source_kind"] == "proposal"
    assert "source binary" not in repr(result)
    before = destination.stat()
    assert _cli(cluster, "repeat-source", arguments) == result
    assert destination.stat().st_ino == before.st_ino
    assert not destination.stat().st_mode & 0o077
    if format == "patch":
        clone = cluster.root / "clean-base"
        git_run(
            cluster.repository,
            "clone",
            "--no-hardlinks",
            str(cluster.repository),
            str(clone),
        )
        git_run(clone, "apply", "--check", str(destination))
    else:
        bundle = json.loads(destination.read_bytes())
        assert b"\x00\xffsource binary\x00" in {
            base64.b64decode(body) for body in bundle["bodies"].values()
        }
    with closing(sqlite3.connect(cluster.paths.control_db)) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(
            connection.execute(
                "SELECT * FROM export_intents_v2 WHERE export_intent_id='cli-export'"
            ).fetchone()
        )
        assert "source binary" not in repr(row)
        assert "bodies" not in row["source_manifest_json"]
        assert destination.read_bytes() not in json.dumps(row).encode()


def test_staged_candidate_and_published_batch_export_through_cli(
    cluster: _Cluster,
) -> None:
    authenticated = _open(cluster)
    read = cluster.rpc(
        "workspace.read_file", **authenticated, relative_path="docs/a.md"
    )
    cluster.rpc(
        "workspace.stage_file",
        **authenticated,
        relative_path="docs/a.md",
        candidate_id="manual-candidate",
        base=read["identity"],
        content="manual source result\n",
    )
    first = _cli(
        cluster,
        "candidate-export",
        [
            "change",
            "export",
            "manual-candidate",
            "--kind",
            "candidate",
            "--to",
            str(cluster.root / "candidate.patch"),
        ],
    )
    assert first["source_kind"] == "candidate"
    assert "manual source result" in (cluster.root / "candidate.patch").read_text()
    isolated = _switch(
        cluster, "batch-isolate", authenticated["session_id"], "--to", "isolated"
    )
    private = Path(isolated["workspace"]["canonical_path"])
    (private / "docs/b.md").write_text("published batch source\n")
    _switch(
        cluster,
        "publish-batch",
        authenticated["session_id"],
        "--to",
        "shared",
        "--publish",
    )
    with closing(sqlite3.connect(cluster.paths.control_db)) as connection:
        batch = connection.execute(
            "SELECT batch_id FROM workspace_batches WHERE state='published' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
    published = _cli(
        cluster,
        "batch-export",
        [
            "change",
            "export",
            batch,
            "--kind",
            "batch",
            "--to",
            str(cluster.root / "batch.patch"),
        ],
    )
    assert published["source_kind"] == "batch"
    assert "published batch source" in (cluster.root / "batch.patch").read_text()


@pytest.mark.parametrize("private_value", [False, True])
def test_screened_source_refusal_is_path_only_and_absent_from_export_rows_and_logs(
    cluster: _Cluster,
    private_value: bool,
) -> None:
    assert not contains_secret_material(_CANARY)
    identifier, _, _ = _held(cluster, with_runtime=private_value)
    secret = _CANARY if private_value else "sk-" + "0" * 40
    with closing(sqlite3.connect(cluster.paths.control_db)) as connection:
        encoded = connection.execute(
            "SELECT proposal_json FROM task_workflows WHERE task_id=?", (identifier,)
        ).fetchone()[0]
        files = list(decode_files(encoded))
        from dataclasses import replace

        files[0] = replace(files[0], content=secret.encode())
        connection.execute(
            "UPDATE task_workflows SET proposal_json=? WHERE task_id=?",
            (encode_files(tuple(files)), identifier),
        )
        connection.commit()
    destination = cluster.root / "refused.patch"
    result = _cli(
        cluster,
        "refuse-screened",
        [
            "change",
            "export",
            identifier,
            "--to",
            str(destination),
            "--intent",
            "private-source",
        ],
        exit_code=65,
    )
    assert result["error"]["code"] == "REPOSITORY_UNSAFE"
    assert result["error"]["details"]["path"] == "docs/a.md"
    assert secret not in repr(result)
    assert not destination.exists()
    with closing(sqlite3.connect(cluster.paths.control_db)) as connection:
        assert (
            connection.execute("SELECT count(*) FROM export_intents_v2").fetchone()[0]
            == 0
        )
        assert secret not in "\n".join(connection.iterdump())
    for log in [
        *cluster.root.glob("*.log"),
        *cluster.paths.log_file.parent.glob("*.jsonl"),
    ]:
        assert secret not in log.read_text(), log


def _paused_daemon_entry() -> None:
    from test_cli_process_orchestration import _daemon_entry

    original = SourceExporter._publish

    def paused(self: SourceExporter, *args: Any, **kwargs: Any) -> Any:
        signals = Path(os.environ["ORCHESTRATION_SIGNALS"])
        (signals / "source-export-ready").touch()
        _wait(lambda: (signals / "source-export-release").exists(), timeout=30)
        return original(self, *args, **kwargs)

    SourceExporter._publish = paused
    _daemon_entry()


class _PausedCluster(_Cluster):
    def _spawn(self, name: str, args: list[str]) -> Any:
        if name == "daemon":
            args = [
                "-c",
                "from test_source_export_cli import _paused_daemon_entry; "
                "_paused_daemon_entry()",
            ]
        return super()._spawn(name, args)


def test_slow_export_does_not_hold_daemon_or_publication_lock(
    tmp_path: Path,
    repository_factory: Any,
) -> None:
    root = repository_factory(tmp_path, {"docs/a.md": "base a\n"})
    with TemporaryDirectory(prefix="c22-rpc-", dir="/tmp") as runtime:
        group = _PausedCluster(tmp_path, root, runtime)
        try:
            identifier, _, authenticated = _held(group)
            process = group._spawn(
                "slow-export",
                [
                    "-m",
                    "llm_cli",
                    "--profile",
                    "test",
                    "--json",
                    "change",
                    "export",
                    identifier,
                    "--to",
                    str(tmp_path / "slow.patch"),
                ],
            )
            _wait(lambda: (group.signals / "source-export-ready").exists())
            try:
                fast = DaemonClient(group.paths, timeout_seconds=2.0)
                assert fast.call("repo.list", autostart=False)
                current = fast.call(
                    "workspace.read_file",
                    {**authenticated, "relative_path": "docs/a.md"},
                    autostart=False,
                )
                assert current["content"] == "base a\n"
            finally:
                (group.signals / "source-export-release").touch()
            assert process.wait(timeout=60) == 0, group.diagnostics()
        finally:
            (group.signals / "source-export-release").touch()
            group.close()
