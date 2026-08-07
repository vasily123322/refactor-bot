from __future__ import annotations

from typing import Any

from app.services.llm.channel_memory import get_channel_memory
from app.services.llm.model_profiles import AI_MODEL_PROFILES, get_model_profile
from app.services.llm.publication_profiles import PUBLICATION_PROFILES, get_publication_profile


def _memory_field_count(memory: dict[str, Any]) -> int:
    count = 0
    for value in memory.values():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple, set, dict)) and not value:
            continue
        count += 1
    return count


def build_ai_settings_summary(ai_settings: Any) -> str:
    """Compact user-facing summary for the channel AI text settings menu."""

    filters = dict(getattr(ai_settings, "filters", {}) or {})
    model_profile = get_model_profile(filters)
    publication_profile = get_publication_profile(filters)
    memory = get_channel_memory(filters)
    memory_count = _memory_field_count(memory)

    has_preset = bool(getattr(ai_settings, "preset_id", None))
    has_custom = bool((getattr(ai_settings, "custom_prompt", "") or "").strip()) or bool(
        (getattr(ai_settings, "user_prompt_template", "") or "").strip()
    )
    prompt_label = "Навыки ИИ" if has_preset else ("Пользовательский" if has_custom else "Дефолт")
    model_label = AI_MODEL_PROFILES.get(model_profile, {}).get("title", "Своя настройка")
    publication_label = PUBLICATION_PROFILES.get(publication_profile, PUBLICATION_PROFILES["default"])["title"]
    memory_label = f"{memory_count} поля" if memory_count else "пустая"

    return (
        "Текущие настройки:\n"
        f"• Промпт: {prompt_label}\n"
        f"• Модель: {model_label}\n"
        f"• Профиль: {publication_label}\n"
        f"• Память: {memory_label}\n"
        f"• Тон: {getattr(ai_settings, 'tone', '—')} · "
        f"Длина: {getattr(ai_settings, 'length', '—')} · "
        f"Эмодзи: {getattr(ai_settings, 'emoji_level', '—')}"
    )
