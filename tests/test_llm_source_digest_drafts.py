"""Tests for auto-digest → multiple drafts feature."""
from __future__ import annotations

from app.services.llm.source_digest import (
    build_source_digest_instruction,
)


def test_build_source_digest_instruction_variant_for_analysis():
    instruction = build_source_digest_instruction(
        3,
        modes={"summary": 2, "rewrite": 1},
        citation_count=1,
        variant="analysis",
    )

    assert "3 материалов" in instruction
    assert "разбор" in instruction.lower()
    assert "структуру" in instruction.lower() or "пункт" in instruction.lower()


def test_build_source_digest_instruction_variant_for_news():
    instruction = build_source_digest_instruction(
        2,
        modes={"summary": 2},
        citation_count=0,
        variant="news",
    )

    assert "новост" in instruction.lower()
    assert "факт" in instruction.lower() or "событ" in instruction.lower()


def test_build_source_digest_instruction_variant_for_sales():
    instruction = build_source_digest_instruction(
        1,
        modes={"rewrite": 1},
        citation_count=1,
        variant="sales",
    )

    assert "прода" in instruction.lower() or "оффер" in instruction.lower()
    assert "CTA" in instruction or "призыв" in instruction.lower()


def test_build_source_digest_instruction_variant_for_meme():
    instruction = build_source_digest_instruction(
        4,
        modes={"paraphrase": 4},
        citation_count=0,
        variant="meme",
    )

    assert "лёгк" in instruction.lower() or "мем" in instruction.lower()
    assert "токсичн" in instruction.lower()


def test_build_source_digest_instruction_default_variant():
    instruction = build_source_digest_instruction(
        2,
        modes={"summary": 2},
        citation_count=0,
    )

    assert "черновик" in instruction.lower() or "пост" in instruction.lower()


def test_build_source_digest_instruction_single_item_uses_correct_form():
    instruction = build_source_digest_instruction(
        1,
        modes={"summary": 1},
        citation_count=0,
    )

    assert "1 материала" in instruction


def test_build_source_digest_instruction_five_plus_uses_plural():
    instruction = build_source_digest_instruction(
        5,
        modes={"summary": 5},
        citation_count=0,
    )

    assert "5 материалов" in instruction
