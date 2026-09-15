"""Обёртка над httpx: рандомизированная задержка, retry с backoff, прокси-ready.

Точка расширения под прокси: конструктор принимает `proxy` (URL строкой).
Когда WB начнёт банить IP — задаёшь PROXY_URL в .env, и все вызовы
продолжают работать без единой правки в wb_api.py или jobs.py.
Если понадобится пул прокси с ротацией — меняется только этот класс.
"""

from __future__ import annotations

import asyncio
import logging
import random

import httpx

logger = logging.getLogger(__name__)

# Без браузерного User-Agent card.wb.ru периодически отдаёт 498/пустые ответы
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# на этих статусах ретраимся (временные), на остальных 4xx — нет смысла
RETRYABLE_STATUS = {429, 498, 500, 502, 503, 504}


class WBRequestError(Exception):
    """Запрос не удался после всех retry — считаем SKU «ошибочным» в этом проходе."""


class HttpClient:
    """Async httpx-клиент с вежливыми паузами между запросами.

    Паузу делает сам клиент перед КАЖДЫМ запросом, поэтому вызывающий код
    (parse_job) может просто идти по SKU последовательно — «не спалиться»
    обеспечивается здесь, в одном месте.
    """

    def __init__(
        self,
        *,
        delay_range: tuple[float, float] = (0.5, 1.5),
        retries: int = 3,
        timeout: float = 10.0,
        proxy: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._delay_range = delay_range
        self._retries = retries
        self._client = httpx.AsyncClient(
            timeout=timeout,
            proxy=proxy,
            headers=headers or DEFAULT_HEADERS,
            follow_redirects=True,
        )

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, url: str, params: dict | None = None) -> dict:
        """GET с задержкой перед запросом и retry с экспоненциальным backoff.

        Backoff: 1s, 2s, 4s, ... (+ немного джиттера, чтобы не биться в ритм).
        """
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            await asyncio.sleep(random.uniform(*self._delay_range))
            try:
                response = await self._client.get(url, params=params)
                if response.status_code in RETRYABLE_STATUS:
                    raise WBRequestError(f"HTTP {response.status_code} от {url}")
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.TransportError, WBRequestError) as exc:
                last_error = exc
                if attempt < self._retries:
                    backoff = 2**attempt + random.uniform(0, 0.5)
                    logger.warning(
                        "Запрос %s не удался (%s), retry %d/%d через %.1fс",
                        url, exc, attempt + 1, self._retries, backoff,
                    )
                    await asyncio.sleep(backoff)
            except httpx.HTTPStatusError as exc:
                # невременная ошибка (404 и т.п.) — ретраить бессмысленно
                raise WBRequestError(f"HTTP {exc.response.status_code} от {url}") from exc
            except ValueError as exc:  # response.json() на не-JSON теле
                raise WBRequestError(f"Не-JSON ответ от {url}") from exc
        raise WBRequestError(f"Запрос {url} не удался после {self._retries} retry: {last_error}")
