"""Price arithmetic, in whole cents."""

from __future__ import annotations


def line_total(unit_cents: int, quantity: int) -> int:
    return unit_cents * quantity
