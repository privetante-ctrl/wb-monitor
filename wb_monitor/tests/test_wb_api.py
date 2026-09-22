"""Разбор ответа card-эндпоинта: v2, v1 и то, что между ними."""

from __future__ import annotations

import pytest

from wb_monitor.parser.client import WBRequestError
from wb_monitor.parser.wb_api import _parse_product

V2 = {
    "name": "Носки шерстяные",
    "brand": "БрендА",
    "sizes": [
        {
            "price": {"basic": 45000, "product": 36900, "total": 36900},
            "stocks": [{"qty": 12}, {"qty": 8}],
        },
        {"price": {"basic": 45000, "product": 36900}, "stocks": [{"qty": 5}]},
    ],
}


def test_v2_price_and_stock():
    data = _parse_product("1", V2)
    assert (data.price, data.discount_price) == (450.0, 369.0)
    assert data.stock_qty == 25  # 12 + 8 + 5
    assert (data.name, data.brand) == ("Носки шерстяные", "БрендА")


def test_v1_fallback():
    data = _parse_product("1", {"priceU": 100000, "salePriceU": 80000, "name": "X"})
    assert (data.price, data.discount_price, data.stock_qty) == (1000.0, 800.0, 0)


def test_total_quantity_used_when_no_stocks_block():
    product = {"sizes": [{"price": {"basic": 10000, "product": 10000}}], "totalQuantity": 7}
    assert _parse_product("1", product).stock_qty == 7


def test_no_discount_when_sale_price_not_lower():
    product = {"sizes": [{"price": {"basic": 10000, "product": 10000}, "stocks": []}]}
    assert _parse_product("1", product).discount_price is None


def test_zero_stock_is_zero_not_missing():
    product = {"sizes": [{"price": {"basic": 10000, "product": 9000}, "stocks": []}]}
    data = _parse_product("1", product)
    assert data.stock_qty == 0 and data.discount_price == 90.0


def test_missing_price_block_raises():
    with pytest.raises(WBRequestError):
        _parse_product("42", {"name": "без цены", "sizes": [{"stocks": [{"qty": 1}]}]})
