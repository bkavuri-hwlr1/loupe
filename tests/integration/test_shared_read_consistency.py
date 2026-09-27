"""Shared read responses must carry the identity of their returned bytes."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from llm_cli.daemon.service import DaemonService
from llm_cli.errors import ErrorCode, LlmCoordError
from llm_cli.protocol.envelopes import Request
from llm_cli.workspace.broker import MAX_SHARED_TEXT_BYTES
from llm_cli.workspace.identity import ObjectKind, content_identity


def test_external_replace_while_recording_read_keeps_identity_and_body_together(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    request_factory: Callable[[str, dict[str, Any]], Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"guide.md": "before save\n"})
        service = service_factory(tmp_path)
        service.initialize()
        credentials = await _open_reader(service, request_factory, repository)
        original_record = service.store.record_workspace_read

        def record_then_save(**kwargs: Any) -> Any:
            result = original_record(**kwargs)
            replacement = repository / "editor-save.tmp"
            replacement.write_text("after save\n", encoding="utf-8")
            replacement.replace(repository / "guide.md")
            return result

        # A daemon barrier cannot exclude an editor's atomic save. Insert one
        # at the former gap between observing identity and reading content.
        monkeypatch.setattr(service.store, "record_workspace_read", record_then_save)
        response = await service.handle(
            request_factory(
                "workspace.read_file",
                {**credentials, "relative_path": "guide.md"},
            )
        )
        identity = response["identity"]
        body = response["content"].encode("utf-8")
        assert identity["digest"] == content_identity(
            ObjectKind.REGULAR, identity["mode"], body
        )
        assert identity["size"] == len(body)
        assert (repository / "guide.md").read_text() == "after save\n"
        with service.store.connection() as connection:
            observed = connection.execute(
                "SELECT observed_digest FROM workspace_read_observations "
                "WHERE session_id = ? AND relative_path = ?",
                (credentials["session_id"], "guide.md"),
            ).fetchone()
        assert observed is not None
        assert observed["observed_digest"] == identity["digest"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"x" * (MAX_SHARED_TEXT_BYTES + 1), "read limit"),
        (b"\xff", "only UTF-8 text"),
    ],
    ids=["oversized", "invalid-utf8"],
)
def test_failed_read_does_not_issue_a_base_observation(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    request_factory: Callable[[str, dict[str, Any]], Request],
    body: bytes,
    message: str,
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"guide.md": "before save\n"})
        service = service_factory(tmp_path)
        service.initialize()
        credentials = await _open_reader(service, request_factory, repository)
        (repository / "guide.md").write_bytes(body)

        with pytest.raises(LlmCoordError, match=message) as error:
            await service.handle(
                request_factory(
                    "workspace.read_file",
                    {**credentials, "relative_path": "guide.md"},
                )
            )
        assert error.value.code == ErrorCode.CONFIG_INVALID
        with service.store.connection() as connection:
            observed = connection.execute(
                "SELECT 1 FROM workspace_read_observations "
                "WHERE session_id = ? AND relative_path = ?",
                (credentials["session_id"], "guide.md"),
            ).fetchone()
        assert observed is None

    asyncio.run(scenario())


async def _open_reader(
    service: DaemonService,
    request: Callable[[str, dict[str, Any]], Request],
    repository: Path,
) -> dict[str, str]:
    await service.handle(request("repo.add", {"path": str(repository)}))
    secret = "shared-read-consistency-resume-secret"
    credentials = {"session_id": "consistent-reader", "resume_secret": secret}
    opened = await service.handle(
        request(
            "session.open",
            {
                "session_id": credentials["session_id"],
                "path": str(repository),
                "resume_token_hash": hashlib.sha256(secret.encode()).hexdigest(),
            },
        )
    )
    await service.handle(
        request(
            "session.ack",
            {**credentials, "sequence": opened["bootstrap_sequence"]},
        )
    )
    return credentials
