from app.services.llm.model_profiles import (
    build_model_profile_button_rows,
    build_model_profile_menu_text,
    build_model_profile_set_message,
    get_model_profile_menu_items,
)


def test_get_model_profile_menu_items_has_three_profiles_plus_custom_in_order():
    items = get_model_profile_menu_items()
    codes = [code for code, _ in items]

    assert codes == ["economy", "balanced", "quality", "custom"]
    assert codes[-1] == "custom"


def test_build_model_profile_menu_text_shows_current_and_descriptions():
    text = build_model_profile_menu_text(current_profile="balanced")

    assert "🤖 Режим качества" in text
    assert "Текущий режим: ⚖️ Качественно" in text
    assert "Быстро" in text
    assert "Максимум" in text
    assert "Своя настройка" in text
    assert "эконом" not in text.lower()


def test_build_model_profile_menu_text_handles_missing_current():
    text = build_model_profile_menu_text(current_profile=None)

    assert "Текущий режим: не выбран" in text


def test_build_model_profile_button_rows_marks_active_and_excludes_custom():
    rows = build_model_profile_button_rows(channel_id=42, current_profile="quality")

    flat = [btn for row in rows for btn in row]
    labels = [label for label, _ in flat]
    callbacks = [cb for _, cb in flat]

    assert "✅ 🧠 Максимум" in labels
    assert "☑️ ⚡ Быстро" in labels
    assert "☑️ ⚖️ Качественно" in labels
    assert all("custom" not in cb for cb in callbacks)
    assert callbacks[-1] == "neu_text_42"


def test_build_model_profile_set_message_returns_title_for_known_code():
    assert build_model_profile_set_message("economy") == "✅ Режим: ⚡ Быстро"
    assert build_model_profile_set_message("quality") == "✅ Режим: 🧠 Максимум"
    assert build_model_profile_set_message("custom") == "✅ Режим: 🔧 Своя настройка"
    assert build_model_profile_set_message("unknown") == "✅ Режим обновлён"
