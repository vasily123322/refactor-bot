import asyncio
from types import SimpleNamespace

from app.services.llm.channel_memory import (
    append_channel_memory_example,
    build_channel_memory_edit_template,
    build_channel_memory_text,
    clear_channel_memory,
    parse_channel_memory_input,
    upsert_channel_memory,
)
from app.services.llm.prompt_builder import PromptBuilder


class _PresetsRepo:
    async def get_by_id(self, preset_id):
        return None


class _CustomRepo:
    async def get_active(self, channel_id):
        return None


def test_prompt_builder_injects_channel_memory_from_filters():
    settings = SimpleNamespace(
        channel_id=123,
        tone="friendly",
        length="medium",
        emoji_level=1,
        lang="ru",
        hashtags_enabled=False,
        cta_enabled=False,
        preset_id=None,
        custom_prompt=None,
        user_prompt_template=None,
        filters={
            "publication_profile": "analysis",
            "memory": {
                "brand": "Yuby",
                "audience": "админы Telegram-каналов",
                "style": "коротко, уверенно, без канцелярита",
                "facts": ["бот помогает вести каналы"],
                "forbidden": ["гарантированный доход"],
                "cta": "Напишите админу для подключения",
                "examples": ["Пост с коротким хуком и мягким CTA"],
            }
        },
    )

    system_prompt, user_prompt = asyncio.run(
        PromptBuilder(_PresetsRepo(), _CustomRepo()).build(
            settings,
            topic="новая функция",
        )
    )

    combined = system_prompt + "\n" + user_prompt
    assert "Память канала" in combined
    assert "Yuby" in combined
    assert "админы Telegram-каналов" in combined
    assert "гарантированный доход" in combined
    assert "Напишите админу для подключения" in combined
    assert "Пост с коротким хуком" in combined
    assert "Профиль публикации" in combined
    assert "разбор" in combined.lower()


def test_upsert_channel_memory_appends_examples_without_losing_existing_filters():
    filters = {"ai_model_profile": "balanced", "memory": {"brand": "Yuby"}}

    updated = upsert_channel_memory(filters, examples=["пример 1", "пример 2"])

    assert updated["ai_model_profile"] == "balanced"
    assert updated["memory"]["brand"] == "Yuby"
    assert updated["memory"]["examples"] == ["пример 1", "пример 2"]


def test_build_channel_memory_text_skips_empty_values_and_formats_lists():
    text = build_channel_memory_text(
        {
            "brand": "Yuby",
            "audience": "владельцы каналов",
            "style": "без воды",
            "facts": ["есть автопостинг", "есть ИИ"],
            "forbidden": ["обещать результат"],
            "links": ["https://example.com"],
            "cta": "Подключиться",
            "empty": "",
        }
    )

    assert "Бренд/проект: Yuby" in text
    assert "Факты: есть автопостинг; есть ИИ" in text
    assert "Запрещено/избегать: обещать результат" in text
    assert "empty" not in text


def test_append_channel_memory_example_trims_dedupes_and_keeps_last_examples():
    filters = {
        "ai_model_profile": "balanced",
        "memory": {
            "brand": "Yuby",
            "examples": ["старый 1", "старый 2", "старый 3", "дубликат"],
        },
    }

    updated = append_channel_memory_example(
        filters,
        "  дубликат  ",
        limit=3,
        max_chars=20,
    )
    updated = append_channel_memory_example(
        updated,
        "новый пример с длинным хвостом",
        limit=3,
        max_chars=20,
    )

    assert updated["ai_model_profile"] == "balanced"
    assert updated["memory"]["brand"] == "Yuby"
    assert updated["memory"]["examples"] == [
        "старый 3",
        "дубликат",
        "новый пример с длинн",
    ]


def test_parse_channel_memory_input_accepts_labeled_lines_and_preserves_filters():
    parsed = parse_channel_memory_input(
        """
        Бренд: Yuby
        Аудитория: админы Telegram-каналов
        Стиль: коротко, уверенно
        Факты: есть автопостинг; есть ИИ-редактор
        Запрещено: гарантированный доход, непроверенные факты
        CTA: Напишите админу
        Ссылки: https://example.com, @demo
        """
    )

    assert parsed == {
        "brand": "Yuby",
        "audience": "админы Telegram-каналов",
        "style": "коротко, уверенно",
        "facts": ["есть автопостинг", "есть ИИ-редактор"],
        "forbidden": ["гарантированный доход", "непроверенные факты"],
        "cta": "Напишите админу",
        "links": ["https://example.com", "@demo"],
    }


def test_parse_channel_memory_input_collects_multiline_good_post_examples():
    parsed = parse_channel_memory_input(
        """
        Бренд: Yuby
        Примеры:
        ---
        Хук: как админам экономить время
        Основная мысль: бот сам готовит черновик
        CTA: открыть тест генерации
        ---
        Второй пример с двоеточием: сохраняем как один пост
        """
    )

    assert parsed["brand"] == "Yuby"
    assert parsed["examples"] == [
        "Хук: как админам экономить время\nОсновная мысль: бот сам готовит черновик\nCTA: открыть тест генерации",
        "Второй пример с двоеточием: сохраняем как один пост",
    ]


def test_build_channel_memory_edit_template_prefills_existing_values_and_examples():
    template = build_channel_memory_edit_template(
        {
            "brand": "Yuby",
            "audience": "админы каналов",
            "facts": ["есть автопостинг", "есть источники"],
            "examples": ["Хороший пост: короткий хук\nCTA в конце"],
        }
    )

    assert "Бренд: Yuby" in template
    assert "Аудитория: админы каналов" in template
    assert "Факты: есть автопостинг; есть источники" in template
    assert "---" in template
    assert "Хороший пост: короткий хук\nCTA в конце" in template


def test_clear_channel_memory_preserves_unrelated_filters():
    filters = {
        "ai_model_profile": "balanced",
        "publication_profile": "analysis",
        "memory": {"brand": "Yuby"},
    }

    updated = clear_channel_memory(filters)

    assert updated == {
        "ai_model_profile": "balanced",
        "publication_profile": "analysis",
    }


def test_append_channel_memory_example_ignores_too_short_examples():
    filters = {"memory": {"examples": ["длинный пример поста"]}}

    updated = append_channel_memory_example(filters, "мало", min_chars=10)

    assert updated == filters
