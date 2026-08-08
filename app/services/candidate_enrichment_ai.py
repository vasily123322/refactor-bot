from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai_generation import AIGenerationService
from app.services.candidate_enrichment import CandidateEnrichmentError
from app.services.candidate_enrichment_llm import LLMJsonCandidateEnricher
from app.services.channel_ai_completion import (
    ChannelAICompletionError,
    ChannelAICompletionService,
)


class ChannelAIEnrichmentProviderFactory:
    """Build strict Inbox enrichment on the shared channel AI runtime boundary."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        generation: AIGenerationService | None = None,
    ) -> None:
        self.runtime = ChannelAICompletionService(session, generation=generation)

    async def build(self, channel_id: int) -> LLMJsonCandidateEnricher:
        try:
            prepared = await self.runtime.prepare(
                channel_id=int(channel_id),
                mode="summary",
                temperature_default=0.2,
                temperature_cap=0.35,
                max_tokens_floor=128,
                max_tokens_cap=900,
            )
        except ChannelAICompletionError as exc:
            raise CandidateEnrichmentError(str(exc)) from exc

        async def completion(messages: list[dict[str, str]]) -> str:
            if len(messages) != 2:
                raise CandidateEnrichmentError("invalid enrichment prompt shape")
            try:
                return await prepared.complete(
                    system_prompt=str(messages[0].get("content") or ""),
                    user_prompt=str(messages[1].get("content") or ""),
                )
            except ChannelAICompletionError as exc:
                raise CandidateEnrichmentError("AI enrichment completion failed") from exc

        return LLMJsonCandidateEnricher(
            completion=completion,
            model=prepared.model,
            provider_name="channel_ai",
        )
