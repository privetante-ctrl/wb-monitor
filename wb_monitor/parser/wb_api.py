"""Получение цены/остатка по nmId через публичный card-эндпоинт WB.

ВАЖНО: разбор ответа (_parse_product ниже) написан под формат
card.wb.ru/cards/v2/detail по состоянию на середину 2026. Если у тебя в
рабочем скрипте другой эндпоинт/разбор — замени тело _parse_product и
CARD_URL/CARD_PARAMS на свои, сигнатура fetch_sku при этом не меняется,
и весь остальной код (jobs, отчёты) ничего не заметит.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from wb_monitor.parser.client import HttpClient, WBRequestError

logger = logging.getLogger(__name__)

CARD_URL = "https://card.wb.ru/cards/v2/detail"
# dest=-1257786 — Москва; curr=rub; appType=1 — desktop
CARD_PARAMS = {"appType": "1", "curr": "rub", "dest": "-1257786", "spp": "30"}


class SkuNotFound(WBRequestError):
    """WB ответил, но товара с таким nmId в выдаче нет (снят с продажи/опечатка)."""


@dataclass(frozen=True)
class SkuData:
    nm_id: str
    price: float                  # цена без скидки, руб.
    discount_price: float | None  # цена со скидкой (как видит покупатель), руб.
    stock_qty: int                # суммарный остаток по всем размерам/складам
    name: str = ""


async def fetch_sku(client: HttpClient, nm_id: str) -> SkuData:
    """Одно измерение цены/остатка по nmId. Бросает WBRequestError/SkuNotFound."""
    data = await client.get_json(CARD_URL, params={**CARD_PARAMS, "nm": str(nm_id)})
    products = (data.get("data") or {}).get("products") or []
    if not products:
        raise SkuNotFound(f"nmId {nm_id}: пустой ответ card-эндпоинта")
    return _parse_product(str(nm_id), products[0])


def _parse_product(nm_id: str, product: dict) -> SkuData:
    """Разбор одного product из ответа v2/detail. Цены приходят в копейках."""
    stock_qty = 0
    basic_kopecks: int | None = None
    total_kopecks: int | None = None

    for size in product.get("sizes") or []:
        for stock in size.get("stocks") or []:
            stock_qty += int(stock.get("qty") or 0)
        price_block = size.get("price")
        if price_block and basic_kopecks is None:
            basic_kopecks = price_block.get("basic")
            total_kopecks = price_block.get("total")

    # fallback на старый формат (v1: priceU/salePriceU в корне product)
    if basic_kopecks is None:
        basic_kopecks = product.get("priceU")
        total_kopecks = product.get("salePriceU")

    if basic_kopecks is None:
        raise WBRequestError(f"nmId {nm_id}: в ответе нет блока цены (изменилась разметка WB?)")

    price = basic_kopecks / 100
    discount_price = total_kopecks / 100 if total_kopecks is not None else None
    if discount_price is not None and discount_price >= price:
        discount_price = None  # скидки нет

    return SkuData(
        nm_id=nm_id,
        price=price,
        discount_price=discount_price,
        stock_qty=stock_qty,
        name=str(product.get("name") or ""),
    )
