"""Сценарий «нажал кнопку — заработало», прогнанный через настоящий Dispatcher.

Telegram подменён фейковой сессией aiogram: апдейты идут через реальный
роутинг, фильтры и FSM, наружу уходят не запросы, а записи в списке.
Проверяется ровно то, ради чего всё затевалось: человек нажимает /start,
кидает ссылку и получает отслеживаемый товар, ничего не настраивая.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    GetMe,
    SendDocument,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import select

from wb_monitor.bot.keyboards import (
    BTN_ADD,
    BTN_CHECK,
    BTN_HELP,
    BTN_LIST,
    BTN_REPORT,
    BTN_STATUS,
)
from wb_monitor.bot.menu import create_menu_router
from wb_monitor.config import Settings
from wb_monitor.models import Client, TrackedSKU
from wb_monitor.parser.wb_api import SkuData
from wb_monitor.services import Services
from wb_monitor.state import AdminRef

OWNER_CHAT = 424242
STRANGER_CHAT = 111222
TOKEN = "42:TESTTOKENTESTTOKENTESTTOKENTESTTOKEN"

FAKE_CARD = SkuData(
    nm_id="18234561",
    price=450.0,
    discount_price=369.0,
    stock_qty=25,
    name="Носки шерстяные",
    brand="БрендА",
)


class FakeSession(BaseSession):
    """Сессия aiogram, которая ничего не шлёт, а копит вызовы."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[Any] = []
        self._message_id = 0

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover - не используется
        yield b""

    def _message(self, bot: Bot, chat_id: int, text: str) -> Message:
        """Ответ Telegram на send/edit — привязанный к боту, как в бою.

        Без привязки у результата message.answer() не будет бота, и
        цепочка «ответил → отредактировал своё сообщение» не соберётся.
        """
        self._message_id += 1
        return Message(
            message_id=self._message_id,
            date=datetime(2026, 7, 20, 12, 0),
            chat=Chat(id=chat_id, type="private"),
            text=text,
        ).as_(bot)

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return self._message(bot, method.chat_id, method.text)
        if isinstance(method, EditMessageText):
            return self._message(bot, method.chat_id or 0, method.text)
        if isinstance(method, SendDocument):
            return self._message(bot, method.chat_id, "<document>")
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="WB", username="wb_test_bot")
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage)):
            return True
        return True

    # --- удобные выборки для проверок ---

    @property
    def texts(self) -> list[str]:
        return [
            c.text for c in self.calls if isinstance(c, (SendMessage, EditMessageText))
        ]

    @property
    def last_text(self) -> str:
        return self.texts[-1]

    def last_markup(self):
        for call in reversed(self.calls):
            if isinstance(call, (SendMessage, EditMessageText)) and call.reply_markup:
                return call.reply_markup
        return None


@pytest_asyncio.fixture
async def bot_setup(session_factory, tmp_path, monkeypatch):
    """Диспетчер с меню-роутером и подменённым походом в WB."""
    session = FakeSession()
    bot = Bot(TOKEN, session=session)

    async def fake_fetch(http, nm_id):
        return FAKE_CARD

    monkeypatch.setattr("wb_monitor.bot.menu.fetch_sku", fake_fetch)

    cfg = Settings(bot_token=TOKEN, admin_chat_id=0, reports_dir=str(tmp_path))
    services = Services(
        cfg=cfg,
        session_factory=session_factory,
        http=None,  # fetch_sku подменён, клиент не нужен
        admin=AdminRef(session_factory),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(create_menu_router(services))

    yield dp, bot, session, services
    await bot.session.close()


def _message_update(chat_id: int, text: str, update_id: int = 1) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime(2026, 7, 20, 12, 0),
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=chat_id, is_bot=False, first_name="Тест"),
            text=text,
        ),
    )


def _callback_update(chat_id: int, data: str, update_id: int = 1) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=User(id=chat_id, is_bot=False, first_name="Тест"),
            chat_instance="ci",
            data=data,
            message=Message(
                message_id=update_id,
                date=datetime(2026, 7, 20, 12, 0),
                chat=Chat(id=chat_id, type="private"),
                text="карточка",
            ),
        ),
    )


async def _send(dp, bot, chat_id: int, text: str, update_id: int = 1) -> None:
    await dp.feed_update(bot, _message_update(chat_id, text, update_id))


async def _click(dp, bot, chat_id: int, data: str, update_id: int = 1) -> None:
    await dp.feed_update(bot, _callback_update(chat_id, data, update_id))


# ------------------------------------------------------------------ /start


async def test_start_claims_admin_and_creates_owner_client(bot_setup, session_factory):
    dp, bot, session, services = bot_setup

    await _send(dp, bot, OWNER_CHAT, "/start")

    assert services.admin.chat_id == OWNER_CHAT
    assert "админский" in session.last_text
    async with session_factory() as db:
        clients = (await db.execute(select(Client))).scalars().all()
    assert len(clients) == 1
    assert clients[0].telegram_chat_id == OWNER_CHAT
    assert clients[0].tier == "pro"  # себе — с алертами


