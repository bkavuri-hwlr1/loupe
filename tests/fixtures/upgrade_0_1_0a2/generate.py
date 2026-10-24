"""Regenerate the release fixture using an unmodified v0.1.0a2 checkout.

Run with an all-extras development interpreter: python generate.py OLD_CHECKOUT.
The old checkout supplies both the application and its scripted-provider helpers.
No credentials, real profiles, authority directories, or network are used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE_COMMIT = "a4b37a95921505d792180468eef89e231c8843dc"
PLACEHOLDER = "/loupe-upgrade-fixture"


def generate(source: Path, output: Path) -> None:
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if (
        commit != SOURCE_COMMIT
        or subprocess.check_output(
            ["git", "-C", str(source), "status", "--porcelain"], text=True
        ).strip()
    ):
        raise SystemExit("source must be a clean v0.1.0a2 checkout")
    sys.path[:0] = [str(source / "src"), str(source / "tests" / "integration")]
    from test_shared_agent_execution import (
        ScriptedProvider,
        _call,
        _finish,
        _open_session,
        _register,
        _request,
        _tools,
    )
    from test_verified_workflow import await_task

    from llm_cli import __version__
    from llm_cli.config.models import Settings
    from llm_cli.daemon.service import DaemonService
    from llm_cli.paths import AppPaths

    assert __version__ == "0.1.0a2"
    with tempfile.TemporaryDirectory(prefix="loupe-upgrade-build-") as temporary:
        root = Path(temporary).resolve()
        repository = root / "repository"
        repository.mkdir()
        (repository / "a.py").write_text("value = 1\n")
        environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        }
        for arguments in (
            ["init", "-b", "main"],
            ["config", "user.name", "Release fixture"],
            ["config", "user.email", "fixture@example.invalid"],
            ["remote", "add", "origin", "https://example.invalid/loupe-upgrade.git"],
            ["add", "a.py"],
            ["commit", "-m", "release upgrade baseline"],
        ):
            subprocess.run(
                ["git", *arguments],
                cwd=repository,
                env=environment,
                check=True,
                capture_output=True,
            )
        paths = AppPaths.resolve(
            "test",
            environ={
                f"LLM_COORD_{name}_HOME": str(root / name.lower())
                for name in ("CONFIG", "DATA", "STATE")
            }
            | {"LLM_COORD_RUNTIME_DIR": str(root / "run")},
            home=root,
        )
        service = DaemonService(
            paths,
            Settings(profile_id="test"),
            asyncio.Event(),
            boot_id="release-fixture-old-daemon",
        )

        async def scenario() -> None:
            providers = [
                ScriptedProvider(
                    f"upgrade-{kind}-session",
                    [
                        _tools(
                            _call("read_file", path="a.py"),
                            _call(
                                "write_file", path="a.py", content=f"value = {value}\n"
                            ),
                        ),
                        _finish(f"{kind} release upgrade task"),
                    ],
                )
                for kind, value in (("published", 2), ("held", 3))
            ]
            _register(service, *providers)
            service.initialize()
            await service.handle(_request("repo.add", {"path": str(repository)}))
            for provider, kind in zip(providers, ("published", "held"), strict=True):
                credentials = await _open_session(service, repository, provider)
                if kind == "held":
                    await service.handle(
                        _request("session.set_mode", {**credentials, "mode": "normal"})
                    )
                await await_task(service, repository, credentials, f"upgrade-{kind}")
            assert service._task_view("upgrade-published")["state"] == "completed"
            assert service._task_view("upgrade-held")["state"] == "awaiting_review"
            assert (repository / "a.py").read_text() == "value = 2\n"

        try:
            asyncio.run(scenario())
        finally:
            service.close()
        output.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "bundle", "create", str(output / "repository.bundle"), "--all"],
            cwd=repository,
            env=environment,
            check=True,
            capture_output=True,
        )
        database = output / "control.sqlite3"
        with (
            sqlite3.connect(paths.control_db) as original,
            sqlite3.connect(database) as saved,
        ):
            original.backup(saved)
            # Only relocate machine paths. Schema, migration receipts, identifiers,
            # history, proposal bodies and publication records come from the release.
            for (table,) in saved.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ):
                columns = list(saved.execute(f'PRAGMA table_info("{table}")'))
                for column in columns:
                    if column[2] != "TEXT":
                        continue
                    name = column[1]
                    saved.execute(
                        f'UPDATE "{table}" SET "{name}" = replace("{name}", ?, ?) '
                        f'WHERE instr("{name}", ?) > 0',
                        (str(root), PLACEHOLDER, str(root)),
                    )
            saved.commit()
            saved.execute("PRAGMA journal_mode=DELETE")
            saved.execute("VACUUM")
            versions = [
                row[0]
                for row in saved.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
            assert versions == list(range(1, 20))
            assert (
                saved.execute(
                    "SELECT count(*) FROM workspace_batches WHERE state='published'"
                ).fetchone()[0]
                == 1
            )
        shutil.copytree(paths.candidate_dir, output / "candidates", dirs_exist_ok=True)
        manifest = {
            "source_tag": "v0.1.0a2",
            "source_commit": SOURCE_COMMIT,
            "schema_version": versions[-1],
            "database_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            "path_placeholder": PLACEHOLDER,
            "repository_files": {"a.py": "value = 2\n"},
            "published_task": "upgrade-published",
            "held_task": "upgrade-held",
        }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    generate(Path(sys.argv[1]).resolve(), Path(__file__).resolve().parent)
