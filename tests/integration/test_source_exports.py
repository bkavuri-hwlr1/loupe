"""Portable source exports preserve typed changes and recover every durable step."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_shared_batches import BatchEnvironment
from test_shared_batches import batch_env as batch_env

from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.workspace.batches import BatchFile, _identity_json, result_identity
from llm_cli.workspace.exports import SourceExporter
from llm_cli.workspace.identity import (
    ABSENT,
    DIRECTORY_MODE,
    EXECUTABLE_MODE,
    REGULAR_MODE,
    SYMLINK_MODE,
    read_identified_path,
)
from llm_cli.workspace.patches import portable_bundle, portable_patch
from llm_cli.workspace.workflow import encode_files


def _files(env: BatchEnvironment) -> tuple[BatchFile, ...]:
    before, original = read_identified_path(env.repository / "a.txt")
    assert original is not None
    return (
        BatchFile("a.txt", before, original, EXECUTABLE_MODE, original),
        BatchFile(
            "binary.dat",
            ABSENT,
            b"\x00\xff\x00binary payload",
            REGULAR_MODE,
            binary=True,
        ),
        BatchFile("link.txt", ABSENT, b"a.txt", SYMLINK_MODE),
    )


def _proposal(
    env: BatchEnvironment,
    files: tuple[BatchFile, ...],
    identifier: str = "held-proposal",
) -> str:
    session = env.store.get_session("first")
    assert session is not None
    checkout = env.store.get_checkout(session.checkout_id)
    assert checkout is not None
    task = env.store.create_task(
        repository_id=checkout.repository_id,
        session_id="first",
        task_id=identifier,
    )
    with env.store.connection() as connection:
        connection.execute(
            "INSERT INTO task_workflows(task_id,attempt,publish_mode,status,"
            "proposal_json,config_json,created_at,agent_mode) "
            "VALUES(?,1,'review','awaiting_review',?,'{}',?,'normal')",
            (task.task_id, encode_files(files), env.store._now(None)),
        )
    return task.task_id


def _manager(env: BatchEnvironment, tmp_path: Path, **kwargs: Any) -> SourceExporter:
    return SourceExporter(
        env.store, env.publisher.candidate_dir, tmp_path / "exports", **kwargs
    )


def _batch(env: BatchEnvironment) -> None:
    before, original = read_identified_path(env.repository / "a.txt")
    result = env.publisher.publish(
        "published-batch",
        "first",
        (
            BatchFile(
                "a.txt", before, b"published export body\n", REGULAR_MODE, original
            ),
        ),
        ("*",),
    )
    assert result.state == "published"


def test_typed_held_proposal_patch_passes_git_apply_check(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Any,
) -> None:
    files = _files(batch_env)
    identifier = _proposal(batch_env, files)
    destination = tmp_path / "held.patch"
    result = _manager(batch_env, tmp_path).export(
        identifier, destination, intent_id="typed-patch"
    )
    assert result["state"] == "exported" and result["files"] == 3
    assert result["source_kind"] == "proposal"
    assert not destination.stat().st_mode & 0o077
    clone = tmp_path / "clean-base"
    git_run(
        batch_env.repository,
        "clone",
        "--no-hardlinks",
        str(batch_env.repository),
        str(clone),
    )
    git_run(clone, "apply", "--check", str(destination))
    git_run(clone, "apply", str(destination))
    assert (clone / "binary.dat").read_bytes() == files[1].content
    assert (clone / "a.txt").stat().st_mode & 0o100
    assert (clone / "link.txt").is_symlink()
    assert os.readlink(clone / "link.txt") == "a.txt"


def test_bundle_has_exact_identities_and_every_content_hash_verifies(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    files = _files(batch_env)
    identifier = _proposal(batch_env, files)
    destination = tmp_path / "held.bundle.json"
    exporter = _manager(batch_env, tmp_path)
    result = exporter.export(
        identifier, destination, intent_id="typed-bundle", strategy="bundle"
    )
    bundle = json.loads(destination.read_bytes())
    assert bundle["version"] == 1 and bundle["format"] == "loupe-source-bundle"
    for item, entry in zip(files, bundle["files"], strict=True):
        assert entry["path"] == item.relative_path
        assert entry["before"] == _identity_json(item.base)
        assert entry["after"] == _identity_json(result_identity(item))
    for digest, encoded in bundle["bodies"].items():
        assert (
            hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest()
            == digest
        )
    row = exporter.get("typed-bundle")
    assert row is not None
    assert "bodies" not in row["source_manifest_json"]
    assert "binary payload" not in repr(row)
    assert destination.read_bytes() not in json.dumps(row).encode()
    assert result["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()


@pytest.mark.parametrize("strategy", ["patch", "bundle"])
def test_published_batch_exports_immutable_recorded_bodies(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    strategy: str,
) -> None:
    _batch(batch_env)
    (batch_env.repository / "a.txt").write_text("later checkout edit\n")
    destination = tmp_path / ("batch." + strategy)
    manager = _manager(batch_env, tmp_path)
    result = manager.export(
        "published-batch", destination, intent_id="batch-export", strategy=strategy
    )
    assert result["source_kind"] == "batch"
    assert "later checkout edit" not in destination.read_text()
    if strategy == "patch":
        assert "published export body" in destination.read_text()
    else:
        bundle = json.loads(destination.read_text())
        assert b"published export body\n" in {
            base64.b64decode(value) for value in bundle["bodies"].values()
        }


def test_unrelated_identical_destination_is_refused_without_adoption(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    files = _files(batch_env)
    identifier = _proposal(batch_env, files)
    destination = tmp_path / "existing.patch"
    destination.write_bytes(portable_patch(files))
    before = destination.stat()
    with pytest.raises(LlmCoordError, match="already exists"):
        _manager(batch_env, tmp_path).export(
            identifier, destination, intent_id="must-not-adopt"
        )
    after = destination.stat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )


@pytest.mark.parametrize(
    "phase",
    [
        "intent_recorded",
        "export_partial",
        "artifact_recorded",
        "stage_recorded",
        "stage_partial",
        "destination_prepared",
        "destination_published",
        "export_recorded",
    ],
)
def test_actual_crash_at_each_export_step_replays_without_rewriting_destination(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    phase: str,
) -> None:
    _batch(batch_env)
    destination = tmp_path / "crash.patch"
    script = """
