from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from wb_monitor.models import Base, Client, PriceSnapshot, Tier, TrackedSKU


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite://")  # in-memory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def client_and_sku(session_factory):
    """Клиент на тарифе standard (алерты включены) с одним SKU."""
    async with session_factory() as session:
        client = Client(
            name="Тест", telegram_chat_id=111, tier=Tier.STANDARD.value, price_rub=1500
        )
        session.add(client)
        await session.flush()
        sku = TrackedSKU(
            client_id=client.id, wb_article_id="12345678", display_name="Носки тестовые"
        )
        session.add(sku)
        await session.commit()
        return client, sku


class FakeBot:
    """Фейковый бот: копит отправленные сообщения вместо похода в Telegram."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id: int, document, caption=None) -> None:
        self.messages.append((chat_id, f"<document {document}>"))


async def add_snapshots(session_factory, sku_id: int, values: list[tuple[float, int]]):
    """Добавить снепшоты (price, stock_qty) с возрастающим временем."""
    base = datetime(2026, 7, 20, 12, 0, 0)
    async with session_factory() as session:
        existing = (
            await session.execute(
                select(func.count())
                .select_from(PriceSnapshot)
                .where(PriceSnapshot.sku_id == sku_id)
            )
        ).scalar_one()
        for i, (price, stock) in enumerate(values):
            session.add(
                PriceSnapshot(
                    sku_id=sku_id,
                    checked_at=base + timedelta(hours=existing + i),
                    price=price,
                    stock_qty=stock,
                )
            )
        await session.commit()
