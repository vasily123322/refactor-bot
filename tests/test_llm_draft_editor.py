"""Tests for app/services/llm/draft_editor.py."""

import pytest

from app.services.llm.draft_editor import (
    DRAFT_EDIT_ACTIONS,
    DraftEditAction,
    build_extra_system_note,
    build_instruction_for,
    get_action,
)


class TestDraftEditActionsRegistry:
    """Registry completeness and uniqueness."""

    def test_all_callbacks_unique(self):
        callbacks = [a.callback for a in DRAFT_EDIT_ACTIONS]
        assert len(callbacks) == len(set(callbacks)), "Duplicate callbacks found"

    def test_all_labels_nonempty(self):
        for action in DRAFT_EDIT_ACTIONS:
            assert action.label, f"Empty label for {action.callback}"

    def test_all_instructions_nonempty(self):
        for action in DRAFT_EDIT_ACTIONS:
            assert action.instruction, f"Empty instruction for {action.callback}"

    def test_expected_actions_present(self):
        callbacks = {a.callback for a in DRAFT_EDIT_ACTIONS}
        expected = {
            "ai_improve_shorten",
            "ai_improve_cta",
            "ai_improve_channel_style",
            "ai_improve_news",
            "ai_improve_analysis",
            "ai_improve_meme",
            "ai_improve_emoji",
            "ai_improve_style",
            "ai_improve_lengthen",
        }
        assert expected == callbacks


class TestGetAction:
    def test_returns_action_for_known_callback(self):
        action = get_action("ai_improve_shorten")
        assert action is not None
        assert action.callback == "ai_improve_shorten"
        assert "короче" in action.label or "Скороти" in action.instruction

    def test_returns_none_for_unknown(self):
        assert get_action("ai_improve_nonexistent") is None


class TestBuildInstructionFor:
    def test_shorten_instruction_mentions_shortening(self):
        instr = build_instruction_for("ai_improve_shorten")
        assert "короче" in instr or "Скороти" in instr or "корот" in instr.lower()

    def test_cta_instruction_mentions_cta(self):
        instr = build_instruction_for("ai_improve_cta")
        assert "CTA" in str(instr) or "призыв" in instr.lower() or "действи" in instr.lower()

    def test_channel_style_instruction_mentions_style(self):
        instr = build_instruction_for("ai_improve_channel_style")
        assert "стил" in instr.lower()

    def test_news_instruction_mentions_news(self):
        instr = build_instruction_for("ai_improve_news")
        assert "новост" in instr.lower()

    def test_analysis_instruction_mentions_analysis(self):
        instr = build_instruction_for("ai_improve_analysis")
        assert "разбор" in instr.lower() or "аналит" in instr.lower()

    def test_meme_instruction_mentions_humor(self):
        instr = build_instruction_for("ai_improve_meme")
        assert "юмор" in instr.lower() or "мем" in instr.lower() or "шутк" in instr.lower()

    def test_unknown_returns_fallback(self):
        instr = build_instruction_for("ai_improve_unknown")
        assert "Улучши текст" in instr


class TestBuildExtraSystemNote:
    def test_channel_style_has_extra_note(self):
        note = build_extra_system_note("ai_improve_channel_style")
        assert len(note) > 0

    def test_shorten_has_no_extra_note(self):
        note = build_extra_system_note("ai_improve_shorten")
        assert note == ""

    def test_unknown_returns_empty(self):
        assert build_extra_system_note("nonexistent") == ""
