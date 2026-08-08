from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai_generation import AIGenerationService
from app.services.candidate_enrichment import CandidateEnrichmentError
from app.services.candidate_enrichment_llm import LLMJsonCandidateEnricher


class ChannelAIEnrichmentProviderFactory:
    """Build the strict Inbox LLM provider on top of the existing AI runtime.

    This class deliberately owns no HTTP client or API key. It reuses
    AIGenerationService so model routing, credential resolution, pooled transport,
    retries, token accounting and plan limits stay on the existing production path.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        generation: AIGenerationService | None = None,
    ) -> None:
        self.session = session
        self.generation = generation or AIGenerationService(session)

    async def build(self, channel_id: int) -> LLMJsonCandidateEnricher:
        ai_settings = await self.generation.ai_repo.get_or_create(int(channel_id))
        if not bool(getattr(ai_settings, "enabled", False)):
            raise CandidateEnrichmentError("AI is disabled for this channel")

        day_limit, month_limit, request_cap = await self.generation._effective_limits(
            ai_settings,
            int(channel_id),
        )
        if day_limit is not None and int(ai_settings.tokens_used_day or 0) >= int(
            day_limit
        ):
            raise CandidateEnrichmentError("daily AI token limit exceeded")
        if month_limit is not None and int(ai_settings.tokens_used_month or 0) >= int(
            month_limit
        ):
            raise CandidateEnrichmentError("monthly AI token limit exceeded")

        model = self.generation._pick_model(ai_settings, mode="summary")
        if not str(model or "").strip():
            raise CandidateEnrichmentError("AI model is not configured")

        settings_snapshot: dict[str, Any] = {
            key: value
            for key, value in dict(ai_settings.__dict__).items()
            if not key.startswith("_sa_")
        }
        settings_snapshot["channel_id"] = int(channel_id)
        settings_snapshot["temperature"] = min(
            max(float(settings_snapshot.get("temperature") or 0.2), 0.0),
            0.35,
        )
        settings_snapshot["max_tokens"] = min(
            max(128, int(settings_snapshot.get("max_tokens") or 700)),
            int(request_cap),
            900,
        )

        async def completion(messages: list[dict[str, str]]) -> str:
            if len(messages) != 2:
                raise CandidateEnrichmentError("invalid enrichment prompt shape")
            system_prompt = str(messages[0].get("content") or "")
            user_prompt = str(messages[1].get("content") or "")
            result = await self.generation.generate_with_model(
                ai_settings=settings_snapshot,
                model=str(model),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            if not bool(result.get("success")):
                # Do not propagate provider/API error bodies into candidate runs or API
                # responses. AIGeneration/OpenRouter already owns operational logging.
                raise CandidateEnrichmentError("AI enrichment completion failed")
            text = str(result.get("text") or "").strip()
            if not text:
                raise CandidateEnrichmentError("AI enrichment returned an empty response")
            return text

        return LLMJsonCandidateEnricher(
            completion=completion,
            model=str(model),
            provider_name="channel_ai",
        )
