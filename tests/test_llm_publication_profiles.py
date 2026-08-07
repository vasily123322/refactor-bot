from app.services.llm.publication_profiles import (
    PUBLICATION_PROFILES,
    apply_publication_profile,
    build_publication_profile_text,
    get_publication_profile,
)


def test_get_publication_profile_defaults_and_validates_unknown_values():
    assert get_publication_profile({}) == "default"
    assert get_publication_profile({"publication_profile": "news"}) == "news"
    assert get_publication_profile({"publication_profile": "unknown"}) == "default"


def test_apply_publication_profile_preserves_unrelated_filter_keys():
    filters = {"ai_model_profile": "balanced", "memory": {"brand": "Yuby"}}

    updated = apply_publication_profile(filters, "analysis")

    assert updated["ai_model_profile"] == "balanced"
    assert updated["memory"]["brand"] == "Yuby"
    assert updated["publication_profile"] == "analysis"


def test_apply_publication_profile_default_removes_explicit_key_only():
    updated = apply_publication_profile(
        {"publication_profile": "sales", "ai_model_profile": "maximum"},
        "default",
    )

    assert "publication_profile" not in updated
    assert updated["ai_model_profile"] == "maximum"


def test_build_publication_profile_text_includes_profile_instruction():
    text = build_publication_profile_text({"publication_profile": "sales"})

    assert PUBLICATION_PROFILES["sales"]["title"] in text
    assert "продающий пост" in text
