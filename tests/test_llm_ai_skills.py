from types import SimpleNamespace

from app.services.llm.ai_skills import (
    build_ai_skill_button_rows,
    build_ai_skills_menu_text,
    build_ai_skill_set_message,
    normalize_ai_skill_title,
)


def test_normalize_ai_skill_title_strips_emoji_and_uses_fallback():
    assert normalize_ai_skill_title("📰 Новости") == "Новости"
    assert normalize_ai_skill_title("   ") == "Навык ИИ"


def test_build_ai_skills_menu_text_shows_active_skill_and_descriptions():
    presets = [
        SimpleNamespace(id=1, title="📰 Новости", description="короткий новостной пост"),
        SimpleNamespace(id=2, title="💼 Продажи", description="оффер и CTA"),
    ]

    text = build_ai_skills_menu_text(presets, current_preset_id=2)

    assert "🧠 Навыки ИИ" in text
    assert "Текущий навык: Продажи" in text
    assert "☑️ Новости — короткий новостной пост" in text
    assert "✅ Продажи — оффер и CTA" in text
    assert "пресет" not in text.lower()


def test_build_ai_skill_button_rows_marks_current_and_adds_back():
    presets = [
        SimpleNamespace(id=1, title="📰 Новости", description=""),
        SimpleNamespace(id=2, title="💼 Продажи", description=""),
        SimpleNamespace(id=3, title="✍️ Рерайт", description=""),
    ]

    rows = build_ai_skill_button_rows(presets, channel_id=777, current_preset_id=2)

    assert rows == [
        [
            ("☑️ Новости", "ai_set_preset_777_1"),
            ("✅ Продажи", "ai_set_preset_777_2"),
        ],
        [("☑️ Рерайт", "ai_set_preset_777_3")],
        [("← Назад", "neu_text_777")],
    ]


def test_build_ai_skill_set_message_uses_product_terms():
    preset = SimpleNamespace(id=2, title="💼 Продажи", description="")

    assert build_ai_skill_set_message(preset) == "✅ Навык ИИ: Продажи"
    assert build_ai_skill_set_message(None) == "✅ Навык ИИ выбран"


def test_build_ai_skills_menu_text_handles_empty_presets():
    text = build_ai_skills_menu_text([], current_preset_id=None)

    assert "Текущий навык: не выбран" in text
    assert "Доступные навыки" not in text


def test_build_ai_skills_menu_text_handles_presets_without_description():
    presets = [SimpleNamespace(id=1, title="📰 Новости", description="")]

    text = build_ai_skills_menu_text(presets, current_preset_id=1)

    assert "✅ Новости" in text
    assert "—" not in text.split("✅ Новости")[0].split("\n")[-1]


def test_build_ai_skill_button_rows_empty_presets():
    rows = build_ai_skill_button_rows([], channel_id=1, current_preset_id=None)

    assert rows == [[("← Назад", "neu_text_1")]]
