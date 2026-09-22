"""Основной интерфейс бота: всё делается кнопками, без CLI и без .env-возни.

Логика владельца: первый, кто нажал /start, становится админом (если
ADMIN_CHAT_ID не прибит в .env) и одновременно заводится как клиент №1 —
дальше он просто добавляет товары ссылкой и получает алерты с отчётами.
Обслуживание других клиентов никуда не делось: команды из handlers.py
продолжают работать, здесь — «одна кнопка» для владельца.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, func, select

from wb_monitor.bot.keyboards import (
    BTN_ADD,
    BTN_CHECK,
    BTN_HELP,
    BTN_LIST,
    BTN_REPORT,
    BTN_STATUS,
    confirm_delete,
    kind_choice,
    main_menu,
    sku_actions,
)
from wb_monitor.models import Alert, Client, ParseRun, PriceSnapshot, Tier, TrackedSKU
from wb_monitor.parser.wb_api import SkuNotFound, fetch_sku
from wb_monitor.parser.wb_link import BadArticle, card_url, extract_nm_id
from wb_monitor.parser.client import WBRequestError
from wb_monitor.services import Services
from wb_monitor.utils import utcnow

logger = logging.getLogger(__name__)

WELCOME_ADMIN = (
    "👋 Это твой монитор конкурентов на Wildberries.\n\n"
    "Всё делается кнопками снизу:\n"
    f"{BTN_ADD} — пришли ссылку на товар или артикул, я найду карточку\n"
    f"{BTN_LIST} — что сейчас отслеживается, там же удаление\n"
    f"{BTN_CHECK} — снять цены прямо сейчас, не дожидаясь расписания\n"
    f"{BTN_REPORT} — Excel-отчёт за последние 7 дней\n"
    f"{BTN_STATUS} — что с парсером и когда был последний проход\n\n"
    "Дальше я работаю сам: снимаю цены по расписанию, пишу, если "
    "конкурент уронил цену больше чем на 10% или обнулил остаток, "
    "и раз в неделю присылаю отчёт."
)

HELP_TEXT = (
    "Как пользоваться:\n\n"
    f"1. {BTN_ADD} → пришли ссылку вида "
    "https://www.wildberries.ru/catalog/12345678/detail.aspx или просто артикул. "
    "Можно прислать ссылку и без кнопки — я пойму.\n"
    "2. Отметь, свой это товар или конкурента. Свои нужны, чтобы в отчёте "
    "было с чем сравнивать.\n"
    f"3. {BTN_CHECK} — первый замер. Алерты начнутся со второго: "
    "сравнивать нужно с чем-то.\n\n"
    "Когда приходит алерт:\n"
    "📉 цена конкурента упала больше чем на 10%\n"
    "📈 выросла больше чем на 10%\n"
    "⛔ остаток обнулился — окно, чтобы продать своё\n"
    "✅ товар вернулся в наличие\n\n"
    "Пороги правятся в файле scheduler/thresholds.py, расписание — в .env."
)


class AddSku(StatesGroup):
    waiting_article = State()


def create_menu_router(services: Services) -> Router:
    router = Router()
    session_factory = services.session_factory

    # ------------------------------------------------------------ helpers

    async def get_client(chat_id: int) -> Client | None:
        async with session_factory() as session:
            return (
                await session.execute(
                    select(Client).where(Client.telegram_chat_id == chat_id)
                )
            ).scalar_one_or_none()

    async def ensure_owner_client(chat_id: int, name: str) -> Client:
        """Владелец бота — он же клиент №1: иначе некуда вешать товары."""
        existing = await get_client(chat_id)
        if existing is not None:
            return existing
        async with session_factory() as session:
            client = Client(
                name=name or "Владелец",
                telegram_chat_id=chat_id,
                tier=Tier.PRO.value,  # себе — полный набор: алерты + отчёты
                price_rub=0.0,
                is_active=True,
            )
            session.add(client)
            await session.commit()
            logger.info("Создан клиент-владелец #%s для чата %s", client.id, chat_id)
            return client

    def is_admin(chat_id: int) -> bool:
        return services.admin.is_set and chat_id == services.admin.chat_id

    async def deny_if_stranger(message: Message) -> Client | None:
        """Не админ и не клиент — отдаём chat_id и молчим дальше."""
        client = await get_client(message.chat.id)
        if client is None and not is_admin(message.chat.id):
            await message.answer(
                "Это бот мониторинга конкурентов на Wildberries.\n"
                f"Ваш chat_id: {message.chat.id} — передайте его менеджеру "
                "для подключения отчётов."
            )
            return None
        return client

    # -------------------------------------------------------------- /start

    @router.message(Command("start", "menu"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        chat_id = message.chat.id
        claimed = await services.admin.claim(chat_id)

        if is_admin(chat_id):
            client = await ensure_owner_client(
                chat_id, message.from_user.full_name if message.from_user else ""
            )
            text = WELCOME_ADMIN
            if claimed:
                text = (
                    "✅ Готово: этот чат теперь админский, технические "
                    "уведомления пойдут сюда.\n\n" + WELCOME_ADMIN
                )
            text += f"\n\nТы зарегистрирован как клиент #{client.id} «{client.name}»."
            await message.answer(text, reply_markup=main_menu(is_admin=True))
            return

        client = await deny_if_stranger(message)
        if client is None:
            return
        await message.answer(
            f"👋 {client.name}, вы подключены к мониторингу конкурентов на Wildberries.\n"
            "Отчёты приходят раз в неделю, срочные изменения — сразу.",
            reply_markup=main_menu(is_admin=False),
        )

    @router.message(F.text == BTN_HELP)
    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        if await deny_if_stranger(message) is None and not is_admin(message.chat.id):
            return
        await message.answer(HELP_TEXT, reply_markup=main_menu(is_admin(message.chat.id)))

    # -------------------------------------------------------- добавление SKU

    @router.message(F.text == BTN_ADD)
    async def start_add(message: Message, state: FSMContext) -> None:
        if not is_admin(message.chat.id):
            await deny_if_stranger(message)
            return
        await state.set_state(AddSku.waiting_article)
        await message.answer(
            "Пришли ссылку на товар с Wildberries или его артикул.\n\n"
            "Например:\nhttps://www.wildberries.ru/catalog/18234561/detail.aspx\n"
            "или просто 18234561\n\n"
            "Отменить — /menu"
        )

    async def handle_article(message: Message, state: FSMContext) -> None:
        """Достать nmId, сходить в WB и показать найденную карточку."""
        try:
            nm_id = extract_nm_id(message.text or "")
        except BadArticle:
            await message.answer(
                "Не вижу здесь артикула. Нужна ссылка на карточку WB "
                "или число из 5-12 цифр."
            )
            return

        async with session_factory() as session:
            client = (
                await session.execute(
                    select(Client).where(Client.telegram_chat_id == message.chat.id)
                )
            ).scalar_one_or_none()
            if client is not None:
                dup = (
                    await session.execute(
                        select(TrackedSKU).where(
                            TrackedSKU.client_id == client.id,
                            TrackedSKU.wb_article_id == nm_id,
                        )
                    )
                ).scalar_one_or_none()
                if dup is not None:
                    await state.clear()
                    await message.answer(
                        f"Артикул {nm_id} уже отслеживается: «{dup.display_name}».",
                        reply_markup=main_menu(True),
                    )
                    return

        wait = await message.answer(f"Ищу {nm_id} на WB…")
        try:
            data = await fetch_sku(services.http, nm_id)
        except SkuNotFound:
            await wait.edit_text(
                f"Товар {nm_id} не найден в выдаче WB. Проверь артикул — "
                "возможно, карточка снята с продажи."
            )
            return
        except WBRequestError as exc:
            logger.warning("Не удалось получить карточку %s: %s", nm_id, exc)
            await wait.edit_text(
                "WB сейчас не отвечает. Попробуй ещё раз через минуту — "
                f"{BTN_STATUS} покажет, если проблема не разовая."
            )
            return

        await state.update_data(nm_id=nm_id, name=data.name, brand=data.brand)
        await state.set_state(AddSku.waiting_article)

        price = data.discount_price or data.price
        await wait.edit_text(
            f"Нашёл: «{data.name or nm_id}»"
            + (f" ({data.brand})" if data.brand else "")
            + f"\nЦена: {price:,.0f} руб."
            + (f" (без скидки {data.price:,.0f})" if data.discount_price else "")
            + f"\nОстаток: {data.stock_qty} шт.\n{card_url(nm_id)}\n\n"
            "Это твой товар или конкурента?",
            reply_markup=kind_choice(nm_id),
        )

    @router.message(AddSku.waiting_article, F.text)
    async def add_by_state(message: Message, state: FSMContext) -> None:
        if not is_admin(message.chat.id):
            return
        await handle_article(message, state)

    @router.callback_query(F.data.startswith("add:"))
    async def finish_add(callback: CallbackQuery, state: FSMContext) -> None:
        _, action, nm_id = (callback.data or "").split(":", 2)
        await callback.answer()
        if not is_admin(callback.message.chat.id):
            return

        if action == "cancel":
            await state.clear()
            await callback.message.edit_text("Отменил.")
            return

        data = await state.get_data()
        name = data.get("name") or f"Артикул {nm_id}"
        is_own = action == "own"

        client = await ensure_owner_client(
            callback.message.chat.id,
            callback.from_user.full_name if callback.from_user else "",
        )
        async with session_factory() as session:
            sku = TrackedSKU(
                client_id=client.id,
                wb_article_id=nm_id,
                display_name=name[:200],
                is_own_product=is_own,
                category=(data.get("brand") or "")[:100] or None,
            )
            session.add(sku)
            await session.commit()
            sku_id = sku.id

        await state.clear()
        await callback.message.edit_text(
            f"✅ Добавлено: «{name}» — {'свой товар' if is_own else 'конкурент'} (#{sku_id}).\n\n"
            f"Нажми {BTN_CHECK}, чтобы снять первый замер прямо сейчас."
        )

    # ------------------------------------------------------------ список SKU

    @router.message(F.text == BTN_LIST)
    async def list_skus(message: Message) -> None:
        client = await deny_if_stranger(message)
        if client is None and not is_admin(message.chat.id):
            return
        if client is None:
            client = await ensure_owner_client(message.chat.id, "Владелец")

        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(TrackedSKU).where(TrackedSKU.client_id == client.id)
                    .order_by(TrackedSKU.is_own_product.desc(), TrackedSKU.id)
                )
            ).scalars().all()
            latest = {}
            for sku in rows:
                snap = (
                    await session.execute(
                        select(PriceSnapshot)
                        .where(PriceSnapshot.sku_id == sku.id)
                        .order_by(PriceSnapshot.checked_at.desc(), PriceSnapshot.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                latest[sku.id] = snap

        if not rows:
            await message.answer(
                f"Пока пусто. Нажми {BTN_ADD} и пришли ссылку на товар.",
                reply_markup=main_menu(is_admin(message.chat.id)),
            )
            return

        lines = []
        for sku in rows:
            snap = latest.get(sku.id)
            mark = "🏠" if sku.is_own_product else "🎯"
            state_mark = "" if sku.is_active else " ⏸"
            if snap is not None:
                lines.append(
                    f"{mark} #{sku.id} {sku.display_name}{state_mark}\n"
                    f"     {snap.effective_price:,.0f} руб., остаток {snap.stock_qty} шт."
                )
            else:
                lines.append(
                    f"{mark} #{sku.id} {sku.display_name}{state_mark}\n     ещё не замерялся"
                )

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        buttons = [
            [InlineKeyboardButton(
                text=f"#{sku.id} {sku.display_name[:28]}",
                callback_data=f"sku:open:{sku.id}",
            )]
            for sku in rows[:30]
        ]
        await message.answer(
            "Отслеживается:\n\n" + "\n".join(lines) + "\n\n🏠 свой · 🎯 конкурент"
            + ("\n\nНажми на товар, чтобы удалить или приостановить." if is_admin(message.chat.id) else ""),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if is_admin(message.chat.id) else None,
        )

    @router.callback_query(F.data.startswith("sku:"))
    async def sku_callbacks(callback: CallbackQuery) -> None:
        _, action, raw_id = (callback.data or "").split(":", 2)
        sku_id = int(raw_id)
        await callback.answer()
        if not is_admin(callback.message.chat.id):
            return

        async with session_factory() as session:
            sku = await session.get(TrackedSKU, sku_id)
            if sku is None:
                await callback.message.edit_text("Этого товара уже нет.")
                return

            if action == "open":
                await callback.message.answer(
                    f"«{sku.display_name}»\nАртикул {sku.wb_article_id}\n"
                    f"{'Свой товар' if sku.is_own_product else 'Конкурент'}\n"
                    f"{card_url(sku.wb_article_id)}",
                    reply_markup=sku_actions(sku.id, sku.is_active),
                )
            elif action == "toggle":
                sku.is_active = not sku.is_active
                await session.commit()
                await callback.message.edit_text(
                    f"«{sku.display_name}» — "
                    + ("снова отслеживается." if sku.is_active else "снят с отслеживания (история сохранена).")
                )
            elif action == "delete":
                await callback.message.edit_text(
                    f"Удалить «{sku.display_name}» вместе со всей историей цен?",
                    reply_markup=confirm_delete(sku.id),
                )
            elif action == "delete_no":
                await callback.message.edit_text(f"«{sku.display_name}» оставлен.")
            elif action == "delete_yes":
                name = sku.display_name
                await session.execute(delete(Alert).where(Alert.sku_id == sku_id))
                await session.execute(
                    delete(PriceSnapshot).where(PriceSnapshot.sku_id == sku_id)
                )
                await session.delete(sku)
                await session.commit()
                await callback.message.edit_text(f"🗑 «{name}» удалён.")

    # ------------------------------------------------------- проверить сейчас

    @router.message(F.text == BTN_CHECK)
    async def check_now(message: Message) -> None:
        if not is_admin(message.chat.id):
            await deny_if_stranger(message)
            return
        if services.parse_lock.locked():
            await message.answer("Проход уже идёт, подожди — сообщу, когда закончу.")
            return

        from wb_monitor.scheduler.jobs import alert_check_job, parse_job

        wait = await message.answer("Снимаю цены… это займёт примерно секунду на товар.")
        async with services.parse_lock:
            run = await parse_job(session_factory, services.http)
            alerts = 0
            if not run.is_suspicious:
                alerts = await alert_check_job(session_factory, message.bot)

        if run.total_skus == 0:
            await wait.edit_text(f"Отслеживать пока нечего — добавь товар кнопкой {BTN_ADD}.")
            return

        text = (
            f"Готово: {run.ok_count} из {run.total_skus} товаров снято"
            + (f", ошибок {run.failed_count}" if run.failed_count else "")
            + f".\nАлертов отправлено: {alerts}."
        )
        if run.is_suspicious:
            text += (
                "\n\n⚠️ Слишком много ошибок за проход — похоже, WB поменял "
                "разметку или банит запросы. Клиентские алерты по этому проходу "
                "подавлены, чтобы не слать ерунду."
            )
        elif alerts == 0:
            text += "\n\nИзменений, на которые стоит реагировать, нет."
        await wait.edit_text(text)

    # ---------------------------------------------------------------- отчёт

    @router.message(F.text == BTN_REPORT)
    async def report_now(message: Message) -> None:
        client = await deny_if_stranger(message)
        if client is None and not is_admin(message.chat.id):
            return
        if client is None:
            client = await ensure_owner_client(message.chat.id, "Владелец")

        from wb_monitor.scheduler.jobs import weekly_report_job

        # Пустой xlsx вместо ответа — худшее, что можно прислать на кнопку,
        # поэтому сначала проверяем, есть ли вообще о чём отчитываться.
        async with session_factory() as session:
            sku_ids = (
                await session.execute(
                    select(TrackedSKU.id).where(
                        TrackedSKU.client_id == client.id, TrackedSKU.is_active
                    )
                )
            ).scalars().all()
            measured = 0
            if sku_ids:
                measured = (
                    await session.execute(
                        select(func.count())
                        .select_from(PriceSnapshot)
                        .where(PriceSnapshot.sku_id.in_(sku_ids))
                    )
                ).scalar_one()

        if not sku_ids:
            await message.answer(
                f"Отслеживать пока нечего — добавь товар кнопкой {BTN_ADD}.",
                reply_markup=main_menu(is_admin(message.chat.id)),
            )
            return
        if measured == 0:
            await message.answer(
                f"Ещё ни одного замера — отчёт собирать не из чего. Нажми {BTN_CHECK}."
            )
            return

        wait = await message.answer("Собираю отчёт…")
        paths = await weekly_report_job(
            session_factory,
            message.bot,
            services.reports_dir,
            client_id=client.id,
            period_end=utcnow().date(),
        )
        if paths:
            await wait.delete()
        else:
            await wait.edit_text("Отчёт не собрался, смотри логи сервиса.")

    # --------------------------------------------------------------- статус

    @router.message(F.text == BTN_STATUS)
    async def status(message: Message) -> None:
        if not is_admin(message.chat.id):
            await deny_if_stranger(message)
            return

        async with session_factory() as session:
            last_run = (
                await session.execute(select(ParseRun).order_by(ParseRun.id.desc()).limit(1))
            ).scalar_one_or_none()
            sku_count = (
                await session.execute(
                    select(func.count()).select_from(TrackedSKU).where(TrackedSKU.is_active)
                )
            ).scalar_one()
            week_alerts = (
                await session.execute(
                    select(func.count())
                    .select_from(Alert)
                    .where(Alert.sent_at >= utcnow() - timedelta(days=7))
                )
            ).scalar_one()
            snapshots = (
                await session.execute(select(func.count()).select_from(PriceSnapshot))
            ).scalar_one()

        if last_run is None:
            last_line = "Парсер ещё ни разу не отрабатывал."
        else:
            age = utcnow() - last_run.started_at
            hours = age.total_seconds() / 3600
            when = f"{int(hours)} ч назад" if hours >= 1 else f"{int(age.total_seconds() // 60)} мин назад"
            last_line = (
                f"Последний проход: {when} "
                f"({last_run.ok_count}/{last_run.total_skus} успешно"
                + (f", ошибок {last_run.failed_count}" if last_run.failed_count else "")
                + ")"
                + ("  ⚠️ помечен как подозрительный" if last_run.is_suspicious else "")
            )

        await message.answer(
            "⚙️ Статус\n\n"
            f"{last_line}\n"
            f"Товаров отслеживается: {sku_count}\n"
            f"Замеров в базе: {snapshots}\n"
            f"Алертов за 7 дней: {week_alerts}\n\n"
            f"Расписание: парсинг каждые {services.cfg.parse_interval_hours} ч, "
            f"отчёт — {services.cfg.report_weekday} в {services.cfg.report_hour}:00 UTC."
        )

    # --------------------------------- ссылка без кнопки = «добавить товар»

    @router.message(F.text, ~F.text.startswith("/"))
    async def loose_link(message: Message, state: FSMContext) -> None:
        """Прислал ссылку просто так — значит, хочет добавить товар."""
        if not is_admin(message.chat.id):
            await deny_if_stranger(message)
            return
        try:
            extract_nm_id(message.text or "")
        except BadArticle:
            await message.answer(
                "Не понял. Пользуйся кнопками снизу или пришли ссылку на товар WB.",
                reply_markup=main_menu(True),
            )
            return
        await handle_article(message, state)

    return router
