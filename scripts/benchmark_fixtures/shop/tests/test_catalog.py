import pytest
from shop.catalog import Catalog, Product


def test_products_are_found_by_sku() -> None:
    catalog = Catalog([Product("tea", "Tea", 450)])
    assert catalog.get("tea").name == "Tea"


def test_duplicate_skus_are_refused() -> None:
    catalog = Catalog([Product("tea", "Tea", 450)])
    with pytest.raises(ValueError):
        catalog.add(Product("tea", "Green tea", 500))


def test_unknown_skus_are_refused() -> None:
    with pytest.raises(KeyError):
        Catalog().get("tea")
