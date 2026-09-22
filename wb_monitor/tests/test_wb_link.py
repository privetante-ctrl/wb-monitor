"""Из чего бот должен уметь достать артикул: ссылки, числа, текст вокруг."""

from __future__ import annotations

import pytest

from wb_monitor.parser.wb_link import BadArticle, card_url, extract_nm_id


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://www.wildberries.ru/catalog/18234561/detail.aspx", "18234561"),
        ("https://wildberries.ru/catalog/221903451/detail.aspx?targetUrl=GP", "221903451"),
        ("http://www.wildberries.ru/catalog/12345678/detail.aspx#comments", "12345678"),
        ("18234561", "18234561"),
        ("  18234561  ", "18234561"),
        ("18 234 561", "18234561"),
        ("18-234-561", "18234561"),
        ("глянь https://www.wildberries.ru/catalog/999888777/detail.aspx", "999888777"),
        ("артикул 22190345 у конкурента", "22190345"),
        ("https://www.wildberries.ru/product?card=87654321", "87654321"),
    ],
)
def test_extract(text, expected):
    assert extract_nm_id(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "привет", "1234", "не ссылка и не число"])
def test_rejects_garbage(text):
    with pytest.raises(BadArticle):
        extract_nm_id(text)


def test_catalog_id_wins_over_query_noise():
    """В ссылке из поиска есть и /catalog/<nmId>/, и посторонние числа."""
    url = "https://www.wildberries.ru/catalog/18234561/detail.aspx?size=123456&card=777777"
    assert extract_nm_id(url) == "18234561"


def test_card_url_roundtrip():
    assert extract_nm_id(card_url("18234561")) == "18234561"
