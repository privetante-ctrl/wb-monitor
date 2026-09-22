"""Извлечение nmId из того, что человек прислал боту.

Артикул в интерфейсе WB зовётся по-разному («артикул», «код товара»,
nmId в API), и копируют его тоже по-разному: то ссылкой из адресной
строки, то ссылкой из кнопки «Поделиться», то просто числом. Бот должен
понимать любой из этих вариантов — иначе «добавить товар» снова
превращается в инструкцию.
"""

from __future__ import annotations

import re

# .../catalog/<nmId>/detail.aspx — основной формат карточки
_CATALOG_RE = re.compile(r"/catalog/(\d{5,12})(?:/|\b)")
# ?card=<nmId>, ?nm=<nmId> — встречается в ссылках из поиска и приложения
_QUERY_RE = re.compile(r"[?&](?:card|nm|nmId)=(\d{5,12})\b", re.IGNORECASE)
# голое число (возможно с пробелами/дефисами, как его копируют из карточки)
_BARE_RE = re.compile(r"\b(\d{5,12})\b")


class BadArticle(ValueError):
    """Из присланного текста не вытащить артикул."""


def extract_nm_id(text: str) -> str:
    """Вернуть nmId строкой. Бросает BadArticle, если не нашлось."""
    raw = (text or "").strip()
    if not raw:
        raise BadArticle("пустая строка")

    for pattern in (_CATALOG_RE, _QUERY_RE):
        match = pattern.search(raw)
        if match:
            return match.group(1)

    # «18 234 561» / «18-234-561» — убираем разделители и пробуем как число
    compact = re.sub(r"[\s\-]", "", raw)
    if compact.isdigit() and 5 <= len(compact) <= 12:
        return compact

    match = _BARE_RE.search(raw)
    if match:
        return match.group(1)

    raise BadArticle(f"не похоже на артикул или ссылку WB: {raw[:80]!r}")


def card_url(nm_id: str) -> str:
    """Ссылка на карточку — чтобы в списке товаров можно было ткнуть и проверить."""
    return f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"
