from types import SimpleNamespace

from app.services.llm.settings_summary import build_ai_settings_summary


def test_build_ai_settings_summary_shows_current_channel_ai_choices():
    settings = SimpleNamespace(
        preset_id=42,
        tone="friendly",
        length="short",
        emoji_level=2,
        filters={
            "ai_model_profile": "quality",
            "publication_profile": "analysis",
            "memory": {
                "brand": "Yuby",
                "facts": ["есть автопостинг"],
                "examples": ["пример поста"],
            },
        },
    )

    text = build_ai_settings_summary(settings)

    assert "Навыки ИИ" in text
    assert "🧠 Максимум" in text
    assert "Разбор" in text
    assert "Память: 3 поля" in text
    assert "Тон: friendly" in text
    assert "Длина: short" in text
    assert "Эмодзи: 2" in text


def test_build_ai_settings_summary_handles_empty_filters_and_custom_prompt():
    settings = SimpleNamespace(
        preset_id=None,
        custom_prompt="system prompt",
        user_prompt_template=None,
        tone="expert",
        length="medium",
        emoji_level=0,
        filters={},
    )

    text = build_ai_settings_summary(settings)

    assert "Промпт: Пользовательский" in text
    assert "Модель: 🔧 Своя настройка" in text
    assert "Профиль: Обычный пост" in text
    assert "Память: пустая" in text
