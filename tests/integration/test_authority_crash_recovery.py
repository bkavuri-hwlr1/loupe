"""An authority marker fences foreign daemons across every publication crash gap."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from llm_cli.config.models import Settings
from llm_cli.daemon import service as service_module
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.envelopes import Request
from llm_cli.workspace import authority as authority_module
from llm_cli.workspace.authority import PhysicalCheckout
from llm_cli.workspace.installation import private_read


class Crash(BaseException):
    pass


def _service(tmp_path: Path, profile: str, boot: str) -> DaemonService:
    paths = AppPaths.resolve(
        profile,
        environ={
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
        },
    )
    service = DaemonService(
        paths,
        Settings(profile_id=profile),
        asyncio.Event(),
        boot_id=boot,
        authority_root=tmp_path / "authority" / "v1",
    )
    service.initialize()
    return service


def _request(method: str, **params: Any) -> Request:
    return Request.create(
        request_id="authority-fault", method=method, params=params, profile_id="owner"
    )


async def _stage(service: DaemonService, repository: Path) -> dict[str, Any]:
    await service.handle(_request("repo.add", path=str(repository)))
    credentials = {
        "session_id": "owner-session",
        "resume_secret": "resume-only-for-this-test",
    }
    opened = await service.handle(
        _request(
            "session.open",
            path=str(repository),
            session_id=credentials["session_id"],
            resume_token_hash=hashlib.sha256(
                credentials["resume_secret"].encode()
            ).hexdigest(),
        )
    )
    await service.handle(
        _request("session.ack", **credentials, sequence=opened["bootstrap_sequence"])
    )
    observed = await service.handle(
        _request("workspace.read_file", **credentials, relative_path="file.txt")
    )
    await service.handle(
        _request(
            "workspace.stage_file",
            **credentials,
            relative_path="file.txt",
            base=observed["identity"],
            content="published result\n",
            candidate_id="fault-candidate",
        )
    )
    return credentials


@pytest.mark.parametrize(
    "boundary", ["dirty", "journal", "file", "database", "clean_marker"]
)
def test_foreign_recovery_never_replays_owner_and_exact_owner_recovers(
    tmp_path: Path,
    repository_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"file.txt": "original base\n"})
        owner = _service(tmp_path, "owner", "boot-before-crash")
        credentials = await _stage(owner, repository)
        physical = PhysicalCheckout.inspect(repository)
        marker_path = (
            owner.physical_authority.root / "checkouts" / physical.key / "marker.json"
        )
        physical.close()
        try:
            with monkeypatch.context() as patch:
                if boundary in {"dirty", "clean_marker"}:
                    original_write = authority_module.private_write

                    def write(path: Path, value: dict[str, Any]) -> None:
                        if boundary == "dirty" and value.get("state") == "dirty":
                            original_write(path, value)
                            raise Crash()
                        if boundary == "clean_marker" and value.get("state") == "clean":
                            raise Crash()
                        original_write(path, value)

                    patch.setattr(authority_module, "private_write", write)
                elif boundary == "journal":
                    original_begin = owner.store.begin_workspace_publication

                    def begin(**kwargs: Any) -> Any:
                        original_begin(**kwargs)
                        raise Crash()

                    patch.setattr(owner.store, "begin_workspace_publication", begin)
                elif boundary == "file":
                    original_replace = service_module.atomic_replace_regular_file

                    def replace(**kwargs: Any) -> None:
                        original_replace(**kwargs)
                        raise Crash()

                    patch.setattr(
                        service_module, "atomic_replace_regular_file", replace
                    )
                else:
                    original_confirm = owner.store.confirm_workspace_publication

                    def confirm(**kwargs: Any) -> Any:
                        original_confirm(**kwargs)
                        raise Crash()

                    patch.setattr(owner.store, "confirm_workspace_publication", confirm)
                with pytest.raises(Crash):
                    await owner.handle(
                        _request(
                            "workspace.publish_candidate",
                            **credentials,
                            candidate_id="fault-candidate",
                        )
                    )
        finally:
            owner.close()
        marker = private_read(marker_path)
        assert marker is not None and marker["state"] == "dirty"
        bytes_after_crash = (repository / "file.txt").read_bytes()
        peer = _service(tmp_path, "peer", "boot-foreign")
        try:
            await peer.handle(_request("repo.add", path=str(repository)))
            with pytest.raises(LlmCoordError) as refused:
                await peer.handle(
                    _request(
                        "session.open",
                        path=str(repository),
                        session_id="peer-session",
                        resume_token_hash=hashlib.sha256(b"peer-resume").hexdigest(),
                    )
                )
            assert refused.value.code is ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
            assert refused.value.details is not None
            assert refused.value.details["owner_profile"] == "owner"
            assert refused.value.details["journal_locator"]
            recovered = await peer.handle(_request("task.recover"))
            assert recovered["outcomes"] == []
            assert (repository / "file.txt").read_bytes() == bytes_after_crash
            assert private_read(marker_path) == marker
            with peer.store.connection() as connection:
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM workspace_publications"
                    ).fetchone()[0]
                    == 0
                )
            # A blocked checkout never disables unrelated work in this profile.
            other_dir = tmp_path / "other"
            other_dir.mkdir()
            other = repository_factory(other_dir, {"file.txt": "independent"})
            await peer.handle(_request("repo.add", path=str(other)))
            await peer.handle(
                _request(
                    "session.open",
                    path=str(other),
                    session_id="independent",
                    resume_token_hash=hashlib.sha256(b"different-resume").hexdigest(),
                )
            )
            observed = await peer.handle(
                _request(
                    "workspace.observe", path=str(repository), relative_path="file.txt"
                )
            )
            assert observed["edit_capability"] is False
            assert observed["cooperative_read_barrier"] is False
        finally:
            peer.close()
        restarted = _service(tmp_path, "owner", "boot-after-crash")
        try:
            settled = private_read(marker_path)
            assert settled is not None
            if boundary == "dirty":
                assert settled["state"] == "dirty"
                with pytest.raises(LlmCoordError):
                    restarted.physical_authority.acquire(repository, recovery=True)
            else:
                assert settled["state"] == "clean"
                expected = (
                    "original base\n" if boundary == "journal" else "published result\n"
                )
                assert (repository / "file.txt").read_text() == expected
                with restarted.store.connection() as connection:
                    publication = connection.execute(
                        "SELECT operation_state FROM workspace_publications"
                    ).fetchone()
                    assert publication[0] == (
                        "rolled_back" if boundary == "journal" else "confirmed"
                    )
        finally:
            restarted.close()

    asyncio.run(scenario())


def test_dirty_marker_with_missing_or_wrong_database_journal_stays_attention(
    tmp_path: Path, repository_factory: Callable[..., Path]
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"file.txt": "base"})
        owner = _service(tmp_path, "owner", "before")
        await _stage(owner, repository)
        with owner.physical_authority.mutation(
            repository, "manual", "missing-candidate"
        ):
            pass
        owner.close()
        with owner.store.connection() as connection:
            connection.execute("DELETE FROM authority_journals")
        restarted = _service(tmp_path, "owner", "after")
        try:
            status = await restarted.handle(
                _request("workspace.status", path=str(repository))
            )
            assert status["authority"]["state"] in {
                "foreign_recovery",
                "recovery_required",
            }
            with pytest.raises(LlmCoordError) as refused:
                restarted.physical_authority.acquire(repository, recovery=True)
            assert refused.value.code is ErrorCode.CHECKOUT_FOREIGN_RECOVERY_REQUIRED
            assert "journal" in str(refused.value)
            assert (repository / "file.txt").read_text() == "base"
        finally:
            restarted.close()

    asyncio.run(scenario())
