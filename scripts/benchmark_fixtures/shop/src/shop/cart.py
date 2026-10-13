"""A shopping cart priced from the catalog."""

from __future__ import annotations

from shop.catalog import Catalog
from shop.pricing import line_total


class Cart:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.lines: dict[str, int] = {}

    def add(self, sku: str, quantity: int = 1) -> None:
        if quantity < 1:
            raise ValueError("quantity must be at least 1")
        self.catalog.get(sku)
        self.lines[sku] = self.lines.get(sku, 0) + quantity

    def remove(self, sku: str) -> None:
        self.lines.pop(sku, None)

    def subtotal(self) -> int:
        return sum(
            line_total(self.catalog.get(sku).price_cents, quantity)
            for sku, quantity in self.lines.items()
        )

    def total(self) -> int:
        return self.subtotal()
