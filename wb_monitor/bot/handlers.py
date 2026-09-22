"""Текстовые админ-команды для работы с ЧУЖИМИ клиентами.

Кнопочное меню (bot/menu.py) закрывает сценарий «слежу за своими
конкурентами» и /start с ним же. Здесь — то, что нужно, когда сервис
продаётся дальше: завести клиента, повесить на него SKU, выслать отчёт.
Доступно только из админского чата.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import select

from sqlalchemy.exc import IntegrityError

from wb_monitor.models import Client, Tier, TrackedSKU
from wb_monitor.scheduler.jobs import SessionFactory, weekly_report_job
from wb_monitor.state import AdminRef

logger = logging.getLogger(__name__)

HELP = (
    "Команды для работы с клиентами (свои товары — кнопками в /menu):\n"
    "/add_client <имя>; <chat_id>; <тариф>; <цена> — новый клиент\n"
    "   тариф: basic | standard | pro\n"
    "/add_sku <client_id> <nmId> <название> [own] — новый SKU (own = свой товар)\n"
    "/clients — список клиентов\n"
    "/skus <client_id> — SKU клиента\n"
    "/report <client_id> — прислать отчёт за прошлую неделю сейчас"
)


def create_router(session_factory: SessionFactory, admin: AdminRef, reports_dir: str) -> Router:
    router = Router()

    def is_admin(message: Message) -> bool:
        return admin.is_set and message.chat.id == admin.chat_id

    @router.message(Command("admin"))
    async def cmd_admin(message: Message) -> None:
        if is_admin(message):
            await message.answer(HELP)

    @router.message(Command("add_client"))
    async def cmd_add_client(message: Message, command: CommandObject) -> None:
        if not is_admin(message):
            return
        try:
            name, chat_id, tier, price = [p.strip() for p in (command.args or "").split(";")]
            tier = Tier(tier.lower()).value
            client = Client(
                name=name, telegram_chat_id=int(chat_id), tier=tier, price_rub=float(price)
            )
        except (ValueError, TypeError):
            await message.answer(
                "Формат: /add_client Имя; chat_id; basic|standard|pro; цена_руб"
            )
            return
        async with session_factory() as session:
            session.add(client)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                await message.answer(f"Клиент с chat_id {chat_id} уже заведён.")
                return
            await message.answer(f"Клиент #{client.id} «{name}» добавлен ({tier}).")

    @router.message(Command("add_sku"))
    async def cmd_add_sku(message: Message, command: CommandObject) -> None:
        if not is_admin(message):
            return
        parts = (command.args or "").split()
        try:
            client_id, nm_id = int(parts[0]), parts[1]
            is_own = parts[-1].lower() == "own"
            display_name = " ".join(parts[2 : -1 if is_own else len(parts)])
            if not display_name:
                raise ValueError
        except (ValueError, IndexError):
            await message.answer("Формат: /add_sku <client_id> <nmId> <название> [own]")
            return
        async with session_factory() as session:
            if await session.get(Client, client_id) is None:
                await message.answer(f"Клиента #{client_id} нет.")
                return
            sku = TrackedSKU(
                client_id=client_id, wb_article_id=nm_id,
                display_name=display_name, is_own_product=is_own,
            )
            session.add(sku)
            await session.commit()
            await message.answer(
                f"SKU #{sku.id} «{display_name}» ({'свой' if is_own else 'конкурент'}) добавлен."
            )

    @router.message(Command("clients"))
    async def cmd_clients(message: Message) -> None:
        if not is_admin(message):
            return
        async with session_factory() as session:
            clients = (await session.execute(select(Client).order_by(Client.id))).scalars().all()
        if not clients:
            await message.answer("Клиентов пока нет.")
            return
        lines = [
            f"#{c.id} {c.name} — {c.tier}, {c.price_rub:.0f} руб., "
            f"chat {c.telegram_chat_id}{'' if c.is_active else ' (выкл)'}"
            for c in clients
        ]
        await message.answer("\n".join(lines))

    @router.message(Command("skus"))
    async def cmd_skus(message: Message, command: CommandObject) -> None:
        if not is_admin(message):
            return
        try:
            client_id = int((command.args or "").strip())
        except ValueError:
            await message.answer("Формат: /skus <client_id>")
            return
        async with session_factory() as session:
            skus = (
                await session.execute(
                    select(TrackedSKU).where(TrackedSKU.client_id == client_id)
                )
            ).scalars().all()
        if not skus:
            await message.answer("SKU не найдены.")
            return
        lines = [
            f"#{s.id} {s.display_name} (nmId {s.wb_article_id}, "
            f"{'свой' if s.is_own_product else 'конкурент'})"
            f"{'' if s.is_active else ' (выкл)'}"
            for s in skus
        ]
        await message.answer("\n".join(lines))

    @router.message(Command("report"))
    async def cmd_report(message: Message, command: CommandObject) -> None:
        if not is_admin(message):
            return
        try:
            client_id = int((command.args or "").strip())
        except ValueError:
            await message.answer("Формат: /report <client_id>")
            return
        paths = await weekly_report_job(
            session_factory, message.bot, reports_dir, client_id=client_id
        )
        await message.answer(
            f"Отчёт отправлен клиенту #{client_id}." if paths else "Отчёт не собрался, смотри логи."
        )

    return router
