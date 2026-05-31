from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.core.callbacks import CB


def build_media_menu_kb(
    pos_label: str,
    spoiler_checked: bool,
    *,
    paid_on: bool | None = None,
    is_album: bool = False,
    price: int | None = None,
) -> InlineKeyboardMarkup:
    label_pos = (
        "Расположение: сверху" if pos_label != "bottom" else "Расположение: снизу"
    )
    label_sp = "✅ Спойлер" if spoiler_checked else "☑️ Спойлер"
    label_paid = "✅ Платный пост" if paid_on else "☑️ Платный пост"
    price_row = (
        [
            InlineKeyboardButton(
                text=(
                    f"Цена за пост: {price} ⭐"
                    if (paid_on and price)
                    else "Цена за пост"
                ),
                callback_data=CB.MEDIA_PAID_PRICE,
            )
        ]
        if paid_on
        else []
    )
    rows = [
        [InlineKeyboardButton(text=label_pos, callback_data=CB.MEDIA_POS_TOGGLE)],
        [InlineKeyboardButton(text=label_paid, callback_data=CB.MEDIA_PAID_TOGGLE)],
        (price_row if price_row else None),
        (
            None
            if paid_on
            else [
                InlineKeyboardButton(
                    text=label_sp, callback_data=CB.MEDIA_SPOILER_TOGGLE
                )
            ]
        ),
        [InlineKeyboardButton(text="← Назад", callback_data=CB.POST_BACK)],
    ]
    # Удалим None-строки
    rows = [r for r in rows if r]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_preview_menu_kb(
    show_above: bool, *, show_enabled: bool = True
) -> InlineKeyboardMarkup:
    """Меню «Превью» для текстовых постов: позиция превью и очистка.

    show_above=True — превью над текстом; False — под текстом.
    """
    label_show = "✅ Отображать" if show_enabled else "☑️ Отображать"
    label_pos = "Расположение: сверху" if show_above else "Расположение: внизу"
    rows = [
        [InlineKeyboardButton(text=label_show, callback_data=CB.PREVIEW_TOGGLE)],
        [InlineKeyboardButton(text=label_pos, callback_data=CB.PREVIEW_POS_TOGGLE)],
        [
            InlineKeyboardButton(
                text="Преобразовать в медиа", callback_data=CB.PREVIEW_CLEAR
            )
        ],
        [InlineKeyboardButton(text="← Назад", callback_data=CB.POST_BACK)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_back_kb() -> InlineKeyboardMarkup:
    from app.core.callbacks import CB

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data=CB.POST_BACK)]
        ]
    )


def build_rows(*rows: list[InlineKeyboardButton] | None) -> InlineKeyboardMarkup:
    """Собрать клавиатуру из переданных рядов, пропуская None."""
    items = [r for r in rows if r]
    return InlineKeyboardMarkup(inline_keyboard=items)


def build_menu(
    title_rows: list[tuple[str, str]], *, back_to: str | None = None
) -> InlineKeyboardMarkup:
    """Построить простое меню из (label, callback_data), опционально добавить назад."""
    rows = [
        [InlineKeyboardButton(text=label, callback_data=cb)] for label, cb in title_rows
    ]
    if back_to is not None:
        rows.append([InlineKeyboardButton(text="← Назад", callback_data=back_to)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_root_settings_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="Каналы/Чаты", callback_data="settings_channels_list"
            )
        ],
        [InlineKeyboardButton(text="Часовой пояс", callback_data="settings_tz_root")],
        [
            InlineKeyboardButton(
                text="Настройки интерфейса", callback_data="settings_ui_root"
            )
        ],
        [InlineKeyboardButton(text="Главное меню", callback_data=CB.GM_GLOBAL_MENU)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
