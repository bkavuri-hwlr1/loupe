"""One double-interrupt gesture shared by the editor and task viewer."""

from __future__ import annotations

import time

EXIT_HINT = "Press Ctrl+C again within 2 seconds to exit."


class ExitRequested(BaseException):
    """Leave the shell through its normal session cleanup."""


class InputInterrupted(KeyboardInterrupt):
    """An interrupt already counted by the keyboard reader."""


class InterruptState:
    def __init__(self) -> None:
        self._last: float | None = None

    @property
    def armed(self) -> bool:
        return self._last is not None and time.monotonic() - self._last <= 2

    def reset(self) -> None:
        self._last = None

    def press(self) -> InputInterrupted | ExitRequested:
        if self.armed:
            return ExitRequested()
        self._last = time.monotonic()
        return InputInterrupted()

    def handle(self, error: KeyboardInterrupt) -> None:
        if not isinstance(error, InputInterrupted):
            result = self.press()
            if isinstance(result, ExitRequested):
                raise result
