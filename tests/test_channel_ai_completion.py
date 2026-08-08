from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.channel_ai_completion import (
    ChannelAICompletionError,
    ChannelAICompletionService,
)


class _Repo:
    def __init__(self, settings) -> None:
        self.settings = settings

    async def get_or_create(self, channel_id: int):
        assert channel_id == 501
        return self.settings


class _Generation:
    def __init__(self, settings, *, result=None, limits=(1000, 10_000, 700)) -> None:
        self.ai_repo = _Repo(settings)
        self.result = result or {
            "success": True,
            "text": "completion text",
            "tokens_used": 22,
        }
        self.limits = limits
        self.pick_modes: list[str] = []
        self.calls: list[dict] = []

    async def _effective_limits(self, ai_settings, channel_id: int):
        assert ai_settings is self.ai_repo.settings
        assert channel_id == 501
        return self.limits

    def _pick_model(self, ai_settings, mode: str) -> str:
        assert ai_settings is self.ai_repo.settings
        self.pick_modes.append(mode)
        return ai_settings.model

    async def generate_with_model(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _settings(**overrides):
    values = {
        "enabled": True,
        "model": "provider/model",
        "tokens_used_day": 10,
        "tokens_used_month": 20,
        "temperature": 0.9,
        "max_tokens": 5000,
        "top_p": 0.9,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_shared_runtime_applies_limits_model_policy_and_generic_completion_errors() -> None:
    async def run() -> None:
        generation = _Generation(_settings())
        prepared = await ChannelAICompletionService(
            None, generation=generation  # type: ignore[arg-type]
        ).prepare(
            channel_id=501,
            mode="summary",
            temperature_default=0.2,
            temperature_cap=0.35,
            max_tokens_floor=128,
            max_tokens_cap=900,
        )
        assert prepared.model == "provider/model"
        assert prepared.ai_settings["channel_id"] == 501
        assert prepared.ai_settings["temperature"] == 0.35
        assert prepared.ai_settings["max_tokens"] == 700
        assert generation.pick_modes == ["summary"]

        text = await prepared.complete(system_prompt="system", user_prompt="user")
        assert text == "completion text"
        assert generation.calls[0]["model"] == "provider/model"
        assert generation.calls[0]["system_prompt"] == "system"
        assert generation.calls[0]["user_prompt"] == "user"

        generation.result = {
            "success": False,
            "text": None,
            "tokens_used": 0,
            "error": "credential=SHOULD_NOT_ESCAPE",
        }
        with pytest.raises(ChannelAICompletionError) as exc_info:
            await prepared.complete(system_prompt="system", user_prompt="user")
        assert "SHOULD_NOT_ESCAPE" not in str(exc_info.value)
        assert "completion failed" in str(exc_info.value)

    asyncio.run(run())


def test_shared_runtime_fails_closed_for_disabled_limits_and_request_cap_below_floor() -> None:
    async def run() -> None:
        disabled = _Generation(_settings(enabled=False))
        with pytest.raises(ChannelAICompletionError, match="disabled"):
            await ChannelAICompletionService(
                None, generation=disabled  # type: ignore[arg-type]
            ).prepare(
                channel_id=501,
                mode="rewrite",
                temperature_default=0.4,
                temperature_cap=0.55,
                max_tokens_floor=256,
                max_tokens_cap=1200,
            )

        daily = _Generation(_settings(tokens_used_day=1000))
        with pytest.raises(ChannelAICompletionError, match="daily"):
            await ChannelAICompletionService(
                None, generation=daily  # type: ignore[arg-type]
            ).prepare(
                channel_id=501,
                mode="summary",
                temperature_default=0.2,
                temperature_cap=0.35,
                max_tokens_floor=128,
                max_tokens_cap=900,
            )

        small_cap = _Generation(_settings(max_tokens=5000), limits=(None, None, 64))
        prepared = await ChannelAICompletionService(
            None, generation=small_cap  # type: ignore[arg-type]
        ).prepare(
            channel_id=501,
            mode="rewrite",
            temperature_default=0.4,
            temperature_cap=0.55,
            max_tokens_floor=256,
            max_tokens_cap=1200,
        )
        assert prepared.ai_settings["max_tokens"] == 64

    asyncio.run(run())


def test_domain_ai_factories_no_longer_call_generation_private_policy_methods_directly() -> None:
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "app/services/candidate_enrichment_ai.py",
        "app/services/candidate_rewrite_ai.py",
    ):
        content = (root / relative).read_text(encoding="utf-8")
        assert "._effective_limits" not in content
        assert "._pick_model" not in content
