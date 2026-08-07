from __future__ import annotations

from typing import Tuple, Dict, Any

from app.services.llm.channel_memory import build_channel_memory_text, get_channel_memory
from app.services.llm.publication_profiles import build_publication_profile_text


class PromptBuilder:
    """Строит системный и пользовательский промпты с подстановкой переменных.
    Учитывает сценарии: from_scratch, improve (original_text), from_link (article_text, source_url).
    """

    def __init__(self, presets_repo, custom_repo) -> None:
        self.presets_repo = presets_repo
        self.custom_repo = custom_repo

    async def build(
        self,
        ai_settings: Any,
        *,
        topic: str,
        extra_vars: Dict[str, Any] | None = None,
        mode: str | None = None,
    ) -> Tuple[str, str]:
        extra_vars = extra_vars or {}
        # Правила тона
        tone_rules_map = {
            "friendly": "Пиши простыми словами, дружелюбно. Избегай канцелярита. Эмодзи 0–2 по делу.",
            "expert": "Точность важнее креатива. Без эмодзи. Терминологию поясняй коротко. Никаких голословных утверждений.",
            "conversational": "Легкий разговорный тон. Допусти риторические вопросы. Эмодзи до 3, не в каждом предложении.",
            "official": "Строго, нейтрально, без эмоциональных окрасов и эмодзи. Краткие, информативные формулировки.",
            "storytelling": "Начни с хука. Дай мини‑историю: проблема → поворот → вывод. Эмодзи максимум 1.",
            "promo": "Четко обозначь пользу. Ограничься фактами. Ясный CTA в конце. Без ложного дефицита.",
        }
        current_tone = getattr(ai_settings, "tone", "friendly")
        tone_rules_text = tone_rules_map.get(
            current_tone, tone_rules_map.get("friendly", "")
        )

        # Мягкие локальные преднастройки длины/эмодзи по тону
        base_length = getattr(ai_settings, "length", "medium")
        base_emoji_level = int(getattr(ai_settings, "emoji_level", 1) or 0)
        if current_tone in ("expert", "official"):
            effective_length = (
                base_length if base_length in ("short", "medium") else "short"
            )
            effective_emoji_level = 0
        elif current_tone in ("promo",):
            effective_length = "short"
            effective_emoji_level = max(1, base_emoji_level)
        elif current_tone in ("friendly", "conversational", "storytelling"):
            effective_length = (
                base_length if base_length in ("medium", "short") else "medium"
            )
            effective_emoji_level = max(1, base_emoji_level)
        else:
            effective_length = base_length
            effective_emoji_level = base_emoji_level

        instruction = (extra_vars.get("instruction") or "").strip()
        has_original = bool(extra_vars.get("original_text"))
        has_article = bool(extra_vars.get("article_text"))

        if has_original:
            default_system = (
                f"Ты — редактор текстов для Telegram. Задача: {instruction} исходный текст. "
                "Сохраняй факты и смысл, убирай воду, делай структуру удобочитаемой. "
                "Соблюдай {tone}, {length}, {emoji_level}, язык {lang}. Ничего не выдумывай.\n"
                "Правила тона: {tone_rules}"
            )
            default_user = (
                "Исходный текст:\n{original_text}\n\n"
                "Контекст (опц.):\n"
                "Цель: {goal}\n"
                "Аудитория: {audience}\n"
                "Тон: {tone}; Длина: {length}; Эмодзи: {emoji_level}; Язык: {lang}\n\n"
                "Инструкция:\n{instruction}\n\n"
                "Перепиши с выходной структурой:\n"
                "1) Хук 1–2 строки.\n"
                "2) 3–6 коротких абзацев или список.\n"
                "3) Отдельной строкой CTA (если есть {cta}).\n"
                "4) Хэштеги (если есть {hashtags}).\n"
                "Ссылки из {links} интегрируй по месту краткими подписями."
            )
        elif has_article:
            default_system = (
                "Ты — копирайтер для Telegram. На основе статьи создай пост. "
                "Сохраняй факты, не придумывай. Стиль: {tone}, длина: {length}, "
                "эмодзи: {emoji_level}, язык: {lang}. Если информации мало — пиши нейтрально и безопасно.\n"
                "Правила тона: {tone_rules}"
            )
            default_user = (
                "Статья/текст источника:\n{article_text}\n\n"
                "Источник: {source_url}\n"
                "Тема: {topic}\n"
                "Цель: {goal}\n"
                "Аудитория: {audience}\n"
                "CTA: {cta}\n"
                "Хэштеги: {hashtags}\n"
                "Ссылки: {links}\n"
                "Тон: {tone}; Длина: {length}; Эмодзи: {emoji_level}; Язык: {lang}\n\n"
                "Сформируй пост со структурой:\n"
                "1) Хук 1–2 строки.\n"
                "2) 3–6 коротких абзацев или список (ключевые выводы из статьи).\n"
                "3) Отдельной строкой CTA (если задан).\n"
                "4) Хэштеги (если заданы)."
            )
        else:
            default_system = (
                "Ты — автор постов для Telegram‑каналов. Пиши ясно и по делу, без упоминания, что ты ИИ. "
                "Соблюдай заданные {tone}, {length}, {emoji_level}, язык {lang}. Не выдумывай факты.\n"
                "Правила тона: {tone_rules}"
            )
            default_user = (
                "Тема: {topic}\n"
                "Цель поста: {goal}\n"
                "Аудитория: {audience}\n"
                "Тон: {tone}; Длина: {length}; Эмодзи: {emoji_level}; Язык: {lang}\n"
                "CTA: {cta}\n"
                "Хэштеги: {hashtags}\n"
                "Ссылки: {links}\n\n"
                "Сформируй пост со структурой:\n"
                "1) Хук 1–2 строки.\n"
                "2) Основная часть: 3–6 коротких абзацев или список (1–2 предложения на пункт).\n"
                "3) Отдельной строкой CTA, если задан.\n"
                "4) В конце хэштеги, если заданы."
            )

        force_custom = bool(extra_vars.get("force_custom", False))
        active_custom = (
            await self.custom_repo.get_active(ai_settings.channel_id)
            if hasattr(ai_settings, "channel_id")
            else None
        )

        if force_custom and (active_custom or ai_settings.custom_prompt):
            system_template = ai_settings.custom_prompt
            user_template = ai_settings.user_prompt_template or default_user
        elif active_custom:
            system_template = active_custom.content
            user_template = ai_settings.user_prompt_template or default_user
        elif ai_settings.preset_id:
            preset = await self.presets_repo.get_by_id(ai_settings.preset_id)
            if preset:
                system_template = preset.system_prompt
                user_template = preset.user_template
            else:
                system_template = default_system
                user_template = default_user
        elif ai_settings.custom_prompt:
            system_template = ai_settings.custom_prompt
            user_template = ai_settings.user_prompt_template or default_user
        else:
            system_template = default_system
            user_template = default_user

        tone_labels = {
            "friendly": "дружелюбный",
            "expert": "экспертный",
            "official": "официальный",
            "provocative": "провокационный",
        }
        length_labels = {
            "short": "короткий (до 500 символов)",
            "medium": "средний (500-1200 символов)",
            "long": "длинный (1200+ символов)",
        }

        filters = getattr(ai_settings, "filters", {}) or {}
        channel_memory_text = build_channel_memory_text(get_channel_memory(filters))
        publication_profile_text = build_publication_profile_text(filters)

        variables = {
            "topic": topic,
            "tone": tone_labels.get(current_tone, current_tone),
            "length": length_labels.get(effective_length, effective_length),
            "emoji_level": str(effective_emoji_level),
            "lang": getattr(ai_settings, "lang", "ru"),
            "tone_rules": tone_rules_text,
            "hashtags": f"Добавь {getattr(ai_settings, 'hashtags_count', 3)} хештега"
            if getattr(ai_settings, "hashtags_enabled", True)
            else "Без хештегов",
            "cta": "Добавь призыв к действию в конце"
            if getattr(ai_settings, "cta_enabled", True)
            else "",
            "goal": extra_vars.get("goal", ""),
            "brand": extra_vars.get("brand", ""),
            "audience": extra_vars.get("audience", "подписчики канала"),
            "channel_title": extra_vars.get("channel_title", ""),
            "channel_memory": channel_memory_text,
            "instruction": instruction,
            "original_text": extra_vars.get("original_text", ""),
            "article_text": extra_vars.get("article_text", ""),
            "source_url": extra_vars.get("source_url", ""),
            "links": extra_vars.get("links", ""),
            "schedule": extra_vars.get("schedule", ""),
            "date": extra_vars.get("date", ""),
            "time": extra_vars.get("time", ""),
            "city": extra_vars.get("city", ""),
        }

        class _SafeDict(dict):
            def __missing__(self, key):  # type: ignore[override]
                return ""

        def _safe_format(tpl: str, vars_: dict) -> str:
            try:
                return tpl.format_map(_SafeDict(vars_))
            except Exception:
                return tpl

        system_prompt = _safe_format(system_template, variables)
        user_prompt = _safe_format(user_template, variables)
        if publication_profile_text and publication_profile_text not in system_prompt:
            system_prompt = f"{system_prompt}\n\n{publication_profile_text}"
        if channel_memory_text and channel_memory_text not in system_prompt:
            system_prompt = f"{system_prompt}\n\n{channel_memory_text}"
        return system_prompt, user_prompt
