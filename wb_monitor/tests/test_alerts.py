"""Тесты alert_check_job — прежде всего антидубль и границы порогов.

Спам клиенту и молчание при реальной проблеме — два самых дорогих бага,
поэтому здесь проверяются оба направления.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from wb_monitor.models import Alert, AlertType, Client, ParseRun, Tier, TrackedSKU
from wb_monitor.scheduler.jobs import alert_check_job, evaluate_transitions
from wb_monitor.tests.conftest import FakeBot, add_snapshots


def _snap(price: float, stock: int):
    """Лёгкий снепшот для чистой функции evaluate_transitions."""
    from wb_monitor.models import PriceSnapshot

    return PriceSnapshot(price=price, stock_qty=stock, checked_at=datetime.utcnow())


# ---------------------------------------------------- evaluate_transitions


def test_price_drop_over_threshold_fires():
    alerts = evaluate_transitions(_snap(450, 10), _snap(369, 10), "X")  # -18%
    assert [a.alert_type for a in alerts] == [AlertType.PRICE_DROP.value]


def test_price_drop_exactly_10_percent_does_not_fire():
    alerts = evaluate_transitions(_snap(100, 10), _snap(90, 10), "X")  # ровно -10%
    assert alerts == []


def test_price_up_over_threshold_fires():
    alerts = evaluate_transitions(_snap(100, 10), _snap(111, 10), "X")  # +11%
    assert [a.alert_type for a in alerts] == [AlertType.PRICE_UP.value]


def test_stock_zero_and_back():
    assert [a.alert_type for a in evaluate_transitions(_snap(100, 5), _snap(100, 0), "X")] == [
        AlertType.STOCK_ZERO.value
    ]
    assert [a.alert_type for a in evaluate_transitions(_snap(100, 0), _snap(100, 7), "X")] == [
        AlertType.STOCK_BACK.value
    ]


def test_no_transition_no_alert():
    assert evaluate_transitions(_snap(100, 0), _snap(100, 0), "X") == []
    assert evaluate_transitions(_snap(100, 5), _snap(101, 5), "X") == []


def test_discount_price_used_when_present():
    prev, cur = _snap(1000, 5), _snap(1000, 5)
    cur.discount_price = 850  # базовая цена не менялась, но скидка -15%
    alerts = evaluate_transitions(prev, cur, "X")
    assert [a.alert_type for a in alerts] == [AlertType.PRICE_DROP.value]


def test_price_drop_and_stock_zero_together():
    alerts = evaluate_transitions(_snap(450, 10), _snap(360, 0), "X")
    assert {a.alert_type for a in alerts} == {
        AlertType.PRICE_DROP.value,
        AlertType.STOCK_ZERO.value,
    }


# ------------------------------------------------------- alert_check_job


async def test_alert_sent_and_logged(session_factory, client_and_sku):
    client, sku = client_and_sku
    await add_snapshots(session_factory, sku.id, [(450, 10), (369, 10)])
    bot = FakeBot()

    sent = await alert_check_job(session_factory, bot)

    assert sent == 1
    assert len(bot.messages) == 1
    assert bot.messages[0][0] == client.telegram_chat_id
    async with session_factory() as session:
        alerts = (await session.execute(select(Alert))).scalars().all()
        assert len(alerts) == 1
        assert alerts[0].alert_type == AlertType.PRICE_DROP.value
        assert alerts[0].sku_id == sku.id
        assert alerts[0].snapshot_id is not None


async def test_rerun_on_same_data_is_silent(session_factory, client_and_sku):
    """Главный антидубль: повторный прогон по тем же снепшотам молчит."""
    _, sku = client_and_sku
    await add_snapshots(session_factory, sku.id, [(450, 10), (369, 10)])
    bot = FakeBot()

    assert await alert_check_job(session_factory, bot) == 1
    assert await alert_check_job(session_factory, bot) == 0
    assert await alert_check_job(session_factory, bot) == 0
    assert len(bot.messages) == 1


async def test_state_unchanged_no_repeat(session_factory, client_and_sku):
    """Остаток обнулился и держится нулевым — алерт ровно один."""
    _, sku = client_and_sku
    await add_snapshots(session_factory, sku.id, [(100, 10), (100, 0)])
    bot = FakeBot()
    assert await alert_check_job(session_factory, bot) == 1

    # следующий проход парсера: остаток всё ещё 0
    await add_snapshots(session_factory, sku.id, [(100, 0)])
    assert await alert_check_job(session_factory, bot) == 0
    assert len(bot.messages) == 1


async def test_state_flip_back_allows_new_alert(session_factory, client_and_sku):
    """10 → 0 → 5 → 0: два честных STOCK_ZERO и один STOCK_BACK между ними."""
    _, sku = client_and_sku
    bot = FakeBot()

    await add_snapshots(session_factory, sku.id, [(100, 10), (100, 0)])
    assert await alert_check_job(session_factory, bot) == 1  # stock_zero

    await add_snapshots(session_factory, sku.id, [(100, 5)])
    assert await alert_check_job(session_factory, bot) == 1  # stock_back

    await add_snapshots(session_factory, sku.id, [(100, 0)])
    assert await alert_check_job(session_factory, bot) == 1  # снова stock_zero

    assert len(bot.messages) == 3
    async with session_factory() as session:
        alerts = (await session.execute(select(Alert).order_by(Alert.id))).scalars().all()
        assert [a.alert_type for a in alerts] == [
            AlertType.STOCK_ZERO.value,
            AlertType.STOCK_BACK.value,
            AlertType.STOCK_ZERO.value,
        ]


async def test_second_price_drop_is_new_alert(session_factory, client_and_sku):
    """Две последовательные просадки >10% — два разных события, оба нужны."""
    _, sku = client_and_sku
    bot = FakeBot()
    await add_snapshots(session_factory, sku.id, [(1000, 10), (850, 10)])
    assert await alert_check_job(session_factory, bot) == 1
    await add_snapshots(session_factory, sku.id, [(700, 10)])
    assert await alert_check_job(session_factory, bot) == 1


async def test_basic_tier_gets_no_alerts(session_factory):
    async with session_factory() as session:
        client = Client(name="Базовый", telegram_chat_id=222, tier=Tier.BASIC.value, price_rub=990)
        session.add(client)
        await session.flush()
        sku = TrackedSKU(client_id=client.id, wb_article_id="1", display_name="Т")
        session.add(sku)
        await session.commit()
        sku_id = sku.id

    await add_snapshots(session_factory, sku_id, [(1000, 10), (500, 0)])
    bot = FakeBot()
    assert await alert_check_job(session_factory, bot) == 0
    assert bot.messages == []


async def test_inactive_sku_ignored(session_factory, client_and_sku):
    _, sku = client_and_sku
    await add_snapshots(session_factory, sku.id, [(1000, 10), (500, 0)])
    async with session_factory() as session:
        db_sku = await session.get(TrackedSKU, sku.id)
        db_sku.is_active = False
        await session.commit()

    bot = FakeBot()
    assert await alert_check_job(session_factory, bot) == 0


async def test_suspicious_parse_run_suppresses_client_alerts(session_factory, client_and_sku):
    """Сломанный парсер: клиент НЕ должен получить «остаток 0 у всех»."""
    _, sku = client_and_sku
    await add_snapshots(session_factory, sku.id, [(100, 10), (100, 0)])
    async with session_factory() as session:
        session.add(ParseRun(total_skus=10, ok_count=2, failed_count=8, is_suspicious=True))
        await session.commit()

    bot = FakeBot()
    assert await alert_check_job(session_factory, bot) == 0
    assert bot.messages == []


async def test_single_snapshot_no_alert(session_factory, client_and_sku):
    """Первый в жизни снепшот — сравнивать не с чем, молчим."""
    _, sku = client_and_sku
    await add_snapshots(session_factory, sku.id, [(100, 0)])
    bot = FakeBot()
    assert await alert_check_job(session_factory, bot) == 0


async def test_alert_logged_even_if_send_fails(session_factory, client_and_sku):
    """Ошибка Telegram не роняет job и не ломает антидубль."""

    class BrokenBot(FakeBot):
        async def send_message(self, chat_id: int, text: str) -> None:
            raise RuntimeError("bot was blocked by the user")

    _, sku = client_and_sku
    await add_snapshots(session_factory, sku.id, [(450, 10), (369, 10)])
    sent = await alert_check_job(session_factory, BrokenBot())
    assert sent == 1  # записан в БД, повторов не будет
    async with session_factory() as session:
        assert len((await session.execute(select(Alert))).scalars().all()) == 1
