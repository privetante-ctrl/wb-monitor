"""Отправка алертов и файлов клиентам/админу.

Все функции принимают bot параметром (а не создают его сами) — так job'ы
шарят один экземпляр с polling-процессом, а в тестах подставляется фейк.
Ошибки отправки не роняют job: клиент с заблокированным ботом не должен
останавливать проход по остальным.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class BotLike(Protocol):
    """Минимальный интерфейс aiogram.Bot, который нам нужен (для тестов)."""

    async def send_message(self, chat_id: int, text: str) -> object: ...
    async def send_document(self, chat_id: int, document: object, caption: str | None = None) -> object: ...


async def send_client_alert(bot: BotLike, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(chat_id, text)
        return True
    except Exception:
        logger.exception("Не удалось отправить алерт в чат %s", chat_id)
        return False


async def send_admin_alert(bot: BotLike, admin_chat_id: int, text: str) -> bool:
    if not admin_chat_id:
        logger.error("ADMIN_CHAT_ID не задан, технический алерт потерян: %s", text)
        return False
    return await send_client_alert(bot, admin_chat_id, f"⚙️ [wb_monitor] {text}")


async def send_report_file(
    bot: BotLike, chat_id: int, file_path: str | Path, caption: str
) -> bool:
    from aiogram.types import FSInputFile  # локальный импорт: тестам aiogram не нужен

    try:
        await bot.send_document(chat_id, FSInputFile(str(file_path)), caption=caption)
        return True
    except Exception:
        logger.exception("Не удалось отправить отчёт %s в чат %s", file_path, chat_id)
        return False
