"""Один процесс на VPS: aiogram-polling + APScheduler (см. architecture.md).

Job'ы:
- run_parse_cycle  — каждые PARSE_INTERVAL_HOURS (parse -> health -> алерты)
- weekly_report_job — REPORT_WEEKDAY в REPORT_HOUR
- health_check_job  — ежедневно в HEALTH_CHECK_HOUR («parse вообще работал?»)
"""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from wb_monitor.bot.handlers import create_router
from wb_monitor.config import Settings, settings
from wb_monitor.db import init_db, make_engine, make_sessionmaker
from wb_monitor.parser.client import HttpClient
from wb_monitor.scheduler.jobs import (
    health_check_job,
    run_parse_cycle,
    weekly_report_job,
)

logger = logging.getLogger(__name__)


def make_http_client(cfg: Settings) -> HttpClient:
    return HttpClient(
        delay_range=(cfg.request_delay_min, cfg.request_delay_max),
        retries=cfg.request_retries,
        timeout=cfg.request_timeout,
        proxy=cfg.proxy_url,  # точка расширения: пока None
    )


async def run_app(cfg: Settings = settings) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not cfg.bot_token:
        raise SystemExit("BOT_TOKEN не задан — заполни .env (см. .env.example)")

    engine = make_engine(cfg.db_url)
    await init_db(engine)
    session_factory = make_sessionmaker(engine)

    bot = Bot(cfg.bot_token)
    dp = Dispatcher()
    dp.include_router(create_router(session_factory, cfg.admin_chat_id, cfg.reports_dir))

    http = make_http_client(cfg)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_parse_cycle,
        "interval",
        hours=cfg.parse_interval_hours,
        args=[session_factory, http, bot, cfg.admin_chat_id],
        id="parse_cycle",
        max_instances=1,           # прошлый проход ещё идёт — новый не стартуем
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        weekly_report_job,
        "cron",
        day_of_week=cfg.report_weekday,
        hour=cfg.report_hour,
        args=[session_factory, bot, cfg.reports_dir],
        id="weekly_report",
        misfire_grace_time=3600 * 6,
    )
    scheduler.add_job(
        health_check_job,
        "cron",
        hour=cfg.health_check_hour,
        args=[session_factory, bot, cfg.admin_chat_id],
        id="health_check",
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info(
        "Планировщик запущен: parse каждые %dч, отчёт %s в %d:00 UTC",
        cfg.parse_interval_hours, cfg.report_weekday, cfg.report_hour,
    )

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await http.aclose()
        await bot.session.close()
        await engine.dispose()
