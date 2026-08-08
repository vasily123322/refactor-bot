from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.callbacks import CB


def home_dashboard_text(*, inline_mode: bool = False) -> str:
    suffix = (
        "\n\nНижнее меню скрыто — все основные действия доступны кнопками ниже."
        if inline_mode
        else "\n\nВыберите действие в нижнем меню. Внутри разделов бот использует компактные inline-экраны."
    )
    return (
        "🧭 <b>Управление каналами</b>\n\n"
        "Создавайте и планируйте публикации, редактируйте посты, управляйте каналами и настройками из одного места."
        + suffix
    )


def home_dashboard_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✍️ Новый пост", callback_data=CB.GM_CREATE_POST
                ),
                InlineKeyboardButton(text="📝 Черновик", callback_data=CB.GM_DRAFT),
            ],
            [
                InlineKeyboardButton(text="📅 Контент-план", callback_data=CB.CP_OPEN),
                InlineKeyboardButton(
                    text="✏️ Редактировать", callback_data=CB.GM_EDIT_POST
                ),
            ],
            [
                InlineKeyboardButton(text="⚙️ Настройки", callback_data=CB.GM_SETTINGS),
                InlineKeyboardButton(
                    text="➕ Канал / чат", callback_data=CB.GM_ADD_CHANNEL
                ),
            ],
        ]
    )
