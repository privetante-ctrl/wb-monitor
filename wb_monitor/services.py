"""Общий контейнер зависимостей для бота и job'ов.

Бот-хендлеры должны уметь то же, что и планировщик (запустить парс,
собрать отчёт), и при этом не создавать второй http-клиент и второй
движок БД. Поэтому всё живёт в одном объекте, который создаётся при
старте процесса и передаётся в роутеры.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from wb_monitor.config import Settings
from wb_monitor.parser.client import HttpClient
from wb_monitor.scheduler.jobs import SessionFactory
from wb_monitor.state import AdminRef


@dataclass
class Services:
    cfg: Settings
    session_factory: SessionFactory
    http: HttpClient
    admin: AdminRef
    # чтобы «Проверить сейчас» из бота и плановый проход не пошли одновременно
    parse_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def reports_dir(self) -> str:
        return self.cfg.reports_dir