async def test_second_start_does_not_duplicate_client(bot_setup, session_factory):
    dp, bot, _, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, "/start", 2)
    async with session_factory() as db:
        assert len((await db.execute(select(Client))).scalars().all()) == 1


async def test_stranger_gets_chat_id_and_no_admin_rights(bot_setup, session_factory):
    dp, bot, session, services = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)

    await _send(dp, bot, STRANGER_CHAT, "/start", 2)
    assert str(STRANGER_CHAT) in session.last_text
    assert services.admin.chat_id == OWNER_CHAT

    # чужому кнопка добавления недоступна: клиента в базе не появилось
    await _send(dp, bot, STRANGER_CHAT, BTN_ADD, 3)
    async with session_factory() as db:
        clients = (await db.execute(select(Client))).scalars().all()
    assert [c.telegram_chat_id for c in clients] == [OWNER_CHAT]


# ------------------------------------------------------- добавление товара


async def test_add_sku_by_link_end_to_end(bot_setup, session_factory):
    """Главный сценарий: /start → кнопка → ссылка → «конкурент» → товар в базе."""
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)

    await _send(dp, bot, OWNER_CHAT, BTN_ADD, 2)
    assert "ссылку" in session.last_text

    await _send(dp, bot, OWNER_CHAT, "https://www.wildberries.ru/catalog/18234561/detail.aspx", 3)
    assert "Носки шерстяные" in session.last_text
    assert "369" in session.last_text  # показали цену со скидкой

    await _click(dp, bot, OWNER_CHAT, "add:rival:18234561", 4)
    assert "Добавлено" in session.last_text

    async with session_factory() as db:
        skus = (await db.execute(select(TrackedSKU))).scalars().all()
    assert len(skus) == 1
    assert skus[0].wb_article_id == "18234561"
    assert skus[0].display_name == "Носки шерстяные"
    assert skus[0].is_own_product is False


async def test_bare_link_without_button_also_adds(bot_setup, session_factory):
    """Ссылка, присланная просто так, тоже должна работать."""
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)

    await _send(dp, bot, OWNER_CHAT, "18234561", 2)
    assert "Носки шерстяные" in session.last_text

    await _click(dp, bot, OWNER_CHAT, "add:own:18234561", 3)
    async with session_factory() as db:
        sku = (await db.execute(select(TrackedSKU))).scalar_one()
    assert sku.is_own_product is True


async def test_duplicate_article_rejected(bot_setup, session_factory):
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, "18234561", 2)
    await _click(dp, bot, OWNER_CHAT, "add:rival:18234561", 3)

    await _send(dp, bot, OWNER_CHAT, "18234561", 4)
    assert "уже отслеживается" in session.last_text
    async with session_factory() as db:
        assert len((await db.execute(select(TrackedSKU))).scalars().all()) == 1


async def test_garbage_text_gets_a_hint_not_a_crash(bot_setup):
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, "привет, как дела", 2)
    assert "кнопками" in session.last_text


async def test_wb_unavailable_is_reported_softly(bot_setup, monkeypatch):
    from wb_monitor.parser.client import WBRequestError

    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)

    async def boom(http, nm_id):
        raise WBRequestError("timeout")

    monkeypatch.setattr("wb_monitor.bot.menu.fetch_sku", boom)
    await _send(dp, bot, OWNER_CHAT, "18234561", 2)
    assert "не отвечает" in session.last_text


async def test_sku_not_found_is_reported(bot_setup, monkeypatch):
    from wb_monitor.parser.wb_api import SkuNotFound

    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)

    async def missing(http, nm_id):
        raise SkuNotFound("нет такого")

    monkeypatch.setattr("wb_monitor.bot.menu.fetch_sku", missing)
    await _send(dp, bot, OWNER_CHAT, "18234561", 2)
    assert "не найден" in session.last_text


# --------------------------------------------------------- список и статус


async def test_list_shows_added_sku(bot_setup):
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, "18234561", 2)
    await _click(dp, bot, OWNER_CHAT, "add:rival:18234561", 3)

    await _send(dp, bot, OWNER_CHAT, BTN_LIST, 4)
    assert "Носки шерстяные" in session.last_text
    assert "ещё не замерялся" in session.last_text


async def test_empty_list_tells_what_to_do(bot_setup):
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, BTN_LIST, 2)
    assert BTN_ADD in session.last_text


async def test_delete_flow_removes_sku(bot_setup, session_factory):
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, "18234561", 2)
    await _click(dp, bot, OWNER_CHAT, "add:rival:18234561", 3)
    async with session_factory() as db:
        sku_id = (await db.execute(select(TrackedSKU))).scalar_one().id

    await _click(dp, bot, OWNER_CHAT, f"sku:delete:{sku_id}", 4)
    assert "Удалить" in session.last_text
    await _click(dp, bot, OWNER_CHAT, f"sku:delete_yes:{sku_id}", 5)

    async with session_factory() as db:
        assert (await db.execute(select(TrackedSKU))).scalars().all() == []


