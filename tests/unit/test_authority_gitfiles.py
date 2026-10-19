"""Real standalone Git directories keep their checkout authority binding."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.workspace.authority import PhysicalCheckout


def _gitfile_checkout(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    kind: str,
) -> Path:
    source = repository_factory(tmp_path / "source", {"file.txt": "source bytes\n"})
    if kind == "separate":
        checkout = tmp_path / "checkout"
        git_run(
            source,
            "clone",
            "--separate-git-dir=" + str(tmp_path / "standalone.git"),
            str(source),
            str(checkout),
        )
        return checkout
    parent = repository_factory(tmp_path / "parent", {"parent.txt": "parent\n"})
    git_run(
        parent,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(source),
        "sub",
    )
    return parent / "sub"


@pytest.mark.parametrize("kind", ["separate", "submodule"])
def test_self_contained_gitfile_checkout_is_verified_without_back_pointer(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    kind: str,
) -> None:
    checkout = _gitfile_checkout(tmp_path, repository_factory, git_run, kind)
    assert (checkout / ".git").is_file()
    physical = PhysicalCheckout.inspect(checkout)
    try:
        assert physical.admin == physical.common
        assert physical.admin != checkout / ".git"
        assert not (physical.admin / "commondir").exists()
        assert not (physical.admin / "gitdir").exists()
        physical.validate()
        second = PhysicalCheckout.inspect(checkout)
        try:
            assert second.key == physical.key
            assert second.store_key == physical.store_key
        finally:
            second.close()
    finally:
        physical.close()


@pytest.mark.parametrize("kind", ["separate", "submodule"])
def test_managed_session_opens_in_self_contained_gitfile_checkout(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    service_factory: Callable[..., DaemonService],
    request_factory: Callable[[str, dict[str, Any]], Any],
    kind: str,
) -> None:
    checkout = _gitfile_checkout(tmp_path, repository_factory, git_run, kind)
    service = service_factory(tmp_path / "service")
    service.initialize()

    async def scenario() -> None:
        await service.handle(request_factory("repo.add", {"path": str(checkout)}))
        secret = "gitfile-session-resume"
        credentials = {"session_id": "gitfile-session", "resume_secret": secret}
        opened = await service.handle(
            request_factory(
                "session.open",
                {
                    "path": str(checkout),
                    "session_id": credentials["session_id"],
                    "workspace": "shared",
                    "resume_token_hash": hashlib.sha256(secret.encode()).hexdigest(),
                },
            )
        )
        await service.handle(
            request_factory(
                "session.ack",
                {**credentials, "sequence": opened["bootstrap_sequence"]},
            )
        )
        read = await service.handle(
            request_factory(
                "workspace.read_file", {**credentials, "relative_path": "file.txt"}
            )
        )
        assert read["content"] == "source bytes\n"
        assert service.physical_authority.status(checkout)["state"] == "owned"
        await service.handle(request_factory("session.close", credentials))

    try:
        asyncio.run(scenario())
    finally:
        service.close()


@pytest.mark.parametrize("kind", ["separate", "submodule"])
@pytest.mark.parametrize(
    "damage", ["retarget", "malformed", "symlink", "oversized", "fifo", "common"]
)
def test_self_contained_gitfile_binding_refuses_pointer_changes(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    kind: str,
    damage: str,
) -> None:
    checkout = _gitfile_checkout(tmp_path, repository_factory, git_run, kind)
    physical = PhysicalCheckout.inspect(checkout)
    try:
        pointer = checkout / ".git"
        if damage == "retarget":
            other = repository_factory(tmp_path / "other", {"file.txt": "other\n"})
            pointer.write_text("gitdir: " + str(other / ".git") + "\n")
        elif damage == "malformed":
            pointer.write_text("not-a-gitdir: " + str(physical.admin) + "\n")
        elif damage == "symlink":
            replacement = tmp_path / "pointer"
            replacement.write_bytes(pointer.read_bytes())
            pointer.unlink()
            pointer.symlink_to(replacement)
        elif damage == "oversized":
            pointer.write_text("gitdir: " + "a" * 4096)
        elif damage == "fifo":
            pointer.unlink()
            os.mkfifo(pointer)
        else:
            (physical.admin / "commondir").write_text(".\n")
        with pytest.raises(LlmCoordError) as refused:
            physical.validate()
        assert refused.value.code is ErrorCode.CHECKOUT_IDENTITY_CHANGED
    finally:
        physical.close()


@pytest.mark.parametrize("kind", ["separate", "submodule"])
def test_self_contained_gitfile_cannot_start_with_a_commondir_pointer(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    kind: str,
) -> None:
    checkout = _gitfile_checkout(tmp_path, repository_factory, git_run, kind)
    admin = Path(git_run(checkout, "rev-parse", "--absolute-git-dir"))
    (admin / "commondir").write_text(".\n")
    with pytest.raises(LlmCoordError) as refused:
        PhysicalCheckout.inspect(checkout)
    assert refused.value.code is ErrorCode.CHECKOUT_IDENTITY_CHANGED


@pytest.mark.parametrize(
    "damage", ["back-pointer", "missing-back-pointer", "commondir"]
)
def test_linked_worktree_relationship_checks_are_retained(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    git_run: Callable[..., str],
    damage: str,
) -> None:
    primary = repository_factory(tmp_path / "primary", {"file.txt": "base\n"})
    linked = tmp_path / "linked"
    git_run(primary, "worktree", "add", "--detach", str(linked), "HEAD")
    physical = PhysicalCheckout.inspect(linked)
    try:
        assert physical.admin != physical.common
        if damage == "back-pointer":
            (physical.admin / "gitdir").write_text(str(primary / ".git") + "\n")
        elif damage == "missing-back-pointer":
            (physical.admin / "gitdir").unlink()
        else:
            (physical.admin / "commondir").unlink()
        with pytest.raises(LlmCoordError) as refused:
            physical.validate()
        assert refused.value.code is ErrorCode.CHECKOUT_IDENTITY_CHANGED
    finally:
        physical.close()
