"""Double Ctrl+C is bounded in time and shared across input surfaces."""

from __future__ import annotations

import pytest

from llm_cli.cli import interrupts


def test_exit_window_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(interrupts.time, "monotonic", lambda: now[0])
    state = interrupts.InterruptState()
    assert isinstance(state.press(), interrupts.InputInterrupted)
    now[0] += 2.1
    assert not state.armed
    assert isinstance(state.press(), interrupts.InputInterrupted)
    now[0] += 0.5
    assert isinstance(state.press(), interrupts.ExitRequested)


def test_reset_and_already_counted_interrupts() -> None:
    state = interrupts.InterruptState()
    first = state.press()
    assert isinstance(first, interrupts.InputInterrupted)
    state.handle(first)
    assert isinstance(state.press(), interrupts.ExitRequested)
    state.reset()
    assert not state.armed
    state.handle(KeyboardInterrupt())
    with pytest.raises(interrupts.ExitRequested):
        state.handle(KeyboardInterrupt())