async def test_toggle_pauses_tracking(bot_setup, session_factory):
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, "18234561", 2)
    await _click(dp, bot, OWNER_CHAT, "add:rival:18234561", 3)
    async with session_factory() as db:
        sku_id = (await db.execute(select(TrackedSKU))).scalar_one().id

    await _click(dp, bot, OWNER_CHAT, f"sku:toggle:{sku_id}", 4)
    async with session_factory() as db:
        assert (await db.get(TrackedSKU, sku_id)).is_active is False
    assert "снят с отслеживания" in session.last_text


async def test_status_before_any_parse(bot_setup):
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, BTN_STATUS, 2)
    assert "ни разу" in session.last_text


async def test_check_now_with_nothing_to_track(bot_setup):
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, BTN_CHECK, 2)
    assert "нечего" in session.last_text


async def test_check_now_parses_and_reports(bot_setup, session_factory, monkeypatch):
    """«Проверить сейчас» реально пишет снепшот и отчитывается человеку."""
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, "18234561", 2)
    await _click(dp, bot, OWNER_CHAT, "add:rival:18234561", 3)

    async def fake_fetch(http, nm_id):
        return FAKE_CARD

    monkeypatch.setattr("wb_monitor.scheduler.jobs.fetch_sku", fake_fetch)
    await _send(dp, bot, OWNER_CHAT, BTN_CHECK, 4)

    assert "1 из 1" in session.last_text
    from wb_monitor.models import PriceSnapshot

    async with session_factory() as db:
        snaps = (await db.execute(select(PriceSnapshot))).scalars().all()
    assert len(snaps) == 1
    assert snaps[0].effective_price == 369.0


@pytest.mark.parametrize("button", [BTN_CHECK, BTN_STATUS, BTN_ADD])
async def test_admin_only_buttons_ignore_strangers(bot_setup, button):
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    before = len(session.calls)
    await _send(dp, bot, STRANGER_CHAT, button, 2)
    # чужому отвечаем только его chat_id, действие не выполняем
    assert str(STRANGER_CHAT) in session.last_text
    assert len(session.calls) == before + 1


async def test_report_button_sends_file(bot_setup, session_factory, monkeypatch):
    """Кнопка «Отчёт» должна прислать xlsx, а не отговорку."""
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, "18234561", 2)
    await _click(dp, bot, OWNER_CHAT, "add:rival:18234561", 3)

    async def fake_fetch(http, nm_id):
        return FAKE_CARD

    monkeypatch.setattr("wb_monitor.scheduler.jobs.fetch_sku", fake_fetch)
    await _send(dp, bot, OWNER_CHAT, BTN_CHECK, 4)
    await _send(dp, bot, OWNER_CHAT, BTN_REPORT, 5)

    documents = [c for c in session.calls if isinstance(c, SendDocument)]
    assert len(documents) == 1
    assert documents[0].chat_id == OWNER_CHAT


async def test_report_without_skus_asks_to_add_one(bot_setup):
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, BTN_REPORT, 2)
    assert BTN_ADD in session.last_text
    assert not [c for c in session.calls if isinstance(c, SendDocument)]


async def test_report_without_measurements_asks_to_check(bot_setup):
    """Товар есть, замеров нет: пустой xlsx слать нельзя, нужно объяснение."""
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, "18234561", 2)
    await _click(dp, bot, OWNER_CHAT, "add:rival:18234561", 3)

    await _send(dp, bot, OWNER_CHAT, BTN_REPORT, 4)
    assert BTN_CHECK in session.last_text
    assert not [c for c in session.calls if isinstance(c, SendDocument)]


async def test_help_available_by_button_and_command(bot_setup):
    """У /help два входа — кнопка и команда; оба должны отвечать одинаково."""
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)

    await _send(dp, bot, OWNER_CHAT, BTN_HELP, 2)
    by_button = session.last_text
    await _send(dp, bot, OWNER_CHAT, "/help", 3)
    assert session.last_text == by_button
    assert "Как пользоваться" in by_button


async def test_menu_command_returns_keyboard(bot_setup):
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, "/menu", 2)
    markup = session.last_markup()
    labels = [b.text for row in markup.keyboard for b in row]
    assert BTN_ADD in labels and BTN_CHECK in labels


async def test_cancel_add_clears_state(bot_setup, session_factory):
    """После отмены следующий текст не должен молча уехать в «добавление»."""
    dp, bot, session, _ = bot_setup
    await _send(dp, bot, OWNER_CHAT, "/start", 1)
    await _send(dp, bot, OWNER_CHAT, BTN_ADD, 2)
    await _send(dp, bot, OWNER_CHAT, "18234561", 3)
    await _click(dp, bot, OWNER_CHAT, "add:cancel:0", 4)
    assert "Отменил" in session.last_text

    await _send(dp, bot, OWNER_CHAT, "просто текст", 5)
    assert "кнопками" in session.last_text
    async with session_factory() as db:
        assert (await db.execute(select(TrackedSKU))).scalars().all() == []
