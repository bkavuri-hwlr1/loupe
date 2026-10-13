"""Products the shop sells."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price_cents: int


class Catalog:
    def __init__(self, products: list[Product] | None = None) -> None:
        self._products: dict[str, Product] = {}
        for product in products or []:
            self.add(product)

    def add(self, product: Product) -> None:
        if product.sku in self._products:
            raise ValueError(f"duplicate sku {product.sku}")
        self._products[product.sku] = product

    def get(self, sku: str) -> Product:
        try:
            return self._products[sku]
        except KeyError:
            raise KeyError(f"unknown sku {sku}") from None
