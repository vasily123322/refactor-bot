"""Helpers for channel AI test generation UX."""

DEFAULT_TEST_GENERATION_TOPIC = "Проверочный пост о том, как канал помогает подписчикам быстро понять пользу продукта"


def normalize_test_generation_topic(text: str | None) -> str:
    """Return a safe topic for channel AI settings test generation."""
    topic = (text or "").strip()
    if not topic:
        return DEFAULT_TEST_GENERATION_TOPIC
    return topic[:500]


def build_test_generation_prompt_key(channel_id: int) -> str:
    """Stable dialog key for AI settings test runs."""
    return f"ai_settings_test:{int(channel_id)}"


def build_test_generation_payload(text: str | None) -> dict[str, str]:
    """Convert a test generation result to a text-post payload."""
    clean = (text or "").strip()
    return {"type": "text", "text": clean}


def build_test_generation_history_input(topic: str | None) -> str:
    """Short input label for FSM generation history after opening a test draft."""
    return f"Тест генерации: {normalize_test_generation_topic(topic)}"
