"""Explicit idle cache recovery uses one profile-local RPC."""

from __future__ import annotations

import pytest

from llm_cli.cli.app import build_parser, dispatch


@pytest.mark.parametrize("all_artifacts", [False, True])
@pytest.mark.parametrize("busy", [False, True])
def test_cache_prune_command_uses_one_rpc(all_artifacts: bool, busy: bool) -> None:
    class Client:
        timeout_seconds = 120

        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def call(self, method: str, params: object) -> dict[str, object]:
            self.calls.append((method, params))
            return {"pruned": not busy, "notice": "retry exact prune command"}

    arguments = build_parser().parse_args(
        ["workspace", "cache", "prune", *(["--all"] if all_artifacts else [])]
    )
    client = Client()
    assert dispatch(arguments, client) == (
        "retry exact prune command" if busy else "Installer cache pruned."
    )
    assert client.calls == [("workspace.cache.prune", {"all": all_artifacts})]
