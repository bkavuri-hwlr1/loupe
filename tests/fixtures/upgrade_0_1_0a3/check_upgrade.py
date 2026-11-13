"""Open the profile build_profile.py made, in place, with the current code.

Usage: uv run python tests/fixtures/upgrade_0_1_0a3/check_upgrade.py ROOT
It uses the same profile directories, repository and authority root, as a
real upgrade does, and then keeps working in the old sessions. A ROOT can be
checked once; build a new one to run again. No credentials or network.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path


def main(root: Path) -> None:
    new = Path(__file__).resolve().parents[3]
    sys.path[:0] = [str(new / "src"), str(new / "tests" / "integration")]
    from test_shared_agent_execution import (
        ScriptedProvider,
        _call,
        _finish,
        _register,
        _request,
        _start,
        _tools,
    )

    import llm_cli
    from llm_cli.config.models import Settings
    from llm_cli.daemon.service import DaemonService
    from llm_cli.paths import AppPaths
    from llm_cli.storage.migrations import CONTROL_MIGRATIONS

    assert str(new) in llm_cli.__file__, llm_cli.__file__
    before = json.loads((root / "old-profile.json").read_text())
    repository = Path(before["repository"])
    private = Path(before["isolated_private_worktree"])
    report: dict[str, object] = {
        "old_version": before["old_version"],
        "new_version": llm_cli.__version__,
    }

    def step(name: str, value: object = "ok") -> None:
        report[name] = value
        print(f"[ok] {name}: {value}", flush=True)

    paths = AppPaths.resolve(
        "test",
        environ={
            f"LLM_COORD_{name}_HOME": str(root / name.lower())
            for name in ("CONFIG", "DATA", "STATE")
        }
        | {"LLM_COORD_RUNTIME_DIR": str(root / "run")},
        home=root,
    )
    with closing(sqlite3.connect(paths.control_db)) as connection:
        old_max = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
    assert old_max == 33, old_max
    step("schema_before", old_max)

    service = DaemonService(
        paths,
        Settings(profile_id="test"),
        asyncio.Event(),
        boot_id="release-upgrade-new-daemon",
        authority_root=root / "physical-authority" / "v1",
    )

    def secret(session_id: str) -> dict[str, str]:
        return {
            "session_id": session_id,
            "resume_secret": f"resume-secret-for-{session_id}",
        }

    def database_checks(label: str) -> None:
        with closing(sqlite3.connect(paths.control_db)) as connection:
            applied = connection.execute(
                "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
            assert applied == [
                (m.version, m.name, m.checksum) for m in CONTROL_MIGRATIONS
            ]
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        step(f"database_{label}", f"migrations 1-{applied[-1][0]}, integrity ok")

    async def scenario() -> None:
        shared = ScriptedProvider(
            "upgrade-published-session",
            [
                _tools(
                    _call("read_file", path="a.py"),
                    _call("write_file", path="a.py", content="value = 4\n"),
                ),
                _finish("shared task after upgrade"),
            ],
        )
        held = ScriptedProvider("upgrade-held-session", [])
        isolated = ScriptedProvider(
            "upgrade-isolated-session",
            [
                _tools(
                    _call("read_file", path="a.py"),
                    _call("read_file", path="c.py"),
                ),
                _tools(_call("write_file", path="d.py", content="after_upgrade = 1\n")),
                _finish("isolated task after upgrade"),
            ],
        )
        _register(service, shared, held, isolated)
        service.initialize()
        database_checks("after_start")

        # --- read-only: old history is inspectable ---
        sessions = await service.handle(_request("session.list", {}))
        assert {s["session_id"] for s in sessions} == {
            "upgrade-published-session",
            "upgrade-held-session",
            "upgrade-isolated-session",
        }, sessions
        states = {}
        for task in ("upgrade-published", "upgrade-held", "upgrade-isolated"):
            shown = await service.handle(_request("task.show", {"task_id": task}))
            states[task] = shown["state"]
            events = await service.handle(_request("task.events", {"task_id": task}))
            assert any(e["event_type"] == "model.finished" for e in events), task
        assert states == {
            "upgrade-published": "completed",
            "upgrade-held": "awaiting_review",
            "upgrade-isolated": "completed",
        }, states
        step("old_history", states)
        proposal = await service.handle(
            _request("task.diff", {"task_id": "upgrade-held"})
        )
        assert proposal["status"] == "awaiting_review"
        assert "-value = 2" in proposal["diff"] and "+value = 3" in proposal["diff"]
        step("held_proposal_diff")
        status = await service.handle(
            _request("workspace.cutover.status", {"path": str(repository)})
        )
        assert status["regime"] == "legacy" and status["intent_id"] is None, status
        step("regime", status["regime"])
        assert {
            p.name: p.read_text() for p in sorted(repository.glob("*.py"))
        } == before["files"]
        step("checkout_untouched_by_startup")

        # --- new in 0.1.0a4: export old work (C22) ---
        exports = root / "exports"
        exports.mkdir()
        exported = await service.handle(
            _request(
                "change.export",
                {
                    "identifier": "upgrade-held",
                    "destination": str(exports / "held.patch"),
                    "export_intent_id": "upgrade-export-held",
                },
            )
        )
        patch = (exports / "held.patch").read_text()
        assert "+value = 3" in patch, patch
        subprocess.run(
            ["git", "apply", "--check", str(exports / "held.patch")],
            cwd=repository,
            check=True,
        )
        step("export_held_proposal_patch", exported.get("state"))
        with closing(sqlite3.connect(paths.control_db)) as connection:
            batches = [
                row[0]
                for row in connection.execute(
                    "SELECT batch_id FROM workspace_batches WHERE state='published' "
                    "ORDER BY rowid"
                )
            ]
        assert len(batches) == 2, batches
        outcomes = {}
        for index, batch in enumerate(batches):
            for strategy in ("patch", "bundle"):
                name = f"batch{index}.{'patch' if strategy == 'patch' else 'json'}"
                intent = f"upgrade-export-{index}-{strategy}"
                try:
                    result = await service.handle(
                        _request(
                            "change.export",
                            {
                                "identifier": batch,
                                "kind": "batch",
                                "format": strategy,
                                "destination": str(exports / name),
                                "export_intent_id": intent,
                            },
                        )
                    )
                    assert (exports / name).stat().st_size > 0
                    outcomes[name] = result.get("state")
                except Exception as error:  # documented refusals are findings
                    outcomes[name] = f"refused: {error}"
        step("export_old_batches", outcomes)

        # --- old held proposal applies with the new code ---
        applied = await service.handle(
            _request("task.apply", {"task_id": "upgrade-held"})
        )
        assert (repository / "a.py").read_text() == "value = 3\n"
        step("apply_old_held_proposal", applied["state"])

        # --- an old shared session resumes and runs a new task ---
        credentials = secret("upgrade-published-session")
        await service.handle(_request("session.resume", credentials))
        work = await _start(
            service, repository, credentials, "after-upgrade-shared", ("*",)
        )
        await asyncio.wait_for(work, 60)
        view = service._task_view("after-upgrade-shared")
        assert view["state"] == "completed", view
        assert (repository / "a.py").read_text() == "value = 4\n"
        step("old_shared_session_new_task", view["state"])

        # --- the old isolated session catches up and runs a new task ---
        (repository / "c.py").write_text("outside = 1\n")
        assert private.is_dir(), "private worktree should survive the upgrade"
        assert (private / "a.py").read_text() == "value = 2\n"  # as opened
        credentials = secret("upgrade-isolated-session")
        await service.handle(_request("session.resume", credentials))
        work = await _start(
            service, repository, credentials, "after-upgrade-isolated", ("*",)
        )
        await asyncio.wait_for(work, 60)
        view = service._task_view("after-upgrade-isolated")
        assert view["state"] == "completed", (
            view,
            service.store.list_task_events("after-upgrade-isolated")[-5:],
        )
        seen = repr(isolated.results[0])
        assert "value = 4" in seen and "outside = 1" in seen, seen
        assert (private / "a.py").read_text() == "value = 4\n"
        assert (private / "c.py").read_text() == "outside = 1\n"
        assert (repository / "d.py").read_text() == "after_upgrade = 1\n"
        step("old_isolated_session_catches_up_and_publishes", view["state"])

        # --- closing the old isolated session removes its private worktree ---
        await service.handle(_request("session.close", credentials))
        assert not private.exists()
        step("old_isolated_session_close_removes_worktree")

        status = await service.handle(
            _request("workspace.cutover.status", {"path": str(repository)})
        )
        assert status["regime"] == "legacy", status
        step("regime_after_use", status["regime"])

    try:
        asyncio.run(scenario())
    finally:
        service.close()
    database_checks("after_use")
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    assert head == before["head"], "Loupe must not create commits"
    step("git_head_unchanged")
    step(
        "final_files",
        {p.name: p.read_text() for p in sorted(repository.glob("*.py"))},
    )
    (root / "upgrade-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("UPGRADE CHECK PASSED")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
