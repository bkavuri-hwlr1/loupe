"""Build a real profile with an unmodified v0.1.0a3 checkout.

Usage: python build_profile.py OLD_CHECKOUT ROOT
ROOT must not exist. The profile, repository and physical authority root all
stay under ROOT, so check_upgrade.py can open the same files in place with
the current code. No credentials, real profiles or network are used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

SOURCE_COMMIT = "4b9615160262c677d07f4136a40857dbb0942d1d"


def main(source: Path, root: Path) -> None:
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain"], text=True
    ).strip()
    if commit != SOURCE_COMMIT or dirty:
        raise SystemExit("source must be a clean v0.1.0a3 checkout")
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

    import llm_cli
    from llm_cli.config.models import Settings
    from llm_cli.daemon.service import DaemonService
    from llm_cli.paths import AppPaths

    assert llm_cli.__version__ == "0.1.0a3", llm_cli.__version__
    assert str(source) in llm_cli.__file__, llm_cli.__file__

    root.mkdir(parents=True)  # refuses an existing root
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
        boot_id="release-upgrade-old-daemon",
        authority_root=root / "physical-authority" / "v1",
    )
    summary: dict[str, object] = {"old_version": llm_cli.__version__}

    async def scenario() -> None:
        shared = [
            ScriptedProvider(
                f"upgrade-{kind}-session",
                [
                    _tools(
                        _call("read_file", path="a.py"),
                        _call("write_file", path="a.py", content=f"value = {value}\n"),
                    ),
                    _finish(f"{kind} release upgrade task"),
                ],
            )
            for kind, value in (("published", 2), ("held", 3))
        ]
        isolated = ScriptedProvider(
            "upgrade-isolated-session",
            [
                _tools(
                    _call("read_file", path="a.py"),
                    _call("write_file", path="b.py", content="isolated = 1\n"),
                ),
                _finish("isolated release upgrade task"),
            ],
        )
        _register(service, *shared, isolated)
        service.initialize()
        await service.handle(_request("repo.add", {"path": str(repository)}))
        for provider, kind in zip(shared, ("published", "held"), strict=True):
            credentials = await _open_session(service, repository, provider)
            if kind == "held":
                await service.handle(
                    _request("session.set_mode", {**credentials, "mode": "normal"})
                )
            await await_task(service, repository, credentials, f"upgrade-{kind}")
        assert service._task_view("upgrade-published")["state"] == "completed"
        assert service._task_view("upgrade-held")["state"] == "awaiting_review"
        assert (repository / "a.py").read_text() == "value = 2\n"

        secret = f"resume-secret-for-{isolated.model}"
        credentials = {"session_id": isolated.model, "resume_secret": secret}
        opened = await service.handle(
            _request(
                "session.open",
                {
                    "path": str(repository),
                    "session_id": isolated.model,
                    "resume_token_hash": hashlib.sha256(secret.encode()).hexdigest(),
                    "provider": "scripted",
                    "model": isolated.model,
                    "workspace": "isolated",
                },
            )
        )
        await service.handle(
            _request(
                "session.ack", {**credentials, "sequence": opened["bootstrap_sequence"]}
            )
        )
        private = Path(opened["workspace"]["canonical_path"])
        await await_task(service, repository, credentials, "upgrade-isolated")
        view = service._task_view("upgrade-isolated")
        summary["isolated_task_state"] = view["state"]
        summary["isolated_private_worktree"] = str(private)
        summary["isolated_private_files"] = sorted(
            p.name for p in private.iterdir() if p.name != ".git"
        )
        summary["session_mode_isolated"] = opened["session"].get("agent_mode")

    try:
        asyncio.run(scenario())
    finally:
        service.close()

    def git(*arguments: str) -> str:
        return subprocess.check_output(
            ["git", *arguments], cwd=repository, env=environment, text=True
        ).strip()

    summary |= {
        "repository": str(repository),
        "head": git("rev-parse", "HEAD"),
        "status": git("status", "--porcelain"),
        "files": {p.name: p.read_text() for p in sorted(repository.glob("*.py"))},
        "control_db": str(paths.control_db),
    }
    (root / "old-profile.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
