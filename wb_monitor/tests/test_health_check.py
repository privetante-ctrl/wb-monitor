"""Тесты health_check_job: сломанный парсер алертит админа, не клиентов."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from wb_monitor.models import Alert, AlertType, ParseRun
from wb_monitor.scheduler.jobs import _run_looks_broken, health_check_job
from wb_monitor.tests.conftest import FakeBot

ADMIN = 999


async def _add_run(session_factory, **kwargs) -> None:
    async with session_factory() as session:
        session.add(ParseRun(**kwargs))
        await session.commit()


# ------------------------------------------------------- флаг is_suspicious


def test_failure_ratio_over_30_percent_is_broken():
    run = ParseRun(total_skus=10, ok_count=6, failed_count=4, zero_stock_count=0)
    assert _run_looks_broken(run)


def test_failure_ratio_exactly_30_percent_is_ok():
    run = ParseRun(total_skus=10, ok_count=7, failed_count=3, zero_stock_count=0)
    assert not _run_looks_broken(run)


def test_mass_zero_stock_is_broken():
    run = ParseRun(total_skus=10, ok_count=10, failed_count=0, zero_stock_count=6)
    assert _run_looks_broken(run)


def test_normal_run_is_ok():
    run = ParseRun(total_skus=10, ok_count=10, failed_count=0, zero_stock_count=2)
    assert not _run_looks_broken(run)


def test_empty_run_is_not_broken():
    assert not _run_looks_broken(ParseRun(total_skus=0, ok_count=0, failed_count=0))


# --------------------------------------------------------- health_check_job


async def test_suspicious_run_alerts_admin_once(session_factory):
    await _add_run(
        session_factory,
        started_at=datetime.utcnow(),
        finished_at=datetime.utcnow(),
        total_skus=10, ok_count=2, failed_count=8, is_suspicious=True,
    )
    bot = FakeBot()

    problems = await health_check_job(session_factory, bot, ADMIN)
    assert len(problems) == 1
    assert bot.messages[0][0] == ADMIN
    assert "парсер" in bot.messages[0][1]

    # повторный вызов по тому же проходу — молчит (антидубль)
    assert await health_check_job(session_factory, bot, ADMIN) == []
    assert len(bot.messages) == 1

    async with session_factory() as session:
        alerts = (await session.execute(select(Alert))).scalars().all()
        assert len(alerts) == 1
        assert alerts[0].alert_type == AlertType.SCRAPER_BROKEN.value
        assert alerts[0].client_id is None  # техническое, не клиентское


async def test_new_suspicious_run_alerts_again(session_factory):
    bot = FakeBot()
    await _add_run(
        session_factory,
        started_at=datetime.utcnow() - timedelta(hours=4),
        total_skus=10, ok_count=2, failed_count=8, is_suspicious=True,
    )
    assert len(await health_check_job(session_factory, bot, ADMIN)) == 1

    await _add_run(
        session_factory,
        started_at=datetime.utcnow(),
        total_skus=10, ok_count=1, failed_count=9, is_suspicious=True,
    )
    assert len(await health_check_job(session_factory, bot, ADMIN)) == 1
    assert len(bot.messages) == 2


async def test_no_runs_at_all_alerts_admin(session_factory):
    bot = FakeBot()
    problems = await health_check_job(session_factory, bot, ADMIN)
    assert len(problems) == 1
    assert "ни разу" in problems[0]


async def test_stale_run_alerts_admin(session_factory):
    await _add_run(
        session_factory,
        started_at=datetime.utcnow() - timedelta(hours=30),
        total_skus=10, ok_count=10, failed_count=0,
    )
    bot = FakeBot()
    problems = await health_check_job(session_factory, bot, ADMIN)
    assert len(problems) == 1
    assert "не запускался" in problems[0]


async def test_healthy_run_is_silent(session_factory):
    await _add_run(
        session_factory,
        started_at=datetime.utcnow(),
        total_skus=10, ok_count=10, failed_count=0,
    )
    bot = FakeBot()
    assert await health_check_job(session_factory, bot, ADMIN) == []
    assert bot.messages == []
