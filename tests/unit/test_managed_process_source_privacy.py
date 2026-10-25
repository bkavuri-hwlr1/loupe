"""Private process values stay private across source copies and daemon restart."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from test_managed_process_cleanup import Environment
from test_managed_process_cleanup import environment as environment

from llm_cli.agent.shared_tools import SharedToolBroker
from llm_cli.agent.source_policy import (
    cached_source_private_values,
    contains_private_source,
    contains_secret_material,
    ensure_safe_source,
    forget_source_private_values,
)
from llm_cli.errors import LlmCoordError
from llm_cli.workspace.isolated import (
    IsolatedWorkspaceManager,
    SourceCaptureCache,
    capture_source,
)

_CANARY = "violet walnut private fixture 519428"


def _remember(env: Environment, *values: str) -> None:
    env.manager.remember_process_values(env.workspace, tuple(values))


def _receipt(env: Environment) -> Path:
    assert env.workspace.git_dir is not None
    return Path(env.workspace.git_dir) / "loupe-process-private/values.json"


def _restart(env: Environment) -> None:
    forget_source_private_values(env.private)
    manager = IsolatedWorkspaceManager(
        env.store,
        env.manager.managed_root,
        env.manager.candidate_dir,
        env.manager.lock,
        lambda _: None,
    )
    manager.validate(env.workspace.workspace_id)
    env.manager = manager


@pytest.mark.parametrize("value", [_CANARY, "qZ6!"])
def test_non_signature_private_source_copy_is_never_read_or_stored(
    environment: Environment,
    value: str,
) -> None:
    env = environment
    assert not contains_secret_material(value)
    _remember(env, value)
    (env.private / "generated.py").write_text("VALUE = " + repr(value))
    (env.private / "generated.bin").write_bytes(b"\0" + value.encode())
    broker = SharedToolBroker(env.private, ("*",))
    for tool, arguments in (
        ("read_file", {"path": "generated.py"}),
        ("read_binary", {"path": "generated.bin"}),
        ("search_text", {"path": ".", "query": "VALUE"}),
    ):
        result = broker.invoke(tool, arguments)
        assert value not in result.content
        if tool != "search_text":
            assert result.is_error
    saved = json.dumps(broker.usage_snapshot())
    assert value not in saved
    manifest = capture_source(env.private, env.manager.candidate_dir)
    for path in ("generated.py", "generated.bin"):
        assert manifest[path]["screened"] is True
        assert manifest[path]["content_hash"] is None
    assert not any(
        value.encode() in item.read_bytes()
        for item in env.manager.candidate_dir.iterdir()
        if item.is_file()
    )


def test_deleted_or_rotated_runtime_keeps_old_values_after_reload(
    environment: Environment,
) -> None:
    env = environment
    _remember(env, _CANARY)
    (env.private / ".env").unlink()
    (env.private / "generated.py").write_text(_CANARY)
    _restart(env)
    assert _CANARY in cached_source_private_values(env.private)
    with pytest.raises(LlmCoordError, match="private runtime"):
        ensure_safe_source(env.private, _CANARY)
    assert (
        SharedToolBroker(env.private, ("*",))
        .invoke("read_file", {"path": "generated.py"})
        .is_error
    )
    (env.private / ".env").write_text("LABEL=another unique private value")
    _remember(env, "another unique private value")
    _restart(env)
    assert {_CANARY, "another unique private value"} <= set(
        cached_source_private_values(env.private)
    )


def test_only_opening_public_values_are_exempt_from_source_guard(
    environment: Environment,
) -> None:
    env = environment
    _remember(env, "1", _CANARY)
    assert "1" in cached_source_private_values(env.private)
    ensure_safe_source(env.private, "value = 1")
    (env.private / "generated.py").write_text(_CANARY)
    _remember(env, _CANARY)
    assert contains_private_source(env.private, _CANARY)
    _restart(env)
    ensure_safe_source(env.private, "value = 1")
    assert contains_private_source(env.private, _CANARY)


def test_registration_invalidates_unchanged_source_capture_cache(
    environment: Environment,
) -> None:
    env = environment
    cache = SourceCaptureCache()
    (env.private / "generated.py").write_text(_CANARY)
    before = capture_source(env.private, env.manager.candidate_dir, cache=cache)
    assert before["generated.py"]["content_hash"]
    _remember(env, _CANARY)
    after = capture_source(env.private, env.manager.candidate_dir, cache=cache)
    assert after["generated.py"]["screened"] is True
    assert after["generated.py"]["content_hash"] is None


@pytest.mark.parametrize("damage", ["missing", "truncated", "owner"])
def test_bad_required_receipt_blocks_source_with_empty_restart_cache(
    environment: Environment,
    damage: str,
) -> None:
    env = environment
    _remember(env, _CANARY)
    (env.private / "generated.py").write_text(_CANARY)
    receipt = _receipt(env)
    if damage == "missing":
        receipt.unlink()
    elif damage == "truncated":
        receipt.write_text("{")
    else:
        data = json.loads(receipt.read_text())
        data["token"] = "foreign-owner"
        receipt.write_text(json.dumps(data))
    forget_source_private_values(env.private)
    fresh = IsolatedWorkspaceManager(
        env.store,
        env.manager.managed_root,
        env.manager.candidate_dir,
        env.manager.lock,
        lambda _: None,
    )
    with pytest.raises(LlmCoordError, match="receipt"):
        fresh.validate(env.workspace.workspace_id)
    result = SharedToolBroker(env.private, ("*",)).invoke(
        "read_file", {"path": "generated.py"}
    )
    assert result.is_error and _CANARY not in result.content
    manifest = capture_source(env.private, env.manager.candidate_dir)
    assert manifest["generated.py"]["content_hash"] is None
    with pytest.raises(LlmCoordError, match="protection"):
        cached_source_private_values(env.private)


@pytest.mark.parametrize("unknown", [False, True])
def test_closed_cleanup_scrubs_source_copies_and_private_seed_before_archive(
    environment: Environment,
    unknown: bool,
) -> None:
    env = environment
    _remember(env, _CANARY)
    (env.private / "a.py").write_text(_CANARY)
    (env.private / "generated.py").write_text(_CANARY)
    if unknown:
        (env.private / "user.txt").write_text("preserve ordinary user edits")
    admin = Path(env.workspace.git_dir)
    env.close()
    assert not (admin / "loupe-process-private").exists()
    assert not (env.private / "generated.py").exists()
    assert not (env.private / "a.py").exists()
    if unknown:
        assert (env.private / "user.txt").read_text() == "preserve ordinary user edits"
    else:
        assert not env.private.exists()
        assert not any(
            _CANARY.encode() in path.read_bytes()
            for path in env.archive.rglob("*")
            if path.is_file()
        )


def test_operator_source_guard_is_cached_and_preserves_generic_policy(
    environment: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = environment
    _remember(env, _CANARY)

    def unexpected_io(*args: object, **kwargs: object) -> None:
        raise AssertionError("operator admission must use the cached root guard")

    monkeypatch.setattr(env.store, "connection", unexpected_io)
    monkeypatch.setattr(Path, "lstat", unexpected_io)
    env.manager.check_process_source(env.workspace.workspace_id, "ordinary source")
    env.manager.check_process_source(env.workspace.workspace_id, "AKIA" + "A" * 16)
    with pytest.raises(LlmCoordError, match="private runtime"):
        env.manager.check_process_source(env.workspace.workspace_id, _CANARY)


def test_restored_receipt_invalidates_capture_classified_during_block(
    environment: Environment,
) -> None:
    env = environment
    _remember(env, _CANARY)
    path = _receipt(env)
    original = path.read_bytes()
    path.write_text("{")
    forget_source_private_values(env.private)
    fresh = IsolatedWorkspaceManager(
        env.store,
        env.manager.managed_root,
        env.manager.candidate_dir,
        env.manager.lock,
        lambda _: None,
    )
    with pytest.raises(LlmCoordError):
        fresh.validate(env.workspace.workspace_id)
    cache = SourceCaptureCache()
    blocked = capture_source(env.private, env.manager.candidate_dir, cache=cache)
    assert blocked["a.py"]["screened"] is True
    path.write_bytes(original)
    fresh.validate(env.workspace.workspace_id)
    allowed = capture_source(env.private, env.manager.candidate_dir, cache=cache)
    assert allowed["a.py"]["content_hash"] is not None
    assert not allowed["a.py"].get("screened")


def test_corrupt_receipt_close_preserves_ordinary_source_and_scrubs_only_known_runtime(
    environment: Environment,
) -> None:
    env = environment
    _remember(env, _CANARY)
    (env.private / "user.txt").write_text("ordinary late user edit")
    _receipt(env).write_text("{")
    forget_source_private_values(env.private)
    env.close()
    assert (env.private / "user.txt").read_text() == "ordinary late user edit"
    assert (env.private / "a.py").read_text() == "value = 1\n"
    assert not (env.private / ".env").exists()
    assert not env.archive.exists()
    assert _receipt(env).exists()
    assert env.store.get_workspace(env.workspace.workspace_id).state == "quarantined"


def test_existing_archive_uses_original_private_guard_and_preserves_ordinary_evidence(
    environment: Environment,
) -> None:
    env = environment
    _remember(env, _CANARY)
    state = env.manager.get(env.workspace.workspace_id)
    status = env.private.stat()
    env.archive.mkdir(mode=0o700)
    (env.archive / "files").mkdir(mode=0o700)
    (env.archive / "owner.json").write_text(
        json.dumps(
            {
                "token": state.creation_token,
                "root_identity": [status.st_dev, status.st_ino],
                "private_paths": [],
            }
        )
    )
    (env.private / "a.py").rename(env.archive / "files/a.py")
    (env.archive / "files/generated-private.py").write_text(_CANARY)
    _restart(env)
    env.close()
    assert not env.private.exists()
    assert not (env.archive / "files/generated-private.py").exists()
    assert (env.archive / "files/a.py").read_text() == "value = 1\n"


def test_private_seed_retirement_crash_replays_after_durable_retired_marker(
    environment: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = environment
    _remember(env, _CANARY)
    (env.private / "generated.py").write_text(_CANARY)
    original = IsolatedWorkspaceManager._remove_private_entry

    class Crash(BaseException):
        pass

    def remove(parent: int, name: str) -> None:
        if name == "loupe-process-private":
            raise Crash()
        original(parent, name)

    monkeypatch.setattr(
        IsolatedWorkspaceManager, "_remove_private_entry", staticmethod(remove)
    )
    env.store.close_session("process-cleanup")
    with pytest.raises(Crash):
        env.manager.cleanup_closed()
    admin = Path(env.workspace.git_dir)
    assert (
        json.loads((admin / "loupe-process-values-required").read_text())["retired"]
        is True
    )
    assert not (env.private / "generated.py").exists()
    # Also cover a crash immediately after removal but before returning.
    original_fd = os.open(admin, os.O_RDONLY | os.O_DIRECTORY)
    try:
        original(original_fd, "loupe-process-private")
    finally:
        os.close(original_fd)
    monkeypatch.setattr(
        IsolatedWorkspaceManager, "_remove_private_entry", staticmethod(original)
    )
    forget_source_private_values(env.private)
    fresh = IsolatedWorkspaceManager(
        env.store,
        env.manager.managed_root,
        env.manager.candidate_dir,
        env.manager.lock,
        lambda _: None,
    )
    fresh.recover()
    assert not env.private.exists()
    assert env.store.get_workspace(env.workspace.workspace_id).state == "removed"
