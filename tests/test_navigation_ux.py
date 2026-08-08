from app.bot.keyboards.navigation import home_dashboard_kb, home_dashboard_text
from app.bot.keyboards.posting import post_actions, settings_menu_kb
from app.core.callbacks import CB


def _labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _callbacks(markup) -> list[str]:
    return [
        str(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


def test_callback_enum_formats_to_wire_value() -> None:
    assert str(CB.POST_PICK_CH_PREFIX) == "post_pick_ch_"
    assert f"{CB.POST_PICK_CH_PREFIX}42" == "post_pick_ch_42"
    assert f"{CB.POST_TIMER_PRESET_PREFIX}300" == "post_timer_preset_300"


def test_inline_dashboard_exposes_core_product_actions() -> None:
    keyboard = home_dashboard_kb()
    callbacks = set(_callbacks(keyboard))
    assert {
        CB.GM_CREATE_POST.value,
        CB.GM_DRAFT.value,
        CB.CP_OPEN.value,
        CB.GM_EDIT_POST.value,
        CB.GM_SETTINGS.value,
        CB.GM_ADD_CHANNEL.value,
    }.issubset(callbacks)
    assert "Нижнее меню скрыто" in home_dashboard_text(inline_mode=True)


def test_editor_has_single_button_control_in_edit_mode() -> None:
    keyboard = post_actions(
        has_media=True,
        has_buttons=True,
        has_text=True,
        edit_mode=True,
    )
    labels = _labels(keyboard)
    assert labels.count("✅ Кнопки") == 1
    assert "🖼 Медиа" in labels
    assert "💾 Сохранить" in labels
    assert len(keyboard.inline_keyboard) == 2


def test_editor_uses_action_oriented_labels() -> None:
    keyboard = post_actions(
        has_media=False,
        has_buttons=False,
        has_text=True,
        edit_mode=False,
        ai_enabled=True,
    )
    labels = _labels(keyboard)
    assert "🔘 Кнопки" in labels
    assert "🔗 Превью" in labels
    assert "✨ AI" in labels
    assert "⚙️ Параметры" in labels
    assert "📅 По расписанию" in labels
    assert "🕒 Отложить" in labels
    assert "🚀 Опубликовать" in labels


def test_publication_settings_show_current_state() -> None:
    keyboard = settings_menu_kb(
        repeat_on=True,
        time_seconds=3600,
        notify_on=False,
        comments_on=True,
        pin_on=True,
        autosign_on=False,
    )
    labels = _labels(keyboard)
    assert "🔁 Автоповтор: вкл" in labels
    assert "🗑 Удаление: 1ч" in labels
    assert "🔕 Звук" in labels
    assert "✅ Комментарии" in labels
    assert "✅ Закрепить" in labels
    assert "☑️ Автоподпись" in labels
