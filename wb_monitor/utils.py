"""Мелкие общие утилиты."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Текущее время UTC без таймзоны.

    В БД все даты naive-UTC (так было с первой версии схемы), а
    datetime.utcnow() с Python 3.12 кидает DeprecationWarning. Одно место,
    где мы приводим aware-время к тому naive-виду, который ждут модели.
    """
    return datetime.now(UTC).replace(tzinfo=None)
