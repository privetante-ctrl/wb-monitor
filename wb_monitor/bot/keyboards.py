"""Клавиатуры бота. Все действия — кнопками, команды остаются как запасной путь."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# --- тексты кнопок главного меню (по ним же ловим нажатия) ---
BTN_ADD = "➕ Добавить товар"
BTN_LIST = "📋 Мои товары"
BTN_CHECK = "🔄 Проверить сейчас"
BTN_REPORT = "📊 Отчёт"
BTN_STATUS = "⚙️ Статус"
BTN_HELP = "❓ Помощь"


def main_menu(is_admin: bool = True) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_LIST)],
        [KeyboardButton(text=BTN_CHECK), KeyboardButton(text=BTN_REPORT)],
        [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_HELP)],
    ]
    if not is_admin:  # клиент: только смотреть свои товары и забирать отчёт
        rows = [
            [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_REPORT)],
            [KeyboardButton(text=BTN_HELP)],
        ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def kind_choice(nm_id: str) -> InlineKeyboardMarkup:
    """«Это свой товар или конкурент?» после того, как карточка найдена."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏠 Мой товар", callback_data=f"add:own:{nm_id}"),
            InlineKeyboardButton(text="🎯 Конкурент", callback_data=f"add:rival:{nm_id}"),
        ],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="add:cancel:0")],
    ])


def sku_actions(sku_id: int, is_active: bool) -> InlineKeyboardMarkup:
    toggle = "⏸ Снять с отслеживания" if is_active else "▶️ Вернуть в отслеживание"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle, callback_data=f"sku:toggle:{sku_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"sku:delete:{sku_id}")],
    ])


def confirm_delete(sku_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Да, удалить", callback_data=f"sku:delete_yes:{sku_id}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"sku:delete_no:{sku_id}"),
        ],
    ])
