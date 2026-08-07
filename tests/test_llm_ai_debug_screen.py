"""Tests for app/services/llm/ai_debug_screen.py."""

import pytest
from types import SimpleNamespace

from app.services.llm.ai_debug_screen import build_ai_debug_screen


def _make_ai_settings(**kwargs) -> SimpleNamespace:
    """Build a minimal ai_settings object."""
    defaults = {
        "preset_id": None,
        "custom_prompt": "",
        "user_prompt_template": "",
        "filters": {},
        "temperature": None,
        "max_tokens": None,
        "moderation_enabled": None,
        "tone": "",
        "length": "",
        "emoji_level": "",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestBuildAiDebugScreen:
    def test_contains_header(self):
        s = _make_ai_settings()
        text = build_ai_debug_screen(s)
        assert "Что влияет на генерацию" in text
        assert "🧠" in text

    def test_includes_channel_name(self):
        s = _make_ai_settings()
        text = build_ai_debug_screen(s, channel_name="Тест канал")
        assert "Тест канал" in text

    def test_preset_shown(self):
        s = _make_ai_settings(preset_id="news_v2")
        text = build_ai_debug_screen(s)
        assert "Навык ИИ" in text
        assert "news_v2" in text

    def test_custom_prompt_shown(self):
        s = _make_ai_settings(custom_prompt="Пиши как журналист")
        text = build_ai_debug_screen(s)
        assert "Свой промпт" in text
        assert "журналист" in text

    def test_default_prompt_when_nothing_set(self):
        s = _make_ai_settings()
        text = build_ai_debug_screen(s)
        assert "Дефолтный" in text

    def test_model_profile_shown(self):
        s = _make_ai_settings(filters={"ai_model_profile": "economy"})
        text = build_ai_debug_screen(s)
        assert "Быстро" in text or "⚡" in text

    def test_publication_profile_shown(self):
        s = _make_ai_settings(filters={"publication_profile": "news"})
        text = build_ai_debug_screen(s)
        assert "Новости" in text or "📰" in text

    def test_memory_fields_shown(self):
        s = _make_ai_settings(
            filters={
                "memory": {
                    "brand": "ZOO",
                    "audience": "Разработчики",
                    "cta": "Подписывайтесь!",
                }
            }
        )
        text = build_ai_debug_screen(s)
        assert "ZOO" in text
        assert "Разработчики" in text
        assert "Подписывайтесь" in text

    def test_memory_examples_shown(self):
        s = _make_ai_settings(
            filters={
                "memory": {
                    "good_post_examples": [
                        "Привет! Сегодня у нас новость…",
                        "🔥 Горячая подборка недели",
                    ]
                }
            }
        )
        text = build_ai_debug_screen(s)
        assert "Примеры постов" in text
        assert "Привет" in text
        assert "Горячая" in text

    def test_empty_memory_message(self):
        s = _make_ai_settings()
        text = build_ai_debug_screen(s)
        assert "Память не заполнена" in text

    def test_temperature_shown_when_set(self):
        s = _make_ai_settings(temperature=0.7)
        text = build_ai_debug_screen(s)
        assert "Температура" in text
        assert "0.7" in text

    def test_max_tokens_shown_when_set(self):
        s = _make_ai_settings(max_tokens=4096)
        text = build_ai_debug_screen(s)
        assert "Макс. токенов" in text
        assert "4096" in text

    def test_moderation_shown(self):
        s = _make_ai_settings(moderation_enabled=True)
        text = build_ai_debug_screen(s)
        assert "Модерация" in text
        assert "вкл" in text

    def test_custom_prompt_truncated_to_120_chars(self):
        long_prompt = "А" * 200
        s = _make_ai_settings(custom_prompt=long_prompt)
        text = build_ai_debug_screen(s)
        # Should be truncated with …
        assert "…" in text


class TestBuildAiDebugScreenMultiline:
    """Edge cases with multi-line memory and special chars."""

    def test_memory_empty_string_treated_as_empty(self):
        s = _make_ai_settings(filters={"brand": "", "audience": "  "})
        text = build_ai_debug_screen(s)
        assert "Память не заполнена" in text

    def test_good_post_examples_as_string(self):
        s = _make_ai_settings(
            filters={"memory": {"good_post_examples": "Один пример поста"}}
        )
        text = build_ai_debug_screen(s)
        assert "Один пример" in text

    def test_filters_none_safe(self):
        s = _make_ai_settings(filters=None)
        text = build_ai_debug_screen(s)
        assert "Дефолтный" in text
