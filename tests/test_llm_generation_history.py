from app.services.llm.generation_history import (
    build_similar_generation_instruction,
    remember_generation,
)


def test_remember_generation_keeps_latest_first_and_trims_text_and_history():
    history = [
        {"mode": "from_scratch", "input": "old topic", "text": "old text"},
        {"mode": "rewrite", "input": "older topic", "text": "older text"},
    ]

    updated = remember_generation(
        history,
        mode="from_link",
        input_text="https://example.com/article",
        generated_text="x" * 2500,
        limit=2,
    )

    assert len(updated) == 2
    assert updated[0]["mode"] == "from_link"
    assert updated[0]["input"] == "https://example.com/article"
    assert updated[0]["text"] == "x" * 2000
    assert updated[1]["text"] == "old text"


def test_remember_generation_ignores_empty_text_and_invalid_history_items():
    updated = remember_generation(
        [{"text": "kept"}, "bad", None],
        mode="from_scratch",
        input_text="topic",
        generated_text="   ",
    )

    assert updated == [{"text": "kept"}]


def test_build_similar_generation_instruction_reuses_style_not_facts():
    instruction = build_similar_generation_instruction(
        "Старый пост про скидки и доставку",
        user_hint="тема: запуск новой функции",
    )

    assert "похожий" in instruction.lower()
    assert "структуру" in instruction.lower()
    assert "не копируй факты" in instruction.lower()
    assert "запуск новой функции" in instruction
    assert "Старый пост про скидки" in instruction
