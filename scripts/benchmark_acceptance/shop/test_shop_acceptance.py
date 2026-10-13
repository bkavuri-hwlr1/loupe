"""Hidden checks of the shop experiment's combined result.

The agents never see these. Each test's name says which request it checks:
`discounts` and `stock` for the two requested changes, `both` for the two
working together, and `operator` for the edit made by hand while the agents
worked, which must survive.
"""

import importlib

import pytest
from shop.cart import Cart
from shop.catalog import Catalog, Product


def _catalog() -> Catalog:
    return Catalog([Product("tea", "Tea", 450), Product("mug", "Mug", 1299, stock=2)])


def _plain_catalog() -> Catalog:
    return Catalog([Product("tea", "Tea", 450), Product("mug", "Mug", 1299)])


def test_discounts_codes_are_listed() -> None:
    from shop.pricing import DISCOUNT_CODES

    assert DISCOUNT_CODES == {"SAVE10": 10, "HALF": 50}


def test_discounts_rounds_down_to_whole_cents() -> None:
    from shop.pricing import apply_discount

    assert apply_discount(999, 10) == 899
    assert apply_discount(1299, 50) == 649
    assert apply_discount(1000, 50) == 500


def test_discounts_code_reduces_total_not_subtotal() -> None:
    cart = Cart(_plain_catalog())
    cart.add("tea", 2)
    cart.apply_code("SAVE10")
    assert cart.subtotal() == 900
    assert cart.total() == 810


def test_discounts_codes_ignore_case() -> None:
    cart = Cart(_plain_catalog())
    cart.add("tea", 2)
    cart.apply_code("save10")
    assert cart.total() == 810


def test_discounts_unknown_code_is_refused() -> None:
    cart = Cart(_plain_catalog())
    with pytest.raises(ValueError):
        cart.apply_code("FREE")


def test_discounts_second_code_replaces_the_first() -> None:
    cart = Cart(_plain_catalog())
    cart.add("tea", 2)
    cart.apply_code("SAVE10")
    cart.apply_code("HALF")
    assert cart.total() == 450


def test_discounts_total_without_a_code_is_the_subtotal() -> None:
    cart = Cart(_plain_catalog())
    cart.add("mug")
    assert cart.total() == cart.subtotal() == 1299


def test_stock_is_unlimited_by_default() -> None:
    assert Product("tea", "Tea", 450).stock is None
    cart = Cart(_catalog())
    cart.add("tea", 1000)
    assert cart.lines["tea"] == 1000


def test_stock_limits_the_cart() -> None:
    from shop.catalog import OutOfStock

    cart = Cart(_catalog())
    cart.add("mug", 2)
    with pytest.raises(OutOfStock):
        cart.add("mug")


def test_stock_one_add_cannot_exceed_stock() -> None:
    from shop.catalog import OutOfStock

    with pytest.raises(OutOfStock):
        Cart(_catalog()).add("mug", 3)


def test_stock_error_is_a_value_error() -> None:
    from shop.catalog import OutOfStock

    assert issubclass(OutOfStock, ValueError)


def test_stock_zero_means_none_can_be_added() -> None:
    from shop.catalog import OutOfStock

    cart = Cart(Catalog([Product("cup", "Cup", 300, stock=0)]))
    with pytest.raises(OutOfStock):
        cart.add("cup")


def test_stock_restock_adds_to_stock() -> None:
    catalog = _catalog()
    catalog.restock("mug", 3)
    assert catalog.get("mug").stock == 5
    cart = Cart(catalog)
    cart.add("mug", 5)
    assert cart.lines["mug"] == 5


def test_stock_restock_keeps_unlimited_products_unlimited() -> None:
    catalog = _catalog()
    catalog.restock("tea", 3)
    assert catalog.get("tea").stock is None


def test_both_discounts_apply_to_stocked_products() -> None:
    cart = Cart(_catalog())
    cart.add("mug", 2)
    cart.apply_code("HALF")
    assert cart.subtotal() == 2598
    assert cart.total() == 1299


def test_both_a_refused_add_keeps_the_discount() -> None:
    from shop.catalog import OutOfStock

    cart = Cart(_catalog())
    cart.add("tea", 2)
    cart.apply_code("SAVE10")
    with pytest.raises(OutOfStock):
        cart.add("mug", 3)
    assert cart.total() == 810


def test_operator_describe_survives() -> None:
    describe = importlib.import_module("shop.cart").describe
    cart = Cart(_plain_catalog())
    cart.add("tea", 2)
    assert describe(cart) == "1 line(s), subtotal 900 cents"
