import asyncio
from typing import Sequence

from app.core.db import AsyncSessionLocal
from app.repositories.ai_settings import AIPresetsRepo


PRESETS: Sequence[dict] = [
    {
        "code": "tg_summary",
        "title": "Новостной саммари",
        "description": "Короткое резюме статьи/источника для поста",
        "system_prompt": (
            "Ты — редактор и копирайтер для Telegram‑каналов. Подготовь пост,\n"
            "соблюдая тон {tone}, длину {length}, уровень эмодзи {emoji_level}, язык ru.\n\n"
            "Правила:\n"
            "- Точность: не придумывай фактов.\n"
            "- Структура: хук 1–2 строки; 3–6 тезисов; вывод/CTA (если есть).\n"
            "- Ссылки упоминай по месту краткими подписями.\n"
            "- Хештеги/CTA добавляй только если явно указано.\n"
            "- Правила тона: {tone_rules}"
        ),
        "user_template": (
            "Статья/источник:\n{article_text}\n\n"
            "Источник: {source_url}\n"
            "Тема: {topic}\n"
            "Тон: {tone}; Длина: {length}; Эмодзи: {emoji_level}\n\n"
            "Сформируй краткое саммари для поста."
        ),
    },
    {
        "code": "tg_rewrite",
        "title": "Рерайт",
        "description": "Переписать исходный текст, сохранив смысл",
        "system_prompt": (
            "Ты — редактор для Telegram. Улучши текст, сохранив факты и смысл.\n"
            "Соблюдай {tone}, {length}, {emoji_level}, язык ru. Правила тона: {tone_rules}"
        ),
        "user_template": (
            "Исходный текст:\n{original_text}\n\n"
            "Инструкция: {instruction}\n"
            "Тон: {tone}; Длина: {length}; Эмодзи: {emoji_level}"
        ),
    },
    {
        "code": "tg_paraphrase",
        "title": "Перефразирование",
        "description": "Сделать проще и чище, сохранив смысл",
        "system_prompt": (
            "Ты — редактор. Перефразируй так, чтобы стало проще читать, без потери смысла.\n"
            "Соблюдай {tone}, {length}, {emoji_level}, ru. Правила тона: {tone_rules}"
        ),
        "user_template": ("Перефразируй и упростай:\n{original_text}"),
    },
    {
        "code": "tg_story",
        "title": "Сторителлинг",
        "description": "Мини‑история: хук → поворот → вывод",
        "system_prompt": (
            "Ты — копирайтер Telegram. Напиши мини‑историю: хук, развитие, вывод.\n"
            "Соблюдай {tone}, {length}, {emoji_level}, ru. Правила тона: {tone_rules}"
        ),
        "user_template": (
            "Тема/событие: {topic}\nКонтекст: {goal}\nАудитория: {audience}"
        ),
    },
    {
        "code": "tg_promo",
        "title": "Промо/Оффер",
        "description": "Выгады, конкретика, понятный CTA",
        "system_prompt": (
            "Ты — копирайтер. Сделай промо‑пост: выгоды, факты, ясный CTA.\n"
            "Соблюдай {tone}, {length}, {emoji_level}, ru. Без кликбейта. Правила тона: {tone_rules}"
        ),
        "user_template": (
            "Продукт/оффер: {topic}\n"
            "Ключевые выгоды: {goal}\n"
            "Аудитория: {audience}\n"
            "CTA: {cta}"
        ),
    },
    {
        "code": "tg_explainer",
        "title": "Эксплейнер",
        "description": "Простое объяснение сложного",
        "system_prompt": (
            "Ты — автор объясняющих постов. Объясняй коротко и ясно, примеры по делу.\n"
            "Соблюдай {tone}, {length}, {emoji_level}, ru. Правила тона: {tone_rules}"
        ),
        "user_template": ("Тема: {topic}\nЧто нужно объяснить: {goal}"),
    },
    {
        "code": "tg_howto",
        "title": "Инструкция/How‑to",
        "description": "Шаги, чек‑лист, конкретные действия",
        "system_prompt": (
            "Ты — автор инструкций. Дай пошаговый план/чек‑лист, без воды.\n"
            "Соблюдай {tone}, {length}, {emoji_level}, ru. Правила тона: {tone_rules}"
        ),
        "user_template": ("Задача/цель: {goal}\nКонтекст: {topic}"),
    },
    {
        "code": "tg_case",
        "title": "Кейс/Разбор",
        "description": "Проблема → решение → результат",
        "system_prompt": (
            "Ты — редактор кейсов. Структура: контекст; проблема; решение; результат; вывод.\n"
            "Соблюдай {tone}, {length}, {emoji_level}, ru. Правила тона: {tone_rules}"
        ),
        "user_template": ("Контекст: {topic}\nЦель: {goal}"),
    },
    {
        "code": "tg_announce",
        "title": "Анонс мероприятия",
        "description": "Что, когда, где, зачем; кратко и ясно",
        "system_prompt": (
            "Ты — редактор. Сделай анонс: что, для кого, когда, где, как участвовать.\n"
            "Соблюдай {tone}, {length}, {emoji_level}, ru. Правила тона: {tone_rules}"
        ),
        "user_template": (
            "Мероприятие: {topic}\nДетали: {goal}\nСсылки/регистрация: {links}"
        ),
    },
    {
        "code": "tg_listicle",
        "title": "Список‑обзор",
        "description": "Подборка: 5–10 пунктов, короткие пояснения",
        "system_prompt": (
            "Ты — автор подборок. Сделай список с короткими пояснениями.\n"
            "Соблюдай {tone}, {length}, {emoji_level}, ru. Правила тона: {tone_rules}"
        ),
        "user_template": ("Тема подборки: {topic}\nКритерии/цель: {goal}"),
    },
]


async def main() -> None:
    async with AsyncSessionLocal() as session:
        repo = AIPresetsRepo(session)
        created, skipped = 0, 0
        for p in PRESETS:
            exists = await repo.get_by_code(p["code"])  # type: ignore[arg-type]
            if exists:
                skipped += 1
                continue
            await repo.create(
                code=p["code"],
                title=p["title"],
                system_prompt=p["system_prompt"],
                user_template=p["user_template"],
                description=p.get("description"),
                rules=None,
                defaults=None,
            )
            created += 1
        print(f"AI presets: created={created}, skipped={skipped}")


if __name__ == "__main__":
    asyncio.run(main())
