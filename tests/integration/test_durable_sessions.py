"""Durable sessions, and the write authority their tasks are allowed to hold.

ADR 0007 makes a top-level chat a durable session whose coordination tasks are
short-lived children: they "must not hold write authority while the user is
thinking".  The workspace-modes plan says the same thing from the other side --
no long-lived path locks, only publication is serialized.  These tests pin that
property, and the ordinary background contract it must not disturb.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from llm_cli.config.models import Settings
from llm_cli.coordination.models import ClaimState
from llm_cli.daemon.service import DaemonService
from llm_cli.errors import LlmCoordError
from llm_cli.paths import AppPaths
from llm_cli.protocol.envelopes import Request

_SECRET = "resume-secret-for-tests"


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    (repository / "docs").mkdir(parents=True)
    (repository / "docs" / "guide.md").write_text("base\n", encoding="utf-8")
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "base")
    return repository


def _service(tmp_path: Path) -> DaemonService:
    paths = AppPaths.resolve(
        "test",
        environ={
            "LLM_COORD_CONFIG_HOME": str(tmp_path / "config"),
            "LLM_COORD_DATA_HOME": str(tmp_path / "data"),
            "LLM_COORD_STATE_HOME": str(tmp_path / "state"),
            "LLM_COORD_RUNTIME_DIR": str(tmp_path / "run"),
        },
        home=tmp_path,
    )
    return DaemonService(
        paths, Settings(profile_id="test"), asyncio.Event(), boot_id="boot-session"
    )


def _request(method: str, params: dict[str, Any]) -> Request:
    return Request.create(
        request_id=f"request_{method.replace('.', '_')}",
        method=method,
        params=params,
        profile_id="test",
    )


async def _open_session(service: DaemonService, repository: Path) -> str:
    opened = await service.handle(
        _request(
            "session.open",
            {
                "session_id": "session-under-test",
                "path": str(repository),
                "resume_token_hash": hashlib.sha256(_SECRET.encode()).hexdigest(),
            },
        )
    )
    await service.handle(
        _request(
            "session.ack",
            {
                "session_id": opened["session"]["session_id"],
                "resume_secret": _SECRET,
                "sequence": opened["bootstrap_sequence"],
            },
        )
    )
    return str(opened["session"]["session_id"])


async def _prompt(
    service: DaemonService,
    *,
    repository: Path,
    session_id: str | None,
    task_id: str,
    write: str,
    scopes: tuple[str, ...] = ("docs/",),
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "title": f"prompt for {task_id}",
        "path": str(repository),
        "scopes": list(scopes),
        "task_id": task_id,
        "fixture_writes": [write],
    }
    if session_id is not None:
        params |= {
            "interactive": True,
            "session_id": session_id,
            "resume_secret": _SECRET,
        }
    accepted = await service.handle(_request("task.run", params))
    background = service._background_tasks.get((task_id, 1))
    if background is not None:
        await asyncio.wait_for(background, timeout=20)
    return dict(accepted)


def test_a_session_task_releases_its_reservation_once_published(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        session_id = await _open_session(service, repository)

        await _prompt(
            service,
            repository=repository,
            session_id=session_id,
            task_id="prompt-one",
            write="docs/one.md=first\n",
        )

        claim_id = service.store.get_task("prompt-one").current_claim_id  # type: ignore[union-attr]
        claim = service.store.get_claim(str(claim_id))
        assert claim is not None
        assert claim.state is ClaimState.RELEASED
        assert claim.release_reason == "session_task_completed"

        # Settled is not discarded: the published result still stands and the
        # task is recorded as completed rather than cancelled.
        task = service.store.get_task("prompt-one")
        assert task is not None and task.state == "completed"
        assert _git(repository, "rev-parse", "refs/llm-coord/tasks/prompt-one")
        service.close()

    asyncio.run(scenario())


def test_a_second_prompt_is_not_blocked_by_the_first(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        session_id = await _open_session(service, repository)

        await _prompt(
            service,
            repository=repository,
            session_id=session_id,
            task_id="prompt-one",
            write="docs/one.md=first\n",
        )
        second = await _prompt(
            service,
            repository=repository,
            session_id=session_id,
            task_id="prompt-two",
            write="docs/two.md=second\n",
        )

        # The operator's own next prompt must not wait behind their last one.
        assert second["execution"] == "scheduled"
        assert service.store.get_task("prompt-two").state == "completed"  # type: ignore[union-attr]
        service.close()

    asyncio.run(scenario())


def test_a_background_task_still_holds_its_reservation(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))

        await _prompt(
            service,
            repository=repository,
            session_id=None,
            task_id="background-one",
            write="docs/one.md=first\n",
        )

        # Nothing owns a background result, so it stays reserved until an
        # operator decides. Only a session changes that.
        claim_id = service.store.get_task("background-one").current_claim_id  # type: ignore[union-attr]
        claim = service.store.get_claim(str(claim_id))
        assert claim is not None
        assert claim.state is ClaimState.ACTIVE_INTEGRATION
        service.close()

    asyncio.run(scenario())


def test_a_failed_session_task_is_not_settled_as_completed(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        session_id = await _open_session(service, repository)

        # Writing outside the claim fails validation, so the claim is released
        # as failed and there is no publication to settle.
        await _prompt(
            service,
            repository=repository,
            session_id=session_id,
            task_id="prompt-bad",
            write="src/outside.py=nope\n",
        )

        task = service.store.get_task("prompt-bad")
        assert task is not None
        assert task.state == "failed"
        claim = service.store.get_claim(str(task.current_claim_id))
        assert claim is not None
        assert claim.state is ClaimState.RELEASED
        assert claim.release_reason != "session_task_completed"
        service.close()

    asyncio.run(scenario())


def test_acknowledging_a_session_activates_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))

        session_id = await _open_session(service, repository)

        # A session stays in 'opening' until it acknowledges its bootstrap, and
        # nothing may submit editing work through it before then.
        session = service.store.get_session(session_id)
        assert session is not None
        assert session.state == "active"
        service.close()

    asyncio.run(scenario())


def test_acknowledging_without_a_sequence_is_a_validation_error(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        opened = await service.handle(
            _request(
                "session.open",
                {
                    "session_id": "session-no-sequence",
                    "path": str(repository),
                    "resume_token_hash": hashlib.sha256(_SECRET.encode()).hexdigest(),
                },
            )
        )
        assert opened["session"]["state"] == "opening"

        # A missing sequence must be a stable validation error, not an
        # unhandled TypeError surfacing as an internal daemon fault.
        with pytest.raises(LlmCoordError) as failure:
            await service.handle(
                _request(
                    "session.ack",
                    {
                        "session_id": "session-no-sequence",
                        "resume_secret": _SECRET,
                    },
                )
            )

        assert "sequence" in failure.value.message
        service.close()

    asyncio.run(scenario())


def test_a_session_binds_to_its_checkout_s_shared_workspace(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        session_id = await _open_session(service, repository)

        session = service.store.get_session(session_id)
        assert session is not None
        assert session.workspace_mode == "shared"
        assert session.workspace_id is not None

        workspace = service.store.get_workspace(session.workspace_id)
        assert workspace is not None
        assert workspace.kind == "shared_checkout"
        assert workspace.canonical_path == str(repository)
        # Revision and epoch are independent counters; a fresh checkout starts
        # at revision 0 of epoch 1 and neither is derived from the other.
        assert (workspace.workspace_epoch, workspace.workspace_revision) == (1, 0)
        service.close()

    asyncio.run(scenario())


def test_every_session_on_one_checkout_shares_its_workspace(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        first = await _open_session(service, repository)
        second_opened = await service.handle(
            _request(
                "session.open",
                {
                    "session_id": "second-session",
                    "path": str(repository),
                    "resume_token_hash": hashlib.sha256(b"other").hexdigest(),
                },
            )
        )

        # The shared workspace is the user's checkout, so it is not owned by
        # whichever session happened to open first.
        assert (
            second_opened["workspace"]["workspace_id"]
            == service.store.get_session(first).workspace_id  # type: ignore[union-attr]
        )
        service.close()

    asyncio.run(scenario())


def test_isolated_mode_is_refused_rather_than_silently_downgraded(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))

        # Hydration of ignored runtime files does not exist yet. Quietly giving
        # the operator a shared workspace instead would break exactly the
        # isolation they asked for.
        with pytest.raises(LlmCoordError) as failure:
            await service.handle(
                _request(
                    "session.open",
                    {
                        "session_id": "isolated-session",
                        "path": str(repository),
                        "resume_token_hash": hashlib.sha256(b"iso").hexdigest(),
                        "workspace": "isolated",
                    },
                )
            )

        assert "not implemented" in failure.value.message
        assert service.store.get_session("isolated-session") is None
        service.close()

    asyncio.run(scenario())


def test_workspace_status_reports_what_shared_mode_cannot_promise(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = _repository(tmp_path)
        service = _service(tmp_path)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        session_id = await _open_session(service, repository)

        status = await service.handle(
            _request("workspace.status", {"path": str(repository)})
        )

        assert status["workspace"]["mode"] == "shared"
        assert status["active_session_ids"] == [session_id]
        # These limits belong in front of an operator, not only in the plan.
        guarantees = " ".join(status["guarantees"])
        assert "cooperative" in guarantees
        assert "isolated mode" in guarantees
        service.close()

    asyncio.run(scenario())
