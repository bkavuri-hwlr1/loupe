"""Disposable authority-root injection for real test daemon processes only."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def install_test_authority() -> None:
    from llm_cli.daemon import main
    from llm_cli.daemon.service import DaemonService

    root = Path(os.environ["LLM_COORD_STATE_HOME"]).resolve().parent
    root = root / "physical-authority" / "v1"

    class DisposableAuthorityService(DaemonService):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("authority_root", root)
            super().__init__(*args, **kwargs)

    main.DaemonService = DisposableAuthorityService


def isolated_autostart_popen(popen: Callable[..., Any]) -> Callable[..., Any]:
    """Route only daemon autostart through an absolute, isolated test bootstrap.

    Keep the real client's -I, environment sanitization, working directory,
    process flags and startup wait. No product environment setting can select
    another authority root, and unrelated child commands are unchanged.
    """

    def start(arguments: Any, *args: Any, **kwargs: Any) -> Any:
        if (
            isinstance(arguments, (list, tuple))
            and list(arguments[1:5]) == ["-I", "-u", "-m", "llm_cli.daemon.main"]
        ):
            arguments = [
                arguments[0],
                "-I",
                "-u",
                str(Path(__file__).resolve()),
                *arguments[5:],
            ]
        return popen(arguments, *args, **kwargs)

    return start


def isolated_cli_arguments(arguments: list[str]) -> list[str]:
    """Keep CLI fallback autostart inside the same disposable test authority."""
    if arguments[:2] in (["-m", "llm_cli"], ["-m", "llm_cli.cli.app"]):
        return [
            "-I",
            str(Path(__file__).resolve()),
            "--test-cli",
            *arguments[2:],
        ]
    return arguments


def run_test_cli() -> None:
    from llm_cli.cli.app import main
    from llm_cli.protocol import client

    client.subprocess.Popen = isolated_autostart_popen(client.subprocess.Popen)
    main()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--test-cli"]:
        del sys.argv[1]
        run_test_cli()
    else:
        install_test_authority()
        from llm_cli.daemon.main import main

        main()
