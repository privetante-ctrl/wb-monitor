"""Схема БД для WB-мониторинга (подписочная модель).

Основа — исходный models.py, адаптированный с точечными правками
(каждая — реальная проблема, не рефакторинг ради рефакторинга):

1. `Client.telegram_chat_id` -> BigInteger: chat_id в Telegram уже сейчас
   превышает 2^31 (например, у супергрупп/новых аккаунтов). В SQLite это
   не стреляет, но при переезде на Postgres Integer молча обрежется.
2. Индексы на `price_snapshots (sku_id, checked_at)` и
   `alerts (sku_id, alert_type, sent_at)` — по ним ходят alert_check_job
   (последние 2 снепшота, последний алерт данного типа) и отчёт (окно 7 дней).
3. `Alert.client_id` стал nullable: технический алерт SCRAPER_BROKEN не
   привязан к клиенту, он идёт админу.
4. `Alert.snapshot_id` (nullable FK) — «на каком снепшоте сработал триггер».
   Это ключ антидубля: повторный прогон alert_check_job по тем же данным
   видит, что алерт для этого снепшота уже отправлен, и молчит.
5. Новая таблица `ParseRun` — статистика одного прохода parse_job
   (сколько SKU, сколько ошибок, сколько нулей). На ней стоит
   health_check_job: «>30% ошибок за проход = сломан парсер, а не рынок».

Движок/сессии — в db.py, здесь только модели.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Tier(str, Enum):
    BASIC = "basic"        # 5 SKU, только Excel-отчёт
    STANDARD = "standard"  # 10-15 SKU, + Telegram-алерты
    PRO = "pro"            # 20+ SKU, + базовая юнит-экономика


# тарифы, для которых включены Telegram-алерты (basic получает только отчёт)
ALERT_TIERS = {Tier.STANDARD.value, Tier.PRO.value}


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    tier: Mapped[str] = mapped_column(String(20), default=Tier.BASIC.value)
    price_rub: Mapped[float] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    skus: Mapped[list["TrackedSKU"]] = relationship(back_populates="client")


class TrackedSKU(Base):
    """Один отслеживаемый товар — свой или конкурента."""

    __tablename__ = "tracked_skus"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    wb_article_id: Mapped[str] = mapped_column(String(20))  # nmId в терминах WB
    display_name: Mapped[str] = mapped_column(String(200))
    is_own_product: Mapped[bool] = mapped_column(Boolean, default=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # снять с отслеживания, не удаляя историю

    client: Mapped["Client"] = relationship(back_populates="skus")
    snapshots: Mapped[list["PriceSnapshot"]] = relationship(back_populates="sku")


class PriceSnapshot(Base):
    """Одно измерение цены/остатка/позиции на конкретный момент."""

    __tablename__ = "price_snapshots"
    __table_args__ = (Index("ix_snapshots_sku_checked", "sku_id", "checked_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("tracked_skus.id"))
    checked_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    price: Mapped[float] = mapped_column(Float)
    discount_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stock_qty: Mapped[int] = mapped_column(Integer)
    search_position: Mapped[int | None] = mapped_column(Integer, nullable=True)  # опционально, дороже парсить

    sku: Mapped["TrackedSKU"] = relationship(back_populates="snapshots")

    @property
    def effective_price(self) -> float:
        """Цена «как видит покупатель»: со скидкой, если она есть."""
        return self.discount_price if self.discount_price is not None else self.price


class AlertType(str, Enum):
    PRICE_DROP = "price_drop"
    PRICE_UP = "price_up"
    STOCK_ZERO = "stock_zero"
    STOCK_BACK = "stock_back"
    POSITION_JUMP = "position_jump"
    SCRAPER_BROKEN = "scraper_broken"  # техническое — уходит тебе, не клиенту


class Alert(Base):
    """Лог отправленных уведомлений — чтобы не спамить повторно один и тот же триггер."""

    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_sku_type_sent", "sku_id", "alert_type", "sent_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    sku_id: Mapped[int | None] = mapped_column(ForeignKey("tracked_skus.id"), nullable=True)
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("price_snapshots.id"), nullable=True
    )
    alert_type: Mapped[str] = mapped_column(String(30))
    message: Mapped[str] = mapped_column(String(500))
    sent_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ParseRun(Base):
    """Статистика одного прохода parse_job — сырьё для health_check_job."""

    __tablename__ = "parse_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_skus: Mapped[int] = mapped_column(Integer, default=0)
    ok_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    zero_stock_count: Mapped[int] = mapped_column(Integer, default=0)
    # выставляется, если доля ошибок/нулей за проход выше порога из thresholds.py;
    # alert_check_job такой проход пропускает (не шлёт клиентам ложные "остаток 0")
    is_suspicious: Mapped[bool] = mapped_column(Boolean, default=False)
