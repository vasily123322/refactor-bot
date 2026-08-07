from app.services.llm.test_generation import (
    DEFAULT_TEST_GENERATION_TOPIC,
    build_test_generation_history_input,
    build_test_generation_payload,
    build_test_generation_prompt_key,
    normalize_test_generation_topic,
)


def test_normalize_test_generation_topic_uses_default_for_empty_text():
    assert normalize_test_generation_topic("   ") == DEFAULT_TEST_GENERATION_TOPIC
    assert normalize_test_generation_topic(None) == DEFAULT_TEST_GENERATION_TOPIC


def test_normalize_test_generation_topic_truncates_long_text():
    topic = "а" * 700

    assert normalize_test_generation_topic(topic) == "а" * 500


def test_build_test_generation_prompt_key_is_channel_specific():
    assert build_test_generation_prompt_key(42) == "ai_settings_test:42"


def test_build_test_generation_payload_trims_text_for_draft():
    assert build_test_generation_payload("  готовый пост  ") == {
        "type": "text",
        "text": "готовый пост",
    }


def test_build_test_generation_history_input_is_stable_and_short():
    assert build_test_generation_history_input("  запуск функции  ") == "Тест генерации: запуск функции"
    assert build_test_generation_history_input("а" * 700) == "Тест генерации: " + "а" * 500
