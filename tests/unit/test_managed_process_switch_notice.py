"""Switch refusals retain the actionable stop command for every live writer."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from llm_cli.daemon.service import DaemonService


def test_live_process_stop_notice_survives_a_finishing_task_worker() -> None:
    process_id = "process_a1b2c3"
    stop_command = (
        "loupe --profile test workspace process stop "
        f"{process_id} --session owning-session"
    )
    notice = f"Process {process_id} is running; stop it with `{stop_command}`."

    async def scenario() -> None:
        session = SimpleNamespace(
            session_id="owning-session",
            workspace_id="private-workspace",
            checkout_id="checkout",
            state="active",
            workspace_mode="isolated",
        )
        service: Any = SimpleNamespace(
            _authority_lock=asyncio.Lock(),
            _pending_session_closes=set(),
            _pending_session_switches=set(),
            _authenticated_session=lambda _params, *, require_active: session,
            _workspace_switch_worker_busy=lambda _session_id: True,
            _managed_process_notice=lambda _workspace_id: notice,
        )
        with pytest.raises(ValueError) as refused:
            await DaemonService._workspace_switch_mode(
                service,
                {
                    "session_id": session.session_id,
                    "resume_secret": "disposable-test-resume-secret",
                    "switch_intent_id": "switch-with-live-process",
                    "workspace": "shared",
                    "disposition": "discard",
                },
            )
        assert process_id in str(refused.value)
        assert stop_command in str(refused.value)

    asyncio.run(scenario())
