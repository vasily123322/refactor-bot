from app.services.llm.source_digest import (
    build_source_digest_context,
    build_source_digest_history_input,
    build_source_digest_instruction,
    build_source_digest_payload,
    build_source_digest_prompt_key,
    clean_source_text,
    summarize_source_digest_modes,
)


def test_clean_source_text_collapses_whitespace_and_truncates():
    text = "  hello\n\n world   " + ("x" * 2000)

    cleaned = clean_source_text(text, limit=20)

    assert "\n" not in cleaned
    assert cleaned.startswith("hello world")
    assert cleaned.endswith("…")
    assert len(cleaned) == 20


def test_build_source_digest_context_formats_items_and_skips_empty():
    context = build_source_digest_context(
        [
            {
                "source": "@one",
                "url": "https://t.me/one/1",
                "text": "Первый пост",
                "mode": "rewrite",
                "citation_enabled": True,
            },
            {"source": "empty", "text": ""},
            {"source": "site", "url": "https://example.com", "text": "Статья"},
        ]
    )

    assert "Источник 1: @one (https://t.me/one/1)" in context
    assert "Режим: рерайт" in context
    assert "Можно дать ссылку/упоминание источника" in context
    assert "Первый пост" in context
    assert "empty" not in context
    assert "Источник 3: site" in context


def test_summarize_source_digest_modes_counts_modes_and_citations():
    summary = summarize_source_digest_modes(
        [
            {"mode": "summary", "citation_enabled": False},
            {"mode": "rewrite", "citation_enabled": True},
            {"mode": "paraphrase", "citation_enabled": True},
            {"mode": "unknown", "citation_enabled": False},
        ]
    )

    assert summary["modes"] == {"summary": 1, "rewrite": 1, "paraphrase": 1, "custom": 1}
    assert summary["citation_count"] == 2


def test_build_source_digest_instruction_mentions_item_count_safety_modes_and_citations():
    instruction = build_source_digest_instruction(
        3,
        modes={"summary": 1, "rewrite": 1, "paraphrase": 1},
        citation_count=2,
    )

    assert "3 материалов" in instruction
    assert "не выдумывай" in instruction
    assert "саммари: 1" in instruction
    assert "Цитирование включено у 2" in instruction


def test_build_source_digest_payload_prompt_key_and_history_input_are_stable():
    assert build_source_digest_payload("  пост  ") == {"type": "text", "text": "пост"}
    assert build_source_digest_prompt_key(7, "abc") == build_source_digest_prompt_key(7, "abc")
    assert build_source_digest_prompt_key(7, "abc").startswith("source_digest:7:")
    assert build_source_digest_history_input(3, "abc") == "Дайджест источников: 3 материалов, контекст 3 символов"
