"""End-to-end checks for the first shared-workspace publication slice."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from llm_cli.daemon.service import DaemonService
from llm_cli.protocol.envelopes import Request
from llm_cli.workspace.broker import atomic_replace_regular_file, load_candidate_content
from llm_cli.workspace.identity import ObjectKind, content_identity

_FIRST_SECRET = "first-shared-workspace-resume-secret"
_SECOND_SECRET = "second-shared-workspace-resume-secret"


async def _open_session(
    service: DaemonService,
    request: Callable[[str, dict[str, Any]], Request],
    repository: Path,
    *,
    session_id: str,
    secret: str,
) -> str:
    opened = await service.handle(
        request(
            "session.open",
            {
                "session_id": session_id,
                "path": str(repository),
                "resume_token_hash": hashlib.sha256(secret.encode()).hexdigest(),
            },
        )
    )
    await service.handle(
        request(
            "session.ack",
            {
                "session_id": session_id,
                "resume_secret": secret,
                "sequence": opened["bootstrap_sequence"],
            },
        )
    )
    return session_id


def _credentials(session_id: str, secret: str) -> dict[str, str]:
    return {"session_id": session_id, "resume_secret": secret}


def test_same_base_has_one_winner_and_preserves_a_durable_divergence(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    request_factory: Callable[[str, dict[str, Any]], Request],
    git_run: Callable[..., str],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        service = service_factory(tmp_path)
        service.initialize()
        await service.handle(request_factory("repo.add", {"path": str(repository)}))
        first = await _open_session(
            service,
            request_factory,
            repository,
            session_id="shared-first",
            secret=_FIRST_SECRET,
        )
        second = await _open_session(
            service,
            request_factory,
            repository,
            session_id="shared-second",
            secret=_SECOND_SECRET,
        )
        head_before = git_run(repository, "rev-parse", "HEAD")
        index_before = git_run(repository, "write-tree")
        refs_before = git_run(repository, "show-ref")

        first_read = await service.handle(
            request_factory(
                "workspace.read_file",
                {
                    **_credentials(first, _FIRST_SECRET),
                    "relative_path": "docs/guide.md",
                },
            )
        )
        second_read = await service.handle(
            request_factory(
                "workspace.read_file",
                {
                    **_credentials(second, _SECOND_SECRET),
                    "relative_path": "docs/guide.md",
                },
            )
        )
        assert first_read["identity"] == second_read["identity"]
        assert first_read["content"] == "base\n"

        first_candidate = await service.handle(
            request_factory(
                "workspace.stage_file",
                {
                    **_credentials(first, _FIRST_SECRET),
                    "candidate_id": "candidate-first",
                    "relative_path": "docs/guide.md",
                    "base": first_read["identity"],
                    "content": "first result\n",
                },
            )
        )
        second_candidate = await service.handle(
            request_factory(
                "workspace.stage_file",
                {
                    **_credentials(second, _SECOND_SECRET),
                    "candidate_id": "candidate-second",
                    "relative_path": "docs/guide.md",
                    "base": second_read["identity"],
                    "content": "second result\n",
                },
            )
        )
        published = await service.handle(
            request_factory(
                "workspace.publish_candidate",
                {
                    **_credentials(first, _FIRST_SECRET),
                    "candidate_id": "candidate-first",
                },
            )
        )
        diverged = await service.handle(
            request_factory(
                "workspace.publish_candidate",
                {
                    **_credentials(second, _SECOND_SECRET),
                    "candidate_id": "candidate-second",
                },
            )
        )

        assert published["outcome"] == "published"
        assert published["workspace"]["workspace_revision"] == 1
        assert (repository / "docs/guide.md").read_text(
            encoding="utf-8"
        ) == "first result\n"
        assert diverged["outcome"] == "diverged"
        assert diverged["candidate"]["state"] == "diverged"
        assert (
            diverged["divergence"]["current_digest"]
            == published["candidate"]["result_digest"]
        )
        durable_second = service.store.get_workspace_candidate("candidate-second")
        durable_divergence = service.store.get_workspace_divergence("candidate-second")
        assert durable_second is not None and durable_second.state == "diverged"
        assert durable_divergence is not None
        assert (
            durable_divergence.current_digest == published["candidate"]["result_digest"]
        )
        second_body = load_candidate_content(
            service.paths.candidate_dir, second_candidate["candidate"]["content_hash"]
        )
        assert second_body == b"second result\n"

        # Shared publication changes exactly the worktree. It does not change
        # HEAD, index, or refs, and Git cannot see the temporary stage root.
        assert git_run(repository, "rev-parse", "HEAD") == head_before
        assert git_run(repository, "write-tree") == index_before
        assert git_run(repository, "show-ref") == refs_before
        events = await service.handle(
            request_factory(
                "session.events", {**_credentials(second, _SECOND_SECRET), "after": 0}
            )
        )
        assert [event["event_type"] for event in events].count(
            "workspace.change_published"
        ) == 1
        assert [event["event_type"] for event in events].count(
            "workspace.candidate_diverged"
        ) == 1
        assert first_candidate["candidate"]["state"] == "staged"
        service.close()

    asyncio.run(scenario())


def test_startup_confirms_a_rename_that_crashed_before_sqlite_commit(
    tmp_path: Path,
    repository_factory: Callable[[Path, dict[str, str]], Path],
    service_factory: Callable[[Path], DaemonService],
    request_factory: Callable[[str, dict[str, Any]], Request],
) -> None:
    async def scenario() -> None:
        repository = repository_factory(tmp_path, {"docs/guide.md": "base\n"})
        service = service_factory(tmp_path)
        service.initialize()
        await service.handle(request_factory("repo.add", {"path": str(repository)}))
        session_id = await _open_session(
            service,
            request_factory,
            repository,
            session_id="crash-session",
            secret=_FIRST_SECRET,
        )
        read = await service.handle(
            request_factory(
                "workspace.read_file",
                {
                    **_credentials(session_id, _FIRST_SECRET),
                    "relative_path": "docs/guide.md",
                },
            )
        )
        staged = await service.handle(
            request_factory(
                "workspace.stage_file",
                {
                    **_credentials(session_id, _FIRST_SECRET),
                    "candidate_id": "candidate-crash",
                    "relative_path": "docs/guide.md",
                    "base": read["identity"],
                    "content": "after crash\n",
                },
            )
        )
        candidate, publication, workspace = service.store.begin_workspace_publication(
            candidate_id="candidate-crash", session_id=session_id
        )
        body = load_candidate_content(
            service.paths.candidate_dir, candidate.content_hash
        )
        atomic_replace_regular_file(
            checkout_root=Path(workspace.canonical_path),
            git_common_dir=Path(workspace.git_dir or ""),
            relative_path=candidate.relative_path,
            content=body,
            mode=candidate.result_mode,
            publication_id=publication.publication_id,
        )
        assert (repository / "docs/guide.md").read_text(
            encoding="utf-8"
        ) == "after crash\n"
        service.close()

        restarted = service_factory(tmp_path)
        restarted.initialize()
        recovered = restarted.store.get_workspace_candidate("candidate-crash")
        recovered_publication = restarted.store.get_workspace_publication(
            "candidate-crash"
        )
        assert recovered is not None and recovered.state == "published"
        assert recovered_publication is not None
        assert recovered_publication.operation_state == "confirmed"
        assert recovered_publication.workspace_revision == 1
        expected = content_identity(
            ObjectKind.REGULAR, recovered.result_mode, b"after crash\n"
        )
        assert recovered.result_digest == expected
        assert staged["candidate"]["state"] == "staged"
        restarted.close()

    asyncio.run(scenario())
