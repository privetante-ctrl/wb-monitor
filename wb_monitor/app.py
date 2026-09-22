"""Один процесс: aiogram-polling + APScheduler. Запускается кнопкой (start.sh).

Job'ы:
- run_parse_cycle  — каждые PARSE_INTERVAL_HOURS (parse -> health -> алерты)
- weekly_report_job — REPORT_WEEKDAY в REPORT_HOUR
- health_check_job  — ежедневно в HEALTH_CHECK_HOUR («parse вообще работал?»)

admin_chat_id намеренно не «замораживается» в аргументах job'ов: бот может
узнать его уже после старта (первый /start), поэтому job'ы обёрнуты в
замыкания, которые читают актуальное значение из AdminRef в момент запуска.
"""

from __future__ import annotations

import logging
import sys
from datetime import timedelta

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from wb_monitor.bot.handlers import create_router
from wb_monitor.bot.menu import create_menu_router
from wb_monitor.config import Settings, settings
from wb_monitor.db import init_db, make_engine, make_sessionmaker
from wb_monitor.models import ParseRun, TrackedSKU
from wb_monitor.parser.client import HttpClient
from wb_monitor.scheduler.jobs import (
    health_check_job,
    run_parse_cycle,
    weekly_report_job,
)
from wb_monitor.services import Services
from wb_monitor.state import AdminRef
from wb_monitor.utils import utcnow

logger = logging.getLogger(__name__)

# Код выхода «чини настройки, перезапуск не поможет» — start.sh на нём
# останавливает цикл авто-перезапуска вместо бесконечного долбления.
EXIT_CONFIG_ERROR = 2

# при рестарте не парсим заново, если свежий проход уже есть
STARTUP_PARSE_MIN_AGE_HOURS = 1
# даём боту ответить на первое сообщение раньше, чем начнётся долгий проход
STARTUP_PARSE_DELAY_SECONDS = 20

BOT_COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="help", description="Как пользоваться"),
    BotCommand(command="admin", description="Команды для работы с клиентами"),
]


def _fail(problem: str, fix: str) -> None:
    """Аккуратно умереть с человеческим текстом вместо стектрейса."""
    print(f"\n❌ {problem}\n   {fix}\n", file=sys.stderr)
    raise SystemExit(EXIT_CONFIG_ERROR)


def make_http_client(cfg: Settings) -> HttpClient:
    return HttpClient(
        delay_range=(cfg.request_delay_min, cfg.request_delay_max),
        retries=cfg.request_retries,
        timeout=cfg.request_timeout,
        proxy=cfg.proxy_url,  # точка расширения: пока None
    )


async def _needs_startup_parse(session_factory) -> bool:
    """Есть что парсить и последний проход не свежий — снимем цены сразу."""
    async with session_factory() as session:
        has_skus = (
            await session.execute(select(TrackedSKU.id).where(TrackedSKU.is_active).limit(1))
        ).first()
        if not has_skus:
            return False
        last_run = (
            await session.execute(select(ParseRun).order_by(ParseRun.id.desc()).limit(1))
        ).scalar_one_or_none()
    if last_run is None:
        return True
    return last_run.started_at < utcnow() - timedelta(hours=STARTUP_PARSE_MIN_AGE_HOURS)


async def run_app(cfg: Settings = settings) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not cfg.bot_token:
        _fail(
            "BOT_TOKEN не задан.",
            "Запусти ./start.sh --setup — он спросит токен и всё пропишет.",
        )

    engine = make_engine(cfg.db_url)
    await init_db(engine)
    session_factory = make_sessionmaker(engine)

    admin = AdminRef(session_factory, env_value=cfg.admin_chat_id)
    await admin.load()
    if not admin.is_set:
        logger.info("Админ ещё не назначен — им станет первый, кто напишет боту /start")

    bot = Bot(cfg.bot_token)
    http = make_http_client(cfg)
    services = Services(cfg=cfg, session_factory=session_factory, http=http, admin=admin)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(create_menu_router(services))          # кнопки — основной путь
    dp.include_router(create_router(session_factory, admin, cfg.reports_dir))  # команды

    # --- job'ы: замыкания, чтобы admin.chat_id читался в момент запуска ---

    async def parse_cycle_job() -> None:
        if services.parse_lock.locked():
            logger.info("Плановый проход пропущен: ручная проверка уже идёт")
            return
        async with services.parse_lock:
            await run_parse_cycle(session_factory, http, bot, admin.chat_id)

    async def daily_health_job() -> None:
        await health_check_job(session_factory, bot, admin.chat_id)

    async def weekly_job() -> None:
        await weekly_report_job(session_factory, bot, cfg.reports_dir)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        parse_cycle_job,
        "interval",
        hours=cfg.parse_interval_hours,
        id="parse_cycle",
        max_instances=1,           # прошлый проход ещё идёт — новый не стартуем
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        weekly_job,
        "cron",
        day_of_week=cfg.report_weekday,
        hour=cfg.report_hour,
        id="weekly_report",
        misfire_grace_time=3600 * 6,
    )
    scheduler.add_job(
        daily_health_job,
        "cron",
        hour=cfg.health_check_hour,
        id="health_check",
        misfire_grace_time=3600,
    )

    if await _needs_startup_parse(session_factory):
        scheduler.add_job(
            parse_cycle_job,
            "date",
            run_date=utcnow() + timedelta(seconds=STARTUP_PARSE_DELAY_SECONDS),
            id="startup_parse",
        )
        logger.info("Свежих данных нет — первый проход запустится через %d с",
                    STARTUP_PARSE_DELAY_SECONDS)

    scheduler.start()
    logger.info(
        "Планировщик запущен: parse каждые %dч, отчёт %s в %d:00 UTC",
        cfg.parse_interval_hours, cfg.report_weekday, cfg.report_hour,
    )

    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            _fail(
                "Telegram отклонил BOT_TOKEN.",
                "Возьми токен заново у @BotFather и запусти ./start.sh --setup",
            )
        except TelegramNetworkError as exc:
            _fail(
                f"Не получается связаться с Telegram: {exc}",
                "Проверь интернет. Если Telegram заблокирован у провайдера — "
                "пропиши прокси в .env (PROXY_URL).",
            )

        logger.info("Бот @%s на связи. Напиши ему /start в Telegram.", me.username)
        try:
            await bot.set_my_commands(BOT_COMMANDS)
        except TelegramNetworkError:  # не повод не запускаться
            logger.warning("Не удалось обновить список команд бота")

        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await http.aclose()
        await bot.session.close()
        await engine.dispose()
