"""Движок и фабрика сессий (вынесено из models.py, чтобы модели остались чистыми)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from wb_monitor.models import Base


def make_engine(db_url: str) -> AsyncEngine:
    return create_async_engine(db_url)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Создать все таблицы (идемпотентно)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
