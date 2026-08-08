from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.core.callbacks import CB


def _format_duration_label(total_seconds: int | None) -> str:
    if not total_seconds or total_seconds <= 0:
        return "нет"
    total_minutes = total_seconds // 60
    days = total_minutes // (24 * 60)
    rem_minutes = total_minutes % (24 * 60)
    hours = rem_minutes // 60
    minutes = rem_minutes % 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes and (days == 0 or hours > 0):
        parts.append(f"{minutes} мин")
    if not parts:
        parts.append("<1ч")
    return " ".join(parts)


def post_actions(
    *,
    for_video_note: bool = False,
    has_media: bool = False,
    has_buttons: bool = False,
    has_text: bool | None = None,
    notify_on: bool | None = None,
    autosign_on: bool | None = None,
    pin_on: bool | None = None,
    comments_on: bool | None = None,
    is_draft: bool | None = None,
    edit_mode: bool | None = None,
    ai_enabled: bool = True,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    buttons_label = "✅ Кнопки" if has_buttons else "🔘 Кнопки"
    media_label = "🔗 Превью" if (has_text and not has_media) else "🖼 Медиа"
    media_callback = (
        CB.PREVIEW_MENU
        if (has_text and not has_media)
        else (CB.POST_ADD_MEDIA if not has_media else CB.MEDIA_MENU)
    )
    rows.append(
        [
            InlineKeyboardButton(text=buttons_label, callback_data=CB.POST_ADD_BUTTON),
            InlineKeyboardButton(text=media_label, callback_data=media_callback),
        ]
    )

    # Published-post edit mode stays deliberately compact. The first row above
    # already contains the button/media controls, so do not duplicate them.
    if edit_mode:
        rows.append(
            [
                InlineKeyboardButton(text="← Назад", callback_data=CB.POST_BACK),
                InlineKeyboardButton(text="💾 Сохранить", callback_data=CB.POST_SEND),
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    if ai_enabled:
        rows.append([InlineKeyboardButton(text="✨ AI", callback_data=CB.POST_AI)])

    rows.append(
        [InlineKeyboardButton(text="⚙️ Параметры", callback_data=CB.POST_NEXT)]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="📅 По расписанию", callback_data=CB.POST_SCHEDULE
            ),
            InlineKeyboardButton(
                text="🕒 Отложить", callback_data=CB.POST_SETTINGS_DEFER
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="← Отмена", callback_data=CB.POST_BACK),
            InlineKeyboardButton(
                text="🚀 Опубликовать", callback_data=CB.POST_SETTINGS_PUBLISH
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_menu_kb(
    *,
    timer_set: bool = False,
    repeat_on: bool = False,
    time_seconds: int | None = None,
    views_value: int | None = None,
    notify_on: bool | None = None,
    autosign_on: bool | None = None,
    pin_on: bool | None = None,
    comments_on: bool | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if views_value and views_value > 0:
        label_v = (
            f"{views_value // 1000}к"
            if (views_value >= 1000 and views_value % 1000 == 0)
            else str(views_value)
        )
        first_label = f"👁 Удаление: {label_v} просмотров"
    elif time_seconds and time_seconds > 0:
        first_label = f"🗑 Удаление: {_format_duration_label(int(time_seconds))}"
    else:
        first_label = "🗑 Удаление: выкл"

    rows.append(
        [
            InlineKeyboardButton(
                text=("🔁 Автоповтор: вкл" if repeat_on else "🔁 Автоповтор: выкл"),
                callback_data=CB.POST_SETTINGS_REPEAT,
            ),
            InlineKeyboardButton(
                text="↗️ Переслать", callback_data=CB.POST_SETTINGS_FORWARD
            ),
        ]
    )
    rows.append(
        [InlineKeyboardButton(text=first_label, callback_data=CB.POST_SETTINGS_TIMER)]
    )

    def _check(on: bool | None) -> str:
        return "✅" if on else "☑️"

    if notify_on is not None or comments_on is not None:
        left = InlineKeyboardButton(
            text=(f"{'🔔' if (notify_on is None or notify_on) else '🔕'} Звук"),
            callback_data=CB.POST_TOGGLE_NOTIFY,
        )
        right = InlineKeyboardButton(
            text=(f"{_check(comments_on)} Комментарии"),
            callback_data=CB.POST_TOGGLE_COMMENTS,
        )
        rows.append([left, right])
    if pin_on is not None or autosign_on is not None:
        left = InlineKeyboardButton(
            text=(f"{_check(pin_on)} Закрепить"), callback_data=CB.POST_TOGGLE_PIN
        )
        right = InlineKeyboardButton(
            text=(f"{_check(autosign_on)} Автоподпись"),
            callback_data=CB.POST_TOGGLE_AUTOSIGN,
        )
        rows.append([left, right])

    rows.append(
        [
            InlineKeyboardButton(
                text="📅 По расписанию", callback_data=CB.POST_SCHEDULE
            ),
            InlineKeyboardButton(
                text="🕒 Отложить", callback_data=CB.POST_SETTINGS_DEFER
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="← Редактор", callback_data=CB.POST_SETTINGS_BACK
            ),
            InlineKeyboardButton(
                text="🚀 Опубликовать", callback_data=CB.POST_SETTINGS_PUBLISH
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- New: Create Post card (ad toggle + AI) ---
def create_post_card_kb(
    *, ad_on: bool, ads_available: bool = True, ai_available: bool = True
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    first_row: list[InlineKeyboardButton] = []
    if ads_available:
        mark = "✅" if ad_on else "☑️"
        first_row.append(
            InlineKeyboardButton(
                text=f"{mark} Это реклама", callback_data=CB.POST_TOGGLE_AD
            )
        )
    if ai_available:
        first_row.append(
            InlineKeyboardButton(text="✨ AI-ассистент", callback_data=CB.POST_AI_QUICK)
        )
    if first_row:
        rows.append(first_row)
    rows.append(
        [InlineKeyboardButton(text="← Главное меню", callback_data=CB.GM_GLOBAL_MENU)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ad_settings_presets_kb(*, active_preset: str | None = None) -> InlineKeyboardMarkup:
    def _btn(label: str) -> InlineKeyboardButton:
        key = label.replace("/", "_")
        star = "🔷 " if (active_preset == key) else ""
        return InlineKeyboardButton(
            text=f"{star}{label}", callback_data=f"{CB.POST_AD_PRESET_PREFIX}{key}"
        )

    rows = [[_btn("1/24"), _btn("2/48"), _btn("3/72")]]
    rows.append([InlineKeyboardButton(text="✏️ Редактор", callback_data=CB.POST_AD_EDIT)])
    rows.append(
        [
            InlineKeyboardButton(
                text="Время в топе: выкл.", callback_data=CB.POST_TOP_OPEN
            ),
            InlineKeyboardButton(
                text="Таймер удаления", callback_data=CB.POST_SETTINGS_TIMER
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="📅 По расписанию", callback_data=CB.POST_SETTINGS_DEFER
            ),
            InlineKeyboardButton(
                text="⚙️ Настройка брони", callback_data=CB.POST_AD_SETTINGS_OPEN
            ),
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="← Отмена", callback_data=CB.POST_AD_OPEN)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
