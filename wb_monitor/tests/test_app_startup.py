"""Старт процесса — то, что происходит после нажатия кнопки.

Проверяем, что run_app собирает всё в рабочее состояние: поднимает базу,
восстанавливает админа, вешает job'ы и сам назначает первый проход, если
данные несвежие.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram import Bot, Dispatcher

from wb_monitor.app import EXIT_CONFIG_ERROR, _needs_startup_parse, run_app
from wb_monitor.config import Settings
from wb_monitor.models import Client, ParseRun, TrackedSKU
from wb_monitor.state import AdminRef
from wb_monitor.tests.test_bot_flow import TOKEN, FakeSession
from wb_monitor.utils import utcnow


# ------------------------------------------------ нужен ли проход при старте


async def _add_sku(session_factory) -> None:
    async with session_factory() as session:
        client = Client(name="К", telegram_chat_id=1, tier="pro", price_rub=0)
        session.add(client)
        await session.flush()
        session.add(TrackedSKU(client_id=client.id, wb_article_id="1", display_name="Т"))
        await session.commit()


async def test_no_skus_means_no_startup_parse(session_factory):
    assert await _needs_startup_parse(session_factory) is False


async def test_skus_without_runs_trigger_startup_parse(session_factory):
    await _add_sku(session_factory)
    assert await _needs_startup_parse(session_factory) is True


async def test_fresh_run_skips_startup_parse(session_factory):
    """Частые рестарты не должны каждый раз долбить WB заново."""
    await _add_sku(session_factory)
    async with session_factory() as session:
        session.add(ParseRun(started_at=utcnow(), total_skus=1, ok_count=1))
        await session.commit()
    assert await _needs_startup_parse(session_factory) is False


async def test_stale_run_triggers_startup_parse(session_factory):
    await _add_sku(session_factory)
    async with session_factory() as session:
        session.add(ParseRun(started_at=utcnow() - timedelta(hours=5), total_skus=1, ok_count=1))
        await session.commit()
    assert await _needs_startup_parse(session_factory) is True


# ----------------------------------------------------------------- run_app


@pytest.fixture
def fake_telegram(monkeypatch):
    """Бот с фейковой сессией + polling, который сразу возвращает управление."""
    created: list[Bot] = []

    def make_bot(token: str) -> Bot:
        bot = Bot(token, session=FakeSession())
        created.append(bot)
        return bot

    async def instant_polling(self, *args, **kwargs) -> None:
        return None

    monkeypatch.setattr("wb_monitor.app.Bot", make_bot)
    monkeypatch.setattr(Dispatcher, "start_polling", instant_polling)
    return created


async def test_run_app_boots_and_shuts_down_cleanly(tmp_path, fake_telegram):
    cfg = Settings(
        bot_token=TOKEN,
        admin_chat_id=0,
        db_path=str(tmp_path / "wb.db"),
        reports_dir=str(tmp_path / "reports"),
    )
    await run_app(cfg)

    assert (tmp_path / "wb.db").exists()  # база создана сама
    bot = fake_telegram[0]
    methods = [type(c).__name__ for c in bot.session.calls]
    assert "GetMe" in methods          # токен проверен на старте
    assert "SetMyCommands" in methods  # меню команд зарегистрировано


async def test_run_app_restores_admin_from_db(tmp_path, fake_telegram):
    cfg = Settings(
        bot_token=TOKEN,
        admin_chat_id=0,
        db_path=str(tmp_path / "wb.db"),
        reports_dir=str(tmp_path / "reports"),
    )
    await run_app(cfg)  # первый запуск создаёт базу

    from wb_monitor.db import make_engine, make_sessionmaker

    engine = make_engine(cfg.db_url)
    session_factory = make_sessionmaker(engine)
    await AdminRef(session_factory).claim(777)
    await engine.dispose()

    await run_app(cfg)  # второй запуск должен помнить админа
    engine = make_engine(cfg.db_url)
    restored = AdminRef(make_sessionmaker(engine))
    assert await restored.load() == 777
    await engine.dispose()


async def test_run_app_without_token_says_what_to_do(tmp_path, capsys):
    """Без токена — понятный текст и код «чини настройки», а не стектрейс."""
    with pytest.raises(SystemExit) as excinfo:
        await run_app(Settings(bot_token="", db_path=str(tmp_path / "wb.db")))
    assert excinfo.value.code == EXIT_CONFIG_ERROR
    assert "--setup" in capsys.readouterr().err


async def test_bad_token_does_not_loop_forever(tmp_path, monkeypatch, fake_telegram):
    """Неверный токен — код 2, чтобы start.sh не крутил перезапуск по кругу."""
    from aiogram.exceptions import TelegramUnauthorizedError
    from aiogram.methods import GetMe

    async def unauthorized(self, bot, method, timeout=None):
        if isinstance(method, GetMe):
            raise TelegramUnauthorizedError(method=method, message="Unauthorized")
        return True

    monkeypatch.setattr(FakeSession, "make_request", unauthorized)
    cfg = Settings(
        bot_token=TOKEN,
        db_path=str(tmp_path / "wb.db"),
        reports_dir=str(tmp_path / "reports"),
    )
    with pytest.raises(SystemExit) as excinfo:
        await run_app(cfg)
    assert excinfo.value.code == EXIT_CONFIG_ERROR
