"""Detailed "what affects generation" debug screen for channel admins."""

from __future__ import annotations

from typing import Any

from app.services.llm.channel_memory import get_channel_memory
from app.services.llm.model_profiles import AI_MODEL_PROFILES, get_model_profile
from app.services.llm.publication_profiles import PUBLICATION_PROFILES, get_publication_profile


def build_ai_debug_screen(ai_settings: Any, *, channel_name: str = "") -> str:
    """Return multi-line text showing everything that affects AI generation.

    Designed for an admin debug screen so the user can understand *why*
    the AI writes the way it does.
    """
    filters = dict(getattr(ai_settings, "filters", {}) or {})
    model_profile = get_model_profile(filters)
    publication_profile = get_publication_profile(filters)
    memory = get_channel_memory(filters)

    # --- Model ---
    model_info = AI_MODEL_PROFILES.get(model_profile, {})
    model_title = model_info.get("title", "🔧 Своя настройка")
    model_desc = model_info.get("description", "")

    # --- Publication profile ---
    pub_info = PUBLICATION_PROFILES.get(publication_profile, PUBLICATION_PROFILES["default"])
    pub_title = pub_info["title"]
    pub_desc = pub_info.get("description", "")

    # --- AI Skill / Prompt ---
    has_preset = bool(getattr(ai_settings, "preset_id", None))
    has_custom = bool((getattr(ai_settings, "custom_prompt", "") or "").strip()) or bool(
        (getattr(ai_settings, "user_prompt_template", "") or "").strip()
    )
    if has_preset:
        prompt_label = f"📌 Навык ИИ (preset: {ai_settings.preset_id})"
    elif has_custom:
        custom_text = (ai_settings.custom_prompt or "") or (ai_settings.user_prompt_template or "")
        preview = custom_text[:120].replace("\n", " ")
        if len(custom_text) > 120:
            preview += "…"
        prompt_label = f"🔧 Свой промпт: {preview}"
    else:
        prompt_label = "🔹 Дефолтный промпт"

    # --- Memory ---
    memory_lines: list[str] = []
    mem_examples = memory.get("good_post_examples") or []
    if isinstance(mem_examples, str):
        mem_examples = [mem_examples]
    brand = memory.get("brand", "")
    audience = memory.get("audience", "")
    cta = memory.get("cta", "")
    tone = memory.get("tone", "")
    length = memory.get("length", "")
    emoji_level = memory.get("emoji_level", "")

    if brand:
        memory_lines.append(f"Бренд: {brand}")
    if audience:
        memory_lines.append(f"Аудитория: {audience}")
    if cta:
        memory_lines.append(f"CTA: {cta}")
    if tone:
        memory_lines.append(f"Тон: {tone}")
    if length:
        memory_lines.append(f"Длина: {length}")
    if emoji_level:
        memory_lines.append(f"Эмодзи: {emoji_level}")

    if mem_examples:
        numbered = []
        for i, ex in enumerate(mem_examples, 1):
            ex_preview = ex[:150].replace("\n", " ")
            if len(ex) > 150:
                ex_preview += "…"
            numbered.append(f"  {i}. {ex_preview}")
        memory_lines.append("Примеры постов:")
        memory_lines.extend(numbered)
    elif not memory_lines:
        memory_lines.append("Память не заполнена")

    # --- Other settings ---
    other_lines: list[str] = []
    if getattr(ai_settings, "temperature", None) is not None:
        other_lines.append(f"Температура: {ai_settings.temperature}")
    if getattr(ai_settings, "max_tokens", None):
        other_lines.append(f"Макс. токенов: {ai_settings.max_tokens}")
    moderation = getattr(ai_settings, "moderation_enabled", None)
    if moderation is not None:
        other_lines.append(f"Модерация: {'вкл' if moderation else 'выкл'}")
    other_text = "\n".join(other_lines) if other_lines else "—"

    # --- Compose ---
    header = "🧠 Что влияет на генерацию"
    if channel_name:
        header += f" ({channel_name})"

    parts = [
        f"**{header}**\n",
        "━━━━━━━━━━━━━━━━━━━━━",
        "",
        "💡 **Промпт**",
        f"  {prompt_label}",
        "",
        "🤖 **Модель**",
        f"  • Профиль: {model_title}",
    ]
    if model_desc:
        parts.append(f"  • {model_desc}")

    parts += [
        "",
        "📋 **Профиль публикации**",
        f"  • {pub_title}",
    ]
    if pub_desc:
        parts.append(f"  • {pub_desc}")

    parts += [
        "",
        "🧩 **Память канала**",
    ]
    for line in memory_lines:
        parts.append(f"  • {line}" if not line.startswith("  ") else line)

    parts += [
        "",
        "⚙️ **Дополнительно**",
        f"  {other_text}",
    ]

    return "\n".join(parts)