import os, sys
from pathlib import Path
from llm_cli.storage.control import ControlStore
from llm_cli.workspace.exports import SourceExporter
store = ControlStore(Path(sys.argv[1]))
manager = SourceExporter(store, Path(sys.argv[2]), Path(sys.argv[3]),
    fault=lambda step: os._exit(86) if step == sys.argv[5] else None)
manager.export("published-batch", Path(sys.argv[4]), intent_id="crash-export")
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(batch_env.store.path),
            str(batch_env.publisher.candidate_dir),
            str(tmp_path / "exports"),
            str(destination),
            phase,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 86, child.stderr
    before = destination.stat() if destination.exists() else None
    if before is not None:
        assert destination.read_bytes().startswith(b"diff --git ")
    manager = _manager(batch_env, tmp_path)
    result = manager.export("published-batch", destination, intent_id="crash-export")
    assert result["state"] == "exported"
    if before is not None:
        after = destination.stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    again = manager.export("published-batch", destination, intent_id="crash-export")
    assert again == result
    assert not list(tmp_path.glob(".source_export_*.tmp"))
    assert not list((tmp_path / "exports" / "artifacts").glob(".switch_export_*.tmp"))


def test_into_checkout_is_explicit_and_partial_staging_stays_in_git_admin(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    identifier = _proposal(batch_env, _files(batch_env))
    destination = batch_env.repository / "review.patch"
    with pytest.raises(LlmCoordError, match="--into-checkout"):
        _manager(batch_env, tmp_path).export(
            identifier, destination, intent_id="no-consent"
        )
    assert not destination.exists()

    def stop(phase: str) -> None:
        if phase == "stage_partial":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _manager(batch_env, tmp_path, fault=stop).export(
            identifier,
            destination,
            intent_id="checkout-export",
            into_checkout=True,
        )
    manager = _manager(batch_env, tmp_path)
    row = manager.get("checkout-export")
    assert row is not None and row["state"] == "writing"
    assert Path(row["stage_parent"]).is_relative_to(batch_env.repository / ".git")
    assert not destination.exists()
    assert not list(batch_env.repository.glob(".source_export_*.tmp"))
    manager.export(
        identifier, destination, intent_id="checkout-export", into_checkout=True
    )
    assert destination.exists()


def test_parent_replacement_after_preparation_is_refused(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    _batch(batch_env)
    directory = tmp_path / "destination-parent"
    directory.mkdir()
    destination = directory / "review.patch"

    def stop(phase: str) -> None:
        if phase == "destination_prepared":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        _manager(batch_env, tmp_path, fault=stop).export(
            "published-batch", destination, intent_id="parent-proof"
        )
    directory.rename(tmp_path / "original-parent")
    directory.mkdir()
    with pytest.raises(LlmCoordError, match="parent changed"):
        _manager(batch_env, tmp_path).export(
            "published-batch", destination, intent_id="parent-proof"
        )
    assert not destination.exists()


def test_screened_candidate_refusal_has_only_path_and_no_new_body_storage(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    credential = ("sk-" + "0" * 40).encode()
    files = (replace(_files(batch_env)[0], content=credential),)
    identifier = _proposal(batch_env, files)
    manager = _manager(batch_env, tmp_path)
    with pytest.raises(LlmCoordError) as caught:
        manager.export(identifier, tmp_path / "unsafe.patch", intent_id="screened")
    assert caught.value.code == ErrorCode.REPOSITORY_UNSAFE
    assert caught.value.details == {"path": "a.txt"}
    assert credential.decode() not in str(caught.value)
    assert manager.get("screened") is None
    assert not list((tmp_path / "exports" / "bodies").iterdir())


def test_runtime_path_is_never_exported_even_with_safe_looking_contents(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    identifier = _proposal(
        batch_env, (BatchFile(".env", ABSENT, b"MODE=ordinary\n", REGULAR_MODE),)
    )
    with pytest.raises(LlmCoordError) as caught:
        _manager(batch_env, tmp_path).export(
            identifier,
            tmp_path / "runtime.bundle",
            intent_id="runtime",
            strategy="bundle",
        )
    assert caught.value.details == {"path": ".env"}
    assert not (tmp_path / "runtime.bundle").exists()


def test_intent_arguments_are_immutable_and_exported_retry_keeps_identity(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    _batch(batch_env)
    manager = _manager(batch_env, tmp_path)
    destination = tmp_path / "first.patch"
    result = manager.export("published-batch", destination, intent_id="same-intent")
    before = destination.stat()
    with pytest.raises(LlmCoordError, match="different arguments"):
        manager.export(
            "published-batch", tmp_path / "second.patch", intent_id="same-intent"
        )
    assert (
        manager.export("published-batch", destination, intent_id="same-intent")
        == result
    )
    assert destination.stat().st_ino == before.st_ino
    assert not (tmp_path / "second.patch").exists()


def test_patch_refuses_missing_before_instead_of_fabricating_creation(
    batch_env: BatchEnvironment,
) -> None:
    before, _ = read_identified_path(batch_env.repository / "a.txt")
    files = (BatchFile("a.txt", before, b"different\n", REGULAR_MODE),)
    with pytest.raises(ValueError, match="exact before bytes"):
        portable_patch(files)
    bundle = json.loads(portable_bundle(files))
    assert bundle["files"][0]["before"] == _identity_json(before)
    assert bundle["files"][0]["before_body"] is None


def test_partial_stage_user_edit_is_never_overwritten(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    _batch(batch_env)
    destination = tmp_path / "guarded.patch"

    def stop(phase: str) -> None:
        if phase == "stage_partial":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        _manager(batch_env, tmp_path, fault=stop).export(
            "published-batch", destination, intent_id="edited-stage"
        )
    manager = _manager(batch_env, tmp_path)
    row = manager.get("edited-stage")
    assert row is not None
    stage = Path(row["stage_parent"]) / row["stage_name"]
    stage.write_bytes(b"user replacement")
    before = stage.stat()
    with pytest.raises(LlmCoordError, match="staging contents changed"):
        manager.export("published-batch", destination, intent_id="edited-stage")
    assert stage.read_bytes() == b"user replacement"
    assert stage.stat().st_mtime_ns == before.st_mtime_ns
    assert not destination.exists()


def test_old_batch_without_recorded_original_uses_only_verified_git_base(
    batch_env: BatchEnvironment,
    tmp_path: Path,
    git_run: Any,
) -> None:
    files = batch_env.files()
    assert all(item.original is None for item in files)
    batch_env.publisher.publish("older-batch", "first", files, ("*",))
    destination = tmp_path / "older.patch"
    _manager(batch_env, tmp_path).export(
        "older-batch", destination, intent_id="older-export"
    )
    clone = tmp_path / "older-base"
    git_run(
        batch_env.repository,
        "clone",
        "--no-hardlinks",
        str(batch_env.repository),
        str(clone),
    )
    git_run(clone, "apply", "--check", str(destination))


def test_directory_only_source_requires_bundle_instead_of_empty_git_patch(
    batch_env: BatchEnvironment,
    tmp_path: Path,
) -> None:
    identifier = _proposal(
        batch_env,
        (BatchFile("empty-directory", ABSENT, b"", DIRECTORY_MODE),),
    )
    manager = _manager(batch_env, tmp_path)
    destination = tmp_path / "directory.patch"
    with pytest.raises(LlmCoordError, match="directory-only changes"):
        manager.export(identifier, destination, intent_id="empty-patch")
    assert not destination.exists()
    bundle = tmp_path / "directory.bundle"
    manager.export(identifier, bundle, intent_id="directory-bundle", strategy="bundle")
    assert (
        json.loads(bundle.read_bytes())["files"][0]["after"]["kind"]
        == "directory_marker"
    )
