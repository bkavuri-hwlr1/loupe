"""Exercise an installed distribution, without accounts or a source checkout."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main() -> None:
    binary_dir = Path(sys.argv[1]).resolve()
    version = sys.argv[2]
    with tempfile.TemporaryDirectory(prefix="loupe-") as temporary:
        # An optional retained profile lets release CI verify real brew upgrades
        # and bottle reinstalls against the same on-disk application state.
        root = Path(sys.argv[3] if len(sys.argv) > 3 else temporary).resolve()
        root.mkdir(parents=True, exist_ok=True)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("LLM_COORD_", "XDG_", "PYTHON", "CODEX_"))
            and key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
        }
        env.update(
            HOME=str(root),
            LLM_COORD_CONFIG_HOME=str(root / "config"),
            LLM_COORD_DATA_HOME=str(root / "data"),
            LLM_COORD_STATE_HOME=str(root / "state"),
            # macOS Unix sockets have a short path limit. /tmp avoids long
            # Homebrew test paths and /var/folders temporary directory names.
            LLM_COORD_RUNTIME_DIR=str(
                Path(tempfile.mkdtemp(prefix="lp-", dir="/tmp")).resolve()
            ),
            NO_COLOR="1",
        )

        def run(*args: str, command: str = "loupe", input: str = "") -> str:
            result = subprocess.run(
                [str(binary_dir / command), *args],
                cwd=root,
                env=env,
                input=input,
                text=True,
                capture_output=True,
                timeout=45,
                check=False,
            )
            if result.returncode:
                raise AssertionError(
                    f"{command} {args} failed ({result.returncode}):\n"
                    f"{result.stdout}\n{result.stderr}"
                )
            return result.stdout

        def rpc(*args: str) -> dict:
            return json.loads(run("--profile", "packaging", "--json", *args))

        try:
            for command in ("loupe", "louped", "llm-coord", "llm-coordd"):
                assert run("--version", command=command).strip() == version
            assert "Loupe" in run("--help")
            assert json.loads(run("--json", "demo"))["simulated"] is True
            run("--plain", input="/exit\n")
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import anthropic, openai; "
                    "from llm_cli.providers import "
                    "anthropic_provider, openai_provider, codex_provider",
                ],
                cwd=root,
                env=env,
                check=True,
                timeout=30,
            )
            assert rpc("init")["initialized"] is True
            assert rpc("daemon", "status")["version"] == version
            health = rpc("doctor")
            assert health["ok"], health
            repository = root / "repository"
            if not repository.exists():
                repository.mkdir()
                subprocess.run(
                    ["git", "init", "-b", "main", str(repository)],
                    env=env,
                    check=True,
                    capture_output=True,
                )
                (repository / "README.md").write_text("Package test\n")
                subprocess.run(
                    ["git", "-C", str(repository), "add", "README.md"],
                    env=env,
                    check=True,
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repository),
                        "-c",
                        "user.name=Package test",
                        "-c",
                        "user.email=package-test@example.invalid",
                        "-c",
                        "commit.gpgsign=false",
                        "commit",
                        "-m",
                        "Initial package fixture",
                    ],
                    env=env,
                    check=True,
                    capture_output=True,
                )
            registered = rpc("repo", "add", str(repository))
            identity_file = root / "registered-repository.json"
            returning_profile = identity_file.exists()
            if returning_profile:
                previous = json.loads(identity_file.read_text())
                assert registered["repository_id"] == previous["repository_id"]
            else:
                identity_file.write_text(json.dumps(registered))
            # An idle restart must retain existing profile files.
            sentinel = (
                root / "state" / "llm-coord" / "profiles" / "packaging" / "sentinel"
            )
            if returning_profile:
                assert sentinel.read_text(encoding="utf-8") == "preserved"
            else:
                sentinel.write_text("preserved", encoding="utf-8")
            rpc("daemon", "restart")
            assert sentinel.read_text(encoding="utf-8") == "preserved"
        finally:
            subprocess.run(
                [
                    str(binary_dir / "loupe"),
                    "--profile",
                    "packaging",
                    "daemon",
                    "stop",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                timeout=15,
                check=False,
            )
            runtime = Path(env["LLM_COORD_RUNTIME_DIR"])
            for _ in range(100):
                if not list(runtime.rglob("*.sock")):
                    break
                time.sleep(0.05)
            shutil.rmtree(runtime)
    print(f"Loupe {version}: installed-package smoke test passed")


if __name__ == "__main__":
    main()
