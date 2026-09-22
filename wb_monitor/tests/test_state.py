"""Самозахват админа: бот запоминает первого, кто нажал /start."""

from __future__ import annotations

from wb_monitor.state import ADMIN_CHAT_ID_KEY, AdminRef, get_setting


async def test_first_claim_wins(session_factory):
    admin = AdminRef(session_factory)
    assert not admin.is_set

    assert await admin.claim(555) is True
    assert admin.chat_id == 555

    # второй желающий уже опоздал
    assert await admin.claim(777) is False
    assert admin.chat_id == 555


async def test_claim_survives_restart(session_factory):
    await AdminRef(session_factory).claim(555)

    after_restart = AdminRef(session_factory)
    assert await after_restart.load() == 555
    assert after_restart.is_set


async def test_env_value_pins_admin(session_factory):
    """ADMIN_CHAT_ID в .env — значит, никакой самозахват не нужен."""
    admin = AdminRef(session_factory, env_value=42)
    assert admin.is_pinned_by_env
    assert await admin.claim(999) is False
    assert admin.chat_id == 42

    async with session_factory() as session:
        assert await get_setting(session, ADMIN_CHAT_ID_KEY) is None


async def test_env_value_survives_stored_value(session_factory):
    await AdminRef(session_factory).claim(555)
    pinned = AdminRef(session_factory, env_value=42)
    assert await pinned.load() == 42


async def test_setting_upsert_overwrites(session_factory):
    from wb_monitor.state import set_setting

    async with session_factory() as session:
        await set_setting(session, "k", "v1")
        await set_setting(session, "k", "v2")
        assert await get_setting(session, "k") == "v2"
