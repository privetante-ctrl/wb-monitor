"""CLI-обвязка: ручной запуск job'ов и админские операции без похода в БД руками.

Запуск: `python cli.py <команда>` из папки wb_monitor/
или `python -m wb_monitor.cli <команда>` из корня репозитория.

Команды с отправкой в Telegram (run-alerts, run-report) по умолчанию шлют
по-настоящему, если задан BOT_TOKEN; --dry-run отключает отправку —
результат виден в БД/файле и логах.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if __package__ in (None, ""):  # запущено как `python cli.py` из папки проекта
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import click

from wb_monitor.config import settings
from wb_monitor.db import init_db, make_engine, make_sessionmaker


def _run(coro_factory) -> None:
    """Открыть движок/сессии, выполнить корутину, корректно всё закрыть."""
    _bot_holder: list = []

    def _get_bot():
        """Ленивое создание бота — только если команда реально шлёт сообщения."""
        if not settings.bot_token:
            raise click.ClickException("BOT_TOKEN не задан — заполни .env или используй --dry-run")
        if not _bot_holder:
            from aiogram import Bot

            _bot_holder.append(Bot(settings.bot_token))
        return _bot_holder[0]

    async def _main() -> None:
        engine = make_engine(settings.db_url)
        await init_db(engine)
        session_factory = make_sessionmaker(engine)
        try:
            await coro_factory(session_factory, _get_bot)
        finally:
            if _bot_holder:
                await _bot_holder[0].session.close()
            await engine.dispose()

    asyncio.run(_main())


@click.group()
def cli() -> None:
    """WB-мониторинг: ручной запуск job'ов и администрирование."""


@cli.command("init-db")
def init_db_cmd() -> None:
    """Создать файл БД со всеми таблицами."""

    async def _job(session_factory, get_bot) -> None:
        pass  # init_db уже выполнен в _run

    _run(_job)
    click.echo(f"БД инициализирована: {settings.db_path}")


@cli.command("run-parse")
def run_parse() -> None:
    """Один проход parse_job по всем активным SKU."""
    from wb_monitor.app import make_http_client
    from wb_monitor.scheduler.jobs import parse_job

    async def _job(session_factory, get_bot) -> None:
        async with make_http_client(settings) as http:
            run = await parse_job(session_factory, http)
        click.echo(
            f"Проход #{run.id}: SKU={run.total_skus}, ok={run.ok_count}, "
            f"ошибок={run.failed_count}, нулевых={run.zero_stock_count}, "
            f"подозрительный={'да' if run.is_suspicious else 'нет'}"
        )

    _run(_job)


@cli.command("run-alerts")
@click.option("--dry-run", is_flag=True, help="Не отправлять в Telegram, только записать в БД.")
def run_alerts(dry_run: bool) -> None:
    """Проверка триггеров по последним снепшотам + отправка алертов."""
    from wb_monitor.scheduler.jobs import alert_check_job

    async def _job(session_factory, get_bot) -> None:
        bot = None if dry_run else get_bot()
        sent = await alert_check_job(session_factory, bot)
        click.echo(f"Алертов: {sent}" + (" (dry-run, не отправлялись)" if dry_run else ""))

    _run(_job)


@cli.command("run-health")
@click.option("--dry-run", is_flag=True, help="Не отправлять в Telegram.")
def run_health(dry_run: bool) -> None:
    """Проверка здоровья парсера (алерт админу при проблемах)."""
    from wb_monitor.scheduler.jobs import health_check_job

    async def _job(session_factory, get_bot) -> None:
        bot = None if dry_run else get_bot()
        problems = await health_check_job(session_factory, bot, settings.admin_chat_id)
        click.echo("Проблем нет." if not problems else "\n".join(problems))

    _run(_job)


@cli.command("run-report")
@click.option("--client-id", type=int, default=None, help="Один клиент (по умолчанию все активные).")
@click.option("--dry-run", is_flag=True, help="Только сгенерировать файл, не отправлять.")
def run_report(client_id: int | None, dry_run: bool) -> None:
    """Сгенерировать и отправить еженедельный отчёт."""
    from wb_monitor.scheduler.jobs import weekly_report_job

    async def _job(session_factory, get_bot) -> None:
        bot = None if dry_run else get_bot()
        paths = await weekly_report_job(
            session_factory, bot, settings.reports_dir, client_id=client_id
        )
        for p in paths:
            click.echo(p)
        if not paths:
            click.echo("Отчёты не сгенерированы (нет активных клиентов?).")

    _run(_job)


