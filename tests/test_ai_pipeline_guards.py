from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.ai_generation import AIGenerationService


class _AIRepo:
    def __init__(self, settings) -> None:
        self.settings = settings

    async def get_or_create(self, channel_id: int):
        return self.settings


def _settings(*, enabled: bool = True, day_used: int = 0, month_used: int = 0):
    return SimpleNamespace(
        enabled=enabled,
        tokens_used_day=day_used,
        tokens_used_month=month_used,
    )


def test_generic_pipeline_rejects_disabled_ai_before_prompt_build() -> None:
    async def run() -> None:
        fake = SimpleNamespace(ai_repo=_AIRepo(_settings(enabled=False)))
        result = await AIGenerationService.run_pipeline(
            fake, channel_id=1, mode="from_scratch", topic="hello"
        )
        assert result["success"] is False
        assert "отключен" in result["error"]

    asyncio.run(run())


def test_generic_pipeline_rejects_day_limit_before_generation() -> None:
    async def effective_limits(settings, channel_id):
        return 100, 1000, 512

    async def run() -> None:
        fake = SimpleNamespace(
            ai_repo=_AIRepo(_settings(day_used=100)),
            _effective_limits=effective_limits,
        )
        result = await AIGenerationService.run_pipeline(
            fake, channel_id=1, mode="from_scratch", topic="hello"
        )
        assert result["success"] is False
        assert "дневной" in result["error"]

    asyncio.run(run())


def test_generic_pipeline_applies_plan_request_cap() -> None:
    captured: dict = {}

    async def effective_limits(settings, channel_id):
        return None, None, 512

    async def build_prompt(channel_id, **kwargs):
        return {"max_tokens": 4000}, "model", "system", "user"

    async def generate_with_model(**kwargs):
        captured.update(kwargs)
        return {"success": True, "text": "ok", "tokens_used": 3, "error": None}

    async def postprocess(text: str, *, ai_settings: dict):
        return text

    async def run() -> None:
        fake = SimpleNamespace(
            ai_repo=_AIRepo(_settings()),
            _effective_limits=effective_limits,
            build_prompt=build_prompt,
            generate_with_model=generate_with_model,
            postprocess=postprocess,
        )
        result = await AIGenerationService.run_pipeline(
            fake, channel_id=1, mode="from_scratch", topic="hello"
        )
        assert result["success"] is True
        assert captured["ai_settings"]["max_tokens"] == 512

    asyncio.run(run())
