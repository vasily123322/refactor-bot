from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai_generation import AIGenerationService


class ChannelAICompletionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedChannelAICompletion:
    generation: AIGenerationService
    model: str
    ai_settings: dict[str, Any]

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        result = await self.generation.generate_with_model(
            ai_settings=self.ai_settings,
            model=self.model,
            system_prompt=str(system_prompt),
            user_prompt=str(user_prompt),
        )
        if not bool(result.get("success")):
            # The legacy/OpenRouter layer owns operational error logging. Never let
            # provider response bodies or credentials escape into Studio/domain runs.
            raise ChannelAICompletionError("channel AI completion failed")
        text = str(result.get("text") or "").strip()
        if not text:
            raise ChannelAICompletionError("channel AI returned an empty response")
        return text


class ChannelAICompletionService:
    """Single compatibility boundary around the existing channel AI runtime.

    Higher-level domain providers should not know how plan limits, model overrides,
    AIGeneration settings snapshots or the production credential path are resolved.
    Any future cleanup of AIGenerationService private compatibility methods is isolated
    here instead of duplicated across enrichment/rewrite features.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        generation: AIGenerationService | None = None,
    ) -> None:
        self.session = session
        self.generation = generation or AIGenerationService(session)

    async def prepare(
        self,
        *,
        channel_id: int,
        mode: str,
        temperature_default: float,
        temperature_cap: float,
        max_tokens_floor: int,
        max_tokens_cap: int,
    ) -> PreparedChannelAICompletion:
        channel_id = int(channel_id)
        ai_settings = await self.generation.ai_repo.get_or_create(channel_id)
        if not bool(getattr(ai_settings, "enabled", False)):
            raise ChannelAICompletionError("AI is disabled for this channel")

        day_limit, month_limit, request_cap = await self.generation._effective_limits(
            ai_settings,
            channel_id,
        )
        if day_limit is not None and int(ai_settings.tokens_used_day or 0) >= int(
            day_limit
        ):
            raise ChannelAICompletionError("daily AI token limit exceeded")
        if month_limit is not None and int(ai_settings.tokens_used_month or 0) >= int(
            month_limit
        ):
            raise ChannelAICompletionError("monthly AI token limit exceeded")

        model = str(self.generation._pick_model(ai_settings, mode=str(mode)) or "").strip()
        if not model:
            raise ChannelAICompletionError("AI model is not configured")

        snapshot: dict[str, Any] = {
            key: value
            for key, value in dict(ai_settings.__dict__).items()
            if not key.startswith("_sa_")
        }
        snapshot["channel_id"] = channel_id

        current_temperature = float(
            snapshot.get("temperature")
            if snapshot.get("temperature") is not None
            else temperature_default
        )
        snapshot["temperature"] = min(
            max(current_temperature, 0.0),
            max(0.0, float(temperature_cap)),
        )

        request_cap_int = max(1, int(request_cap))
        configured_tokens = int(snapshot.get("max_tokens") or max_tokens_floor)
        desired_tokens = max(int(max_tokens_floor), configured_tokens)
        snapshot["max_tokens"] = min(
            desired_tokens,
            request_cap_int,
            max(1, int(max_tokens_cap)),
        )

        return PreparedChannelAICompletion(
            generation=self.generation,
            model=model,
            ai_settings=snapshot,
        )
