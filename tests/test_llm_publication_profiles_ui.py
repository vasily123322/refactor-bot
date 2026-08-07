from app.services.llm.publication_profiles import (
    build_publication_profile_button_rows,
    build_publication_profile_menu_text,
    build_publication_profile_set_message,
    get_publication_profile_menu_items,
)


def test_get_publication_profile_menu_items_returns_all_profiles_in_order():
    items = get_publication_profile_menu_items()
    codes = [code for code, _ in items]

    assert codes == ["default", "news", "sales", "analysis", "meme"]


def test_build_publication_profile_menu_text_shows_current_and_description():
    text = build_publication_profile_menu_text(current_profile="sales")

    assert "🧩 Профиль публикации" in text
    assert "Текущий: Продажи" in text
    assert "Сценарий: польза, возражения, мягкий призыв" in text
    assert "новость, продажи, разбор" in text


def test_build_publication_profile_menu_text_defaults_to_default():
    text = build_publication_profile_menu_text(current_profile=None)

    assert "Текущий: Обычный пост" in text


def test_build_publication_profile_button_rows_marks_active_and_adds_back():
    rows = build_publication_profile_button_rows(channel_id=99, current_profile="analysis")

    flat = [btn for row in rows for btn in row]
    labels = [label for label, _ in flat]
    callbacks = [cb for _, cb in flat]

    assert "✅ Разбор" in labels
    assert "☑️ Обычный пост" in labels
    assert "☑️ Новости" in labels
    assert all("publication_profile_99" in cb for cb in callbacks[:-1])
    assert callbacks[-1] == "neu_text_99"


def test_build_publication_profile_set_message_returns_title():
    assert build_publication_profile_set_message("news") == "✅ Профиль: Новости"
    assert build_publication_profile_set_message("default") == "✅ Профиль: Обычный пост"
    assert build_publication_profile_set_message("unknown") == "✅ Профиль обновлён"