@cli.command("run")
def run_cmd() -> None:
    """Основной процесс: aiogram-бот + APScheduler (для systemd)."""
    from wb_monitor.app import run_app

    asyncio.run(run_app())


# ------------------------------------------------------- администрирование


@cli.command("add-client")
@click.option("--name", required=True)
@click.option("--chat-id", required=True, type=int, help="telegram chat_id клиента")
@click.option("--tier", default="basic", type=click.Choice(["basic", "standard", "pro"]))
@click.option("--price", default=0.0, type=float, help="цена подписки, руб/мес")
def add_client(name: str, chat_id: int, tier: str, price: float) -> None:
    """Добавить клиента."""
    from wb_monitor.models import Client

    async def _job(session_factory, get_bot) -> None:
        async with session_factory() as session:
            client = Client(name=name, telegram_chat_id=chat_id, tier=tier, price_rub=price)
            session.add(client)
            await session.commit()
            click.echo(f"Клиент #{client.id} «{name}» добавлен ({tier}).")

    _run(_job)


@cli.command("add-sku")
@click.option("--client-id", required=True, type=int)
@click.option("--nm-id", required=True, help="артикул WB (nmId)")
@click.option("--name", required=True, help="отображаемое имя в отчёте")
@click.option("--own", is_flag=True, help="это свой товар, а не конкурента")
@click.option("--category", default=None)
def add_sku(client_id: int, nm_id: str, name: str, own: bool, category: str | None) -> None:
    """Добавить отслеживаемый SKU клиенту."""
    from wb_monitor.models import Client, TrackedSKU

    async def _job(session_factory, get_bot) -> None:
        async with session_factory() as session:
            if await session.get(Client, client_id) is None:
                raise click.ClickException(f"Клиента #{client_id} нет.")
            sku = TrackedSKU(
                client_id=client_id, wb_article_id=nm_id, display_name=name,
                is_own_product=own, category=category,
            )
            session.add(sku)
            await session.commit()
            click.echo(f"SKU #{sku.id} «{name}» добавлен клиенту #{client_id}.")

    _run(_job)


@cli.command("list-clients")
def list_clients() -> None:
    """Список клиентов с их SKU-счётчиком."""
    from sqlalchemy import func, select

    from wb_monitor.models import Client, TrackedSKU

    async def _job(session_factory, get_bot) -> None:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(Client, func.count(TrackedSKU.id))
                    .outerjoin(TrackedSKU, TrackedSKU.client_id == Client.id)
                    .group_by(Client.id)
                    .order_by(Client.id)
                )
            ).all()
        if not rows:
            click.echo("Клиентов пока нет.")
        for client, sku_count in rows:
            click.echo(
                f"#{client.id} {client.name} — {client.tier}, {client.price_rub:.0f} руб/мес, "
                f"chat {client.telegram_chat_id}, SKU: {sku_count}"
                f"{'' if client.is_active else ' (ВЫКЛЮЧЕН)'}"
            )

    _run(_job)


@cli.command("list-skus")
@click.option("--client-id", required=True, type=int)
def list_skus(client_id: int) -> None:
    """SKU клиента."""
    from sqlalchemy import select

    from wb_monitor.models import TrackedSKU

    async def _job(session_factory, get_bot) -> None:
        async with session_factory() as session:
            skus = (
                await session.execute(
                    select(TrackedSKU).where(TrackedSKU.client_id == client_id)
                )
            ).scalars().all()
        if not skus:
            click.echo("SKU не найдены.")
        for s in skus:
            click.echo(
                f"#{s.id} {s.display_name} — nmId {s.wb_article_id}, "
                f"{'свой' if s.is_own_product else 'конкурент'}"
                f"{'' if s.is_active else ' (снят с отслеживания)'}"
            )

    _run(_job)


if __name__ == "__main__":
    cli()
