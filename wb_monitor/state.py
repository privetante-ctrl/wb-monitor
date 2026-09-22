"""Настройки, которые сервис узнаёт сам во время работы (таблица app_settings).

Главная из них — admin_chat_id. Раньше его надо было найти через
@userinfobot и прописать в .env; теперь бот запоминает первого, кто нажал
/start, и дальше живёт с этим знанием сам. .env остаётся приоритетным:
если ADMIN_CHAT_ID там задан явно, он побеждает запомненное значение.
"""

from __future__ import annotations

import logging

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wb_monitor.models import AppSetting
from wb_monitor.utils import utcnow

logger = logging.getLogger(__name__)

ADMIN_CHAT_ID_KEY = "admin_chat_id"


async def get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(AppSetting, key)
    return row.value if row is not None else None


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    """UPSERT — чтобы не ловить гонку между «прочитал» и «записал»."""
    await session.execute(
        sqlite_insert(AppSetting)
        .values(key=key, value=value, updated_at=utcnow())
        .on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={"value": value, "updated_at": utcnow()},
        )
    )
    await session.commit()


class AdminRef:
    """Изменяемая ссылка на admin_chat_id.

    Job'ы и хендлеры создаются один раз при старте, а chat_id админа может
    появиться уже после (в момент первого /start). Поэтому передаём не
    число, а эту ссылку, и читаем `.chat_id` в момент отправки.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], env_value: int = 0) -> None:
        self._session_factory = session_factory
        self._env_value = env_value
        self._chat_id = env_value

    @property
    def chat_id(self) -> int:
        return self._chat_id

    @property
    def is_set(self) -> bool:
        return bool(self._chat_id)

    @property
    def is_pinned_by_env(self) -> bool:
        """ADMIN_CHAT_ID задан в .env — самозахват через /start запрещён."""
        return bool(self._env_value)

    async def load(self) -> int:
        """Поднять сохранённое значение из БД (если .env не задал своё)."""
        if self._env_value:
            return self._chat_id
        async with self._session_factory() as session:
            stored = await get_setting(session, ADMIN_CHAT_ID_KEY)
        if stored:
            self._chat_id = int(stored)
            logger.info("Админ восстановлен из БД: chat_id=%s", self._chat_id)
        return self._chat_id

    async def claim(self, chat_id: int) -> bool:
        """Назначить админом этот чат, если админа ещё нет. True — назначили."""
        if self.is_pinned_by_env or self.is_set:
            return False
        async with self._session_factory() as session:
            await set_setting(session, ADMIN_CHAT_ID_KEY, str(chat_id))
        self._chat_id = chat_id
        logger.info("Админом назначен chat_id=%s (первый /start)", chat_id)
        return True
