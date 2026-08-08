from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from loguru import logger

from app.core.config import settings
from app.services.ai_generation import AIGenerationService
from app.services.extractors.html import extract_article_text
from app.services.llm.openrouter_client import ChatResult, ChatStreamEvent


def _error_result(message: str) -> ChatResult:
    return {
        "success": False,
        "text": None,
        "tokens_used": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "error": message,
    }


def _float_setting(settings_dict: dict[str, Any], key: str, default: float) -> float:
    value = settings_dict.get(key)
    return float(default if value is None else value)


class InteractiveAIStreamingService:
    """Interactive streaming orchestration for Telegram editor actions.

    Background workers deliberately keep using the regular non-streaming pipeline.
    This service adds early quota checks, conversation memory and UI-safe streaming
    without coupling Telegram rendering to the LLM client.
    """

    def __init__(self, generation: AIGenerationService) -> None:
        self.generation = generation
        self.session = generation.session

    async def _guard(self, channel_id: int) -> tuple[ChatResult | None, int]:
        ai_settings = await self.generation.ai_repo.get_or_create(channel_id)
        if not bool(getattr(ai_settings, "enabled", False)):
            return _error_result("ИИ отключен для этого канала"), 0

        day_limit, month_limit, request_cap = await self.generation._effective_limits(
            ai_settings, channel_id
        )
        if day_limit is not None and int(ai_settings.tokens_used_day or 0) >= int(
            day_limit
        ):
            return _error_result("Превышен дневной лимит токенов"), request_cap
        if month_limit is not None and int(ai_settings.tokens_used_month or 0) >= int(
            month_limit
        ):
            return _error_result("Превышен месячный лимит токенов"), request_cap
        return None, int(request_cap)

    async def stream_pipeline(
        self,
        *,
        channel_id: int,
        mode: str,
        topic: str = "",
        original_text: str = "",
        url: str = "",
        instruction: str | None = None,
        extra: dict[str, Any] | None = None,
        user_id: int | None = None,
        prompt_key: str | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        guard_error, request_cap = await self._guard(channel_id)
        if guard_error is not None:
            yield {"type": "error", "result": guard_error}
            return

        ai_settings, model, system_prompt, user_prompt = (
            await self.generation.build_prompt(
                channel_id,
                mode=mode,
                topic=topic,
                original_text=original_text,
                url=url,
                instruction=instruction,
                extra=extra,
            )
        )
        max_tokens = min(
            max(1, int(ai_settings.get("max_tokens") or 2000)),
            max(1, request_cap),
        )
        base_url, api_key = self.generation._resolve_model_credentials(model)
        if not api_key or not base_url:
            yield {
                "type": "error",
                "result": _error_result("OpenRouter API key не настроен"),
            }
            return

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt}
        ]
        conversation_id: int | None = None
        if user_id is not None and prompt_key:
            conversation_id = await self.generation.conv_repo.get_or_create(
                user_id=int(user_id),
                prompt_key=str(prompt_key),
                channel_id=int(channel_id),
            )
            budget = max(512, int(max_tokens * 0.7))
            summary, summary_tokens = await self.generation.conv_repo.get_summary(
                conversation_id
            )
            if summary:
                messages.append(
                    {"role": "system", "content": f"Сводка контекста:\n{summary}"}
                )
            recent = await self.generation.conv_repo.list_recent_by_tokens(
                conversation_id,
                max_tokens=max(0, budget - int(summary_tokens or 0)),
            )
            messages.extend(recent)

        messages.append({"role": "user", "content": user_prompt})
        if conversation_id is not None:
            await self.generation.conv_repo.append(
                conversation_id,
                role="user",
                content=user_prompt,
                tokens=0,
            )

        moderation_enabled = bool(ai_settings.get("moderation_enabled", False))
        terminal: ChatResult | None = None
        async for event in self.generation.llm.stream_chat(
            messages=messages,  # type: ignore[arg-type]
            model=model,
            temperature=_float_setting(ai_settings, "temperature", 0.7),
            top_p=_float_setting(ai_settings, "top_p", 1.0),
            max_tokens=max_tokens,
            base_url=str(base_url),
            api_key=str(api_key),
        ):
            event_type = event.get("type")
            if event_type == "delta":
                # Moderated channels buffer raw model text until postprocessing, so
                # forbidden content cannot briefly appear in a Telegram draft.
                if not moderation_enabled:
                    yield event
                continue
            terminal = event.get("result")

        if terminal is None:
            terminal = _error_result("Поток генерации завершился без результата")

        if not terminal.get("success"):
            yield {"type": "error", "result": terminal}
            return

        final_text = terminal.get("text") or ""
        if moderation_enabled:
            final_text = await self.generation.postprocess(
                final_text, ai_settings=ai_settings
            )
            terminal["text"] = final_text
            if final_text:
                yield {"type": "delta", "text": final_text}

        if conversation_id is not None:
            await self.generation.conv_repo.append(
                conversation_id,
                role="assistant",
                content=final_text,
                tokens=int(terminal.get("completion_tokens", 0) or 0),
            )

        used = int(terminal.get("tokens_used", 0) or 0)
        if used > 0:
            await self.generation.ai_repo.increment_tokens(channel_id, used)
        await self.session.commit()
        yield {"type": "done", "result": terminal}

    async def stream_from_link(
        self,
        *,
        channel_id: int,
        url: str,
        mode: str = "summary",
        user_id: int | None = None,
        prompt_key: str | None = None,
        force_custom: bool = False,
    ) -> AsyncIterator[ChatStreamEvent]:
        """Fetch an article safely, extract text, then stream its LLM transform."""
        guard_error, _ = await self._guard(channel_id)
        if guard_error is not None:
            yield {"type": "error", "result": guard_error}
            return

        normalized_mode = str(mode or "summary").lower().strip()
        if normalized_mode not in {"summary", "rewrite", "paraphrase"}:
            normalized_mode = "summary"

        try:
            from app.services.http.fetcher import fetch_html

            html_content = await fetch_html(
                url,
                timeout_seconds=settings.http_fetch_timeout_seconds,
                max_retries=settings.http_fetch_max_retries,
                backoff_initial=settings.http_fetch_backoff_initial,
                backoff_max=settings.http_fetch_backoff_max,
                user_agent=settings.http_fetch_user_agent,
            )
            article_text = extract_article_text(
                html_content,
                max_len=int(settings.content_extract_max_len),
            )
        except Exception as exc:
            logger.warning("Interactive AI link fetch failed url={} error={!r}", url, exc)
            yield {
                "type": "error",
                "result": _error_result(f"Не удалось загрузить страницу: {exc}"),
            }
            return

        if not article_text.strip():
            yield {
                "type": "error",
                "result": _error_result("Не удалось извлечь текст статьи"),
            }
            return

        async for event in self.stream_pipeline(
            channel_id=channel_id,
            mode="from_link",
            url=url,
            extra={
                "article_text": article_text,
                "link_mode": normalized_mode,
                "force_custom": bool(force_custom),
            },
            user_id=user_id,
            prompt_key=prompt_key,
        ):
            yield event
