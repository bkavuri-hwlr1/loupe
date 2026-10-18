"""Disposable authority-root injection for real test daemon processes only."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def install_test_authority() -> None:
    from llm_cli.daemon import main
    from llm_cli.daemon.service import DaemonService

    root = Path(os.environ["LLM_COORD_STATE_HOME"]).parent / "physical-authority" / "v1"

    class DisposableAuthorityService(DaemonService):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("authority_root", root)
            super().__init__(*args, **kwargs)

    main.DaemonService = DisposableAuthorityService
