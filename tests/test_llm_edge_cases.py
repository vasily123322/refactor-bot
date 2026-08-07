"""Edge-case tests for LLM service helpers."""
from __future__ import annotations

from types import SimpleNamespace

from app.services.llm.generation_history import remember_generation
from app.services.llm.generated_payload import apply_generated_text_to_payload, extract_payload_text
from app.services.llm.settings_summary import build_ai_settings_summary
from app.services.llm.test_generation import normalize_test_generation_topic


# ── settings_summary edge cases ──────────────────────────────────────────────

def test_build_ai_settings_summary_defaults_when_no_preset_no_custom():
    settings = SimpleNamespace(
        preset_id=None,
        custom_prompt=None,
        user_prompt_template=None,
        tone="friendly",
        length="medium",
        emoji_level=1,
        filters={},
    )

    text = build_ai_settings_summary(settings)

    assert "Промпт: Дефолт" in text
    assert "Модель: 🔧 Своя настройка" in text
    assert "Профиль: Обычный пост" in text
    assert "Память: пустая" in text


def test_build_ai_settings_summary_with_only_user_template():
    settings = SimpleNamespace(
        preset_id=None,
        custom_prompt=None,
        user_prompt_template="my template",
        tone="expert",
        length="long",
        emoji_level=0,
        filters={},
    )

    text = build_ai_settings_summary(settings)

    assert "Промпт: Пользовательский" in text


def test_build_ai_settings_summary_memory_with_empty_values():
    settings = SimpleNamespace(
        preset_id=None,
        custom_prompt=None,
        user_prompt_template=None,
        tone="friendly",
        length="medium",
        emoji_level=1,
        filters={
            "memory": {
                "brand": "",
                "audience": "   ",
                "facts": [],
                "forbidden": ["ok"],
                "cta": None,
            }
        },
    )

    text = build_ai_settings_summary(settings)

    assert "Память: 1 поля" in text


# ── test_generation edge cases ────────────────────────────────────────────────

def test_normalize_test_generation_topic_preserves_short_text():
    topic = "пост о запуске"
    assert normalize_test_generation_topic(topic) == topic


def test_normalize_test_generation_topic_strips_whitespace():
    assert normalize_test_generation_topic("  тема  ") == "тема"


# ── generated_payload edge cases ──────────────────────────────────────────────

def test_apply_generated_text_to_payload_empty_payload_becomes_text():
    result = apply_generated_text_to_payload({}, "hello")
    assert result == {"type": "text", "text": "hello"}


def test_apply_generated_text_to_payload_none_payload_becomes_text():
    result = apply_generated_text_to_payload(None, "hello")
    assert result == {"type": "text", "text": "hello"}


def test_apply_generated_text_to_payload_preserves_extra_keys():
    payload = {"type": "photo", "caption": "old", "file_id": "abc"}
    result = apply_generated_text_to_payload(payload, "new caption")
    assert result["file_id"] == "abc"
    assert result["caption"] == "new caption"
    assert result["type"] == "photo"


def test_extract_payload_text_from_text_post():
    assert extract_payload_text({"type": "text", "text": "hello"}) == "hello"


def test_extract_payload_text_from_media_post():
    assert extract_payload_text({"type": "photo", "caption": "pic"}) == "pic"


def test_extract_payload_text_empty_payload():
    assert extract_payload_text({}) == ""
    assert extract_payload_text(None) == ""


# ── generation_history edge cases ─────────────────────────────────────────────

def test_remember_generation_with_none_history():
    updated = remember_generation(
        None,
        mode="from_scratch",
        input_text="topic",
        generated_text="result",
    )

    assert len(updated) == 1
    assert updated[0]["mode"] == "from_scratch"


def test_remember_generation_with_empty_history():
    updated = remember_generation(
        [],
        mode="improve",
        input_text="instr",
        generated_text="improved",
    )

    assert len(updated) == 1


def test_remember_generation_limit_1_keeps_only_latest():
    history = [
        {"mode": "old", "input": "old", "text": "old text"},
    ]

    updated = remember_generation(
        history,
        mode="new",
        input_text="new topic",
        generated_text="new text",
        limit=1,
    )

    assert len(updated) == 1
    assert updated[0]["mode"] == "new"
