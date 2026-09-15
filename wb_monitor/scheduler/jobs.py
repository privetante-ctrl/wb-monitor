"""Job'ы планировщика: parse_job, alert_check_job, health_check_job.

Все job'ы принимают session_factory и (где нужно) bot параметрами, чтобы
их можно было дёргать и из APScheduler, и из CLI, и из тестов.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wb_monitor.bot.notify import BotLike, send_admin_alert, send_client_alert
from wb_monitor.models import (
    ALERT_TIERS,
    Alert,
    AlertType,
    Client,
    ParseRun,
    PriceSnapshot,
    TrackedSKU,
)
from wb_monitor.parser.client import HttpClient, WBRequestError
from wb_monitor.parser.wb_api import fetch_sku
from wb_monitor.scheduler import thresholds

logger = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]


# ---------------------------------------------------------------- parse_job


async def parse_job(session_factory: SessionFactory, http: HttpClient) -> ParseRun:
    """Проход по всем активным SKU активных клиентов, пишет PriceSnapshot'ы.

    Идёт последовательно (паузу между запросами держит HttpClient) — на
    50-150 SKU это 2-5 минут, быстрее и не нужно. Ошибка по одному SKU не
    прерывает проход: снепшот просто не пишется, ошибка идёт в счётчик
    ParseRun.failed_count.
    """
    async with session_factory() as session:
        run = ParseRun(started_at=datetime.utcnow())
        session.add(run)

        skus = (
            await session.execute(
                select(TrackedSKU)
                .join(Client)
                .where(TrackedSKU.is_active, Client.is_active)
                .order_by(TrackedSKU.id)
            )
        ).scalars().all()

        run.total_skus = len(skus)
        for sku in skus:
            try:
                data = await fetch_sku(http, sku.wb_article_id)
            except WBRequestError as exc:
                run.failed_count += 1
                logger.warning("SKU %s (%s): %s", sku.id, sku.wb_article_id, exc)
                continue
            session.add(
                PriceSnapshot(
                    sku_id=sku.id,
                    checked_at=datetime.utcnow(),
                    price=data.price,
                    discount_price=data.discount_price,
                    stock_qty=data.stock_qty,
                )
            )
            run.ok_count += 1
            if data.stock_qty == 0:
                run.zero_stock_count += 1

        run.finished_at = datetime.utcnow()
        run.is_suspicious = _run_looks_broken(run)
        await session.commit()

        logger.info(
            "parse_job: %d SKU, ok=%d, failed=%d, zero=%d, suspicious=%s",
            run.total_skus, run.ok_count, run.failed_count,
            run.zero_stock_count, run.is_suspicious,
        )
        return run


def _run_looks_broken(run: ParseRun) -> bool:
    """Массовые ошибки/нули за один проход — признак сломанного парсера."""
    if run.total_skus == 0:
        return False
    if run.failed_count / run.total_skus > thresholds.SCRAPER_FAILURE_RATIO:
        return True
    return (
        run.ok_count > 0
        and run.zero_stock_count / run.ok_count > thresholds.SCRAPER_ZERO_STOCK_RATIO
    )


# ---------------------------------------------------------- alert_check_job


@dataclass(frozen=True)
class AlertCandidate:
    alert_type: str
    message: str


def evaluate_transitions(prev: PriceSnapshot, cur: PriceSnapshot, sku_name: str) -> list[AlertCandidate]:
    """Чистая функция: какие триггеры даёт переход prev -> cur.

    Все условия edge-triggered (сравнение двух соседних состояний), поэтому
    пока состояние не меняется, триггер не повторяется по построению.
    Пороги строгие: ровно 10% — не алерт (см. thresholds.py).
    """
    result: list[AlertCandidate] = []

    prev_price, cur_price = prev.effective_price, cur.effective_price
    if prev_price > 0:
        change = (cur_price - prev_price) / prev_price
        if change < -thresholds.PRICE_DROP_THRESHOLD:
            result.append(AlertCandidate(
                AlertType.PRICE_DROP.value,
                f"📉 «{sku_name}»: цена упала на {-change:.0%} "
                f"({prev_price:.0f} → {cur_price:.0f} руб.)",
            ))
        elif change > thresholds.PRICE_UP_THRESHOLD:
            result.append(AlertCandidate(
                AlertType.PRICE_UP.value,
                f"📈 «{sku_name}»: цена выросла на {change:.0%} "
                f"({prev_price:.0f} → {cur_price:.0f} руб.)",
            ))

    if prev.stock_qty > 0 and cur.stock_qty == 0:
        result.append(AlertCandidate(
            AlertType.STOCK_ZERO.value,
            f"⛔ «{sku_name}»: остаток обнулился (было {prev.stock_qty} шт.)",
        ))
    elif prev.stock_qty == 0 and cur.stock_qty > 0:
        result.append(AlertCandidate(
            AlertType.STOCK_BACK.value,
            f"✅ «{sku_name}»: товар снова в наличии ({cur.stock_qty} шт.)",
        ))

    return result


async def _is_duplicate(
    session: AsyncSession, sku_id: int, alert_type: str, cur_snapshot_id: int
) -> bool:
    """Антидубль: сверяемся с последним Alert по этому sku_id + alert_type.

    Триггеры edge-triggered, поэтому два алерта одного типа подряд возможны
    только если состояние успело измениться обратно (например, 10 → 0 → 5 → 0
    даёт два честных STOCK_ZERO). Значит дубль — это ровно ситуация, когда
    последний алерт этого типа сработал на том же (или более новом) снепшоте:
    повторный прогон alert_check_job по тем же данным, рестарт после падения
    между отправкой и коммитом и т.п.
    """
    last = (
        await session.execute(
            select(Alert)
            .where(Alert.sku_id == sku_id, Alert.alert_type == alert_type)
            .order_by(Alert.sent_at.desc(), Alert.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is None:
        return False
    return last.snapshot_id is not None and last.snapshot_id >= cur_snapshot_id


async def alert_check_job(session_factory: SessionFactory, bot: BotLike | None) -> int:
    """Сравнивает последний снепшот с предыдущим и шлёт алерты клиентам.

    Возвращает число отправленных алертов. Обрабатываются только тарифы с
    алертами (standard/pro) — basic получает лишь еженедельный отчёт.
    Если последний проход parse_job помечен как подозрительный (сломан
    парсер), клиентские алерты пропускаются целиком: массовый «остаток 0» —
    это не рыночное событие (см. architecture.md).
    """
    sent = 0
    async with session_factory() as session:
        last_run = (
            await session.execute(select(ParseRun).order_by(ParseRun.id.desc()).limit(1))
        ).scalar_one_or_none()
        if last_run is not None and last_run.is_suspicious:
            logger.warning(
                "alert_check_job: проход #%d подозрительный, клиентские алерты пропущены",
                last_run.id,
            )
            return 0

        skus = (
            await session.execute(
                select(TrackedSKU, Client)
                .join(Client)
                .where(
                    TrackedSKU.is_active,
                    Client.is_active,
                    Client.tier.in_(ALERT_TIERS),
                )
                .order_by(TrackedSKU.id)
            )
        ).all()

        for sku, client in skus:
            snapshots = (
                await session.execute(
                    select(PriceSnapshot)
                    .where(PriceSnapshot.sku_id == sku.id)
                    .order_by(PriceSnapshot.checked_at.desc(), PriceSnapshot.id.desc())
                    .limit(2)
                )
            ).scalars().all()
            if len(snapshots) < 2:
                continue
            cur, prev = snapshots[0], snapshots[1]

            for candidate in evaluate_transitions(prev, cur, sku.display_name):
                if await _is_duplicate(session, sku.id, candidate.alert_type, cur.id):
                    continue
                session.add(Alert(
                    client_id=client.id,
                    sku_id=sku.id,
                    snapshot_id=cur.id,
                    alert_type=candidate.alert_type,
                    message=candidate.message[:500],
                    sent_at=datetime.utcnow(),
                ))
                # коммитим ДО отправки: упасть между отправкой и записью —
                # это повторный алерт клиенту; между записью и отправкой —
                # один потерянный (видно в логах). Второе дешевле.
                await session.commit()
                if bot is not None:
                    await send_client_alert(bot, client.telegram_chat_id, candidate.message)
                sent += 1

    logger.info("alert_check_job: отправлено алертов: %d", sent)
    return sent


# --------------------------------------------------------- health_check_job


async def health_check_job(
    session_factory: SessionFactory, bot: BotLike | None, admin_chat_id: int
) -> list[str]:
    """Здоровье парсера. Проблемы идут ТЕБЕ в личку, не клиентам.

    Две проверки:
    1. parse_job вообще отработал за последние PARSE_STALE_HOURS часов?
    2. последний проход не подозрительный? (>30% ошибок или массовые нули —
       см. thresholds.SCRAPER_FAILURE_RATIO / SCRAPER_ZERO_STOCK_RATIO,
       сам флаг ставит parse_job в _run_looks_broken)

    Антидубль: по одному проходу не алертим дважды — если последний
    SCRAPER_BROKEN-алерт свежее начала этого прохода, молчим.
    Возвращает список отправленных сообщений (удобно для CLI и тестов).
    """
    problems: list[str] = []
    async with session_factory() as session:
        last_run = (
            await session.execute(select(ParseRun).order_by(ParseRun.id.desc()).limit(1))
        ).scalar_one_or_none()

        stale_after = datetime.utcnow() - timedelta(hours=thresholds.PARSE_STALE_HOURS)
        if last_run is None:
            problems.append("parse_job ещё ни разу не отработал — планировщик запущен?")
        elif last_run.started_at < stale_after:
            problems.append(
                f"parse_job не запускался с {last_run.started_at:%d.%m %H:%M} UTC "
                f"(порог {thresholds.PARSE_STALE_HOURS}ч) — процесс жив?"
            )
        elif last_run.is_suspicious:
            last_broken_alert = (
                await session.execute(
                    select(Alert)
                    .where(Alert.alert_type == AlertType.SCRAPER_BROKEN.value)
                    .order_by(Alert.sent_at.desc(), Alert.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            already_alerted = (
                last_broken_alert is not None
                and last_broken_alert.sent_at >= last_run.started_at
            )
            if not already_alerted:
                problems.append(
                    f"Похоже, сломался парсер: проход #{last_run.id} — "
                    f"{last_run.failed_count} ошибок и {last_run.zero_stock_count} нулевых "
                    f"остатков из {last_run.total_skus} SKU. Клиентские алерты по этому "
                    f"проходу подавлены, проверь разметку WB."
                )

        for text in problems:
            session.add(Alert(
                client_id=None,
                sku_id=None,
                alert_type=AlertType.SCRAPER_BROKEN.value,
                message=text[:500],
                sent_at=datetime.utcnow(),
            ))
        if problems:
            await session.commit()

    for text in problems:
        logger.error("health_check: %s", text)
        if bot is not None:
            await send_admin_alert(bot, admin_chat_id, text)
    if not problems:
        logger.info("health_check: всё в порядке")
    return problems


# -------------------------------------------------------- weekly_report_job


async def weekly_report_job(
    session_factory: SessionFactory,
    bot: BotLike | None,
    reports_dir: str,
    client_id: int | None = None,
    period_end: date | None = None,
) -> list[str]:
    """Excel-отчёт за последние 7 дней каждому активному клиенту (или одному).

    По умолчанию период — предыдущие 7 полных дней (job запускается в
    понедельник утром -> период пн-вс прошлой недели). Возвращает пути
    сгенерированных файлов. Ошибка по одному клиенту не мешает остальным.
    """
    from wb_monitor.bot.notify import send_report_file
    from wb_monitor.reports.excel_report import (
        build_report_file,
        collect_report_data,
        format_period,
    )

    end = period_end or (datetime.utcnow().date() - timedelta(days=1))
    start = end - timedelta(days=6)

    generated: list[str] = []
    async with session_factory() as session:
        query = select(Client).where(Client.is_active)
        if client_id is not None:
            query = query.where(Client.id == client_id)
        clients = (await session.execute(query.order_by(Client.id))).scalars().all()

        for client in clients:
            try:
                data = await collect_report_data(session, client, start, end)
                path = Path(reports_dir) / f"wb_report_client{client.id}_{end:%Y%m%d}.xlsx"
                build_report_file(data, path)
            except Exception:
                logger.exception("Отчёт для клиента %d не собрался", client.id)
                continue
            generated.append(str(path))
            if bot is not None:
                await send_report_file(
                    bot,
                    client.telegram_chat_id,
                    path,
                    f"📊 Еженедельный отчёт: {format_period(start, end)}",
                )

    logger.info("weekly_report_job: сгенерировано отчётов: %d", len(generated))
    return generated


# ---------------------------------------------------------- полный цикл


async def run_parse_cycle(
    session_factory: SessionFactory,
    http: HttpClient,
    bot: BotLike | None,
    admin_chat_id: int,
) -> None:
    """parse → health-check → алерты клиентам. Вешается на APScheduler.

    health_check идёт ДО alert_check: если проход подозрительный, ты
    узнаёшь об этом сразу (не ждёшь суточного health-check), а клиентские
    алерты alert_check и так пропустит по флагу is_suspicious.
    """
    run = await parse_job(session_factory, http)
    if run.is_suspicious:
        await health_check_job(session_factory, bot, admin_chat_id)
        return
    await alert_check_job(session_factory, bot)
