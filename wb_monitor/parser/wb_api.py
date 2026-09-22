"""Получение цены/остатка по nmId через публичный card-эндпоинт WB.

WB периодически переносит карточный эндпоинт (card.wb.ru -> u-card.wb.ru)
и меняет разметку ответа. Поэтому здесь:

* список хостов, которые пробуются по очереди, с запоминанием рабочего —
  переезд эндпоинта не роняет сервис и не требует правок кода;
* разбор, который понимает и v2 (sizes[].price.{basic,product,total}),
  и v1 (priceU/salePriceU), и берёт остаток либо из stocks, либо из
  totalQuantity.

Сигнатура fetch_sku фиксирована: jobs/отчёты ничего не знают про формат.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from wb_monitor.parser.client import HttpClient, WBRequestError

logger = logging.getLogger(__name__)

# Пробуются по порядку; первый ответивший запоминается в _preferred_url.
CARD_URLS = (
    "https://card.wb.ru/cards/v2/detail",
    "https://u-card.wb.ru/cards/v2/detail",
    "https://card.wb.ru/cards/detail",
)
# dest=-1257786 — Москва; curr=rub; appType=1 — desktop
CARD_PARAMS = {"appType": "1", "curr": "rub", "dest": "-1257786", "spp": "30"}

_preferred_url: str | None = None


class SkuNotFound(WBRequestError):
    """WB ответил, но товара с таким nmId в выдаче нет (снят с продажи/опечатка)."""


@dataclass(frozen=True)
class SkuData:
    nm_id: str
    price: float                  # цена без скидки, руб.
    discount_price: float | None  # цена со скидкой (как видит покупатель), руб.
    stock_qty: int                # суммарный остаток по всем размерам/складам
    name: str = ""
    brand: str = ""


def _candidate_urls() -> list[str]:
    """Рабочий хост — первым, остальные как запасные."""
    if _preferred_url is None:
        return list(CARD_URLS)
    return [_preferred_url] + [u for u in CARD_URLS if u != _preferred_url]


async def fetch_sku(client: HttpClient, nm_id: str) -> SkuData:
    """Одно измерение цены/остатка по nmId. Бросает WBRequestError/SkuNotFound."""
    global _preferred_url

    params = {**CARD_PARAMS, "nm": str(nm_id)}
    last_error: Exception | None = None

    for url in _candidate_urls():
        try:
            data = await client.get_json(url, params=params)
        except WBRequestError as exc:
            last_error = exc
            logger.debug("card-эндпоинт %s не ответил: %s", url, exc)
            continue

        products = (data.get("data") or {}).get("products") or []
        if not products:
            # Ответ валидный, но товара нет — другой хост не поможет.
            _preferred_url = url
            raise SkuNotFound(f"nmId {nm_id}: товара нет в выдаче WB (снят с продажи?)")

        if url != _preferred_url:
            logger.info("Рабочий card-эндпоинт: %s", url)
            _preferred_url = url
        return _parse_product(str(nm_id), products[0])

    raise WBRequestError(f"nmId {nm_id}: ни один card-эндпоинт не ответил ({last_error})")


def _first_price_block(product: dict) -> dict | None:
    """Первый непустой блок цены среди размеров (v2)."""
    for size in product.get("sizes") or []:
        price_block = size.get("price")
        if price_block:
            return price_block
    return None


def _stock_from_sizes(product: dict) -> int | None:
    """Сумма остатков по складам всех размеров. None — блока stocks вообще нет."""
    found = False
    total = 0
    for size in product.get("sizes") or []:
        stocks = size.get("stocks")
        if stocks is None:
            continue
        found = True
        for stock in stocks:
            total += int(stock.get("qty") or 0)
    return total if found else None


def _parse_product(nm_id: str, product: dict) -> SkuData:
    """Разбор одного product из ответа card-эндпоинта. Цены приходят в копейках."""
    basic_kopecks: int | None = None
    total_kopecks: int | None = None

    price_block = _first_price_block(product)
    if price_block:
        basic_kopecks = price_block.get("basic")
        # product — цена с учётом всех скидок, total — то же поле в старых ответах
        total_kopecks = price_block.get("product") or price_block.get("total")

    if basic_kopecks is None:  # fallback на v1: priceU/salePriceU в корне product
        basic_kopecks = product.get("priceU")
        total_kopecks = product.get("salePriceU")

    if basic_kopecks is None:
        raise WBRequestError(f"nmId {nm_id}: в ответе нет блока цены (изменилась разметка WB?)")

    price = basic_kopecks / 100
    discount_price = total_kopecks / 100 if total_kopecks is not None else None
    if discount_price is not None and discount_price >= price:
        discount_price = None  # скидки нет

    stock_qty = _stock_from_sizes(product)
    if stock_qty is None:
        stock_qty = int(product.get("totalQuantity") or 0)

    return SkuData(
        nm_id=nm_id,
        price=price,
        discount_price=discount_price,
        stock_qty=stock_qty,
        name=str(product.get("name") or ""),
        brand=str(product.get("brand") or ""),
    )
