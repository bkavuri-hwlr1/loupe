import pytest
from shop.cart import Cart
from shop.catalog import Catalog, Product
from shop.pricing import line_total


def _cart() -> Cart:
    return Cart(Catalog([Product("tea", "Tea", 450), Product("mug", "Mug", 1299)]))


def test_lines_are_priced_in_cents() -> None:
    assert line_total(450, 3) == 1350


def test_adding_the_same_product_adds_quantity() -> None:
    cart = _cart()
    cart.add("tea")
    cart.add("tea", 2)
    assert cart.lines == {"tea": 3}


def test_subtotal_sums_every_line() -> None:
    cart = _cart()
    cart.add("tea", 2)
    cart.add("mug")
    assert cart.subtotal() == 2199
    assert cart.total() == 2199


def test_removed_lines_are_not_priced() -> None:
    cart = _cart()
    cart.add("tea")
    cart.remove("tea")
    assert cart.subtotal() == 0


def test_quantities_must_be_positive() -> None:
    with pytest.raises(ValueError):
        _cart().add("tea", 0)


def test_unknown_products_are_refused() -> None:
    with pytest.raises(KeyError):
        _cart().add("cake")
