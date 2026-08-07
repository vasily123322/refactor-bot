import asyncio
from types import SimpleNamespace

from app.services.ai_generation import AIGenerationService
from app.services.llm.model_profiles import AI_MODEL_PROFILES, apply_model_profile, resolve_model_for_mode


def test_apply_model_profile_stores_mode_overrides_without_losing_existing_filters():
    filters = {"memory": {"brand": "Yuby"}}

    updated = apply_model_profile(filters, "quality")

    assert updated["memory"] == {"brand": "Yuby"}
    assert updated["ai_model_profile"] == "quality"
    assert updated["ai_models"] == AI_MODEL_PROFILES["quality"]["models"]


def test_resolve_model_for_mode_prefers_override_then_default_model():
    settings = SimpleNamespace(
        model="default/model",
        filters={"ai_models": {"summary": "summary/model", "from_scratch": "scratch/model"}},
    )

    assert resolve_model_for_mode(settings, "summary") == "summary/model"
    assert resolve_model_for_mode(settings, "from-scratch") == "scratch/model"
    assert resolve_model_for_mode(settings, "rewrite") == "default/model"


def test_ai_generation_pick_model_uses_shared_resolver():
    settings = SimpleNamespace(
        model="default/model",
        filters={"ai_models": {"summary": "summary/model"}},
    )
    service = object.__new__(AIGenerationService)

    assert service._pick_model(settings, "summary") == "summary/model"


class _FailingLLM:
    async def chat(self, **kwargs):
        return {"success": False, "error": "boom", "text": None, "tokens_used": 0}


def test_call_openrouter_messages_returns_llm_errors(monkeypatch):
    from app.services.ai_generation import settings as ai_settings_module

    monkeypatch.setattr(ai_settings_module, "openrouter_api_key", "test-key")
    service = object.__new__(AIGenerationService)
    service.llm = _FailingLLM()

    result = asyncio.run(
        service._call_openrouter_messages(
            [{"role": "user", "content": "hi"}],
            model="x/model",
            temperature=0.1,
            top_p=0.9,
            max_tokens=100,
        )
    )

    assert result == {"success": False, "error": "boom", "text": None, "tokens_used": 0}
