"""
Сервис для генерации контента через OpenRouter API.
"""

from loguru import logger
from app.core.config import settings
from app.repositories.ai_settings import ChannelAISettingsRepo, AIPresetsRepo
from app.repositories.custom_prompts import CustomSystemPromptsRepo
from app.repositories.conversations import ConversationsRepo
from sqlalchemy.ext.asyncio import AsyncSession
import json
from app.repositories.channels import ChannelsRepo
from app.domain.models import Client
from app.services.llm.openrouter_client import OpenRouterClient
from app.services.llm.prompt_builder import PromptBuilder
from app.services.notifier import Notifier
from app.services.extractors.html import extract_article_text
import uuid as _uuid


class AIGenerationService:
    """Сервис генерации контента через LLM."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.ai_repo = ChannelAISettingsRepo(session)
        self.presets_repo = AIPresetsRepo(session)
        self.conv_repo = ConversationsRepo(session)
        self.custom_repo = CustomSystemPromptsRepo(session)
        self.llm = OpenRouterClient(
            timeout_seconds=settings.openrouter_timeout_seconds,
            max_retries=settings.openrouter_max_retries,
            backoff_initial=settings.openrouter_backoff_initial,
            backoff_max=settings.openrouter_backoff_max,
        )
        self.prompt_builder = PromptBuilder(self.presets_repo, self.custom_repo)
        self.notifier = Notifier(session)

    async def clear_history(
        self,
        *,
        user_id: int,
        prompt_key: str | None = None,
        channel_id: int | None = None,
    ) -> int:
        """Очистить историю диалога.
        Если указан prompt_key — удалит только один диалог пользователя.
        Если указан channel_id — удалит все диалоги, привязанные к каналу.
        Иначе — все диалоги пользователя.
        Возвращает количество удалённых диалогов.
        """
        if prompt_key:
            return await self.conv_repo.delete_by_user_and_prompt_key(
                user_id=user_id,
                prompt_key=prompt_key,
                channel_id=channel_id,
            )
        if channel_id is not None:
            return await self.conv_repo.delete_all_by_channel(channel_id)
        return await self.conv_repo.delete_all_by_user(user_id)

    async def generate_text(self, channel_id: int, topic: str, **extra_vars) -> dict:
        """
        Генерация текста для поста.

        Args:
                channel_id: ID канала
                topic: Тема поста
                **extra_vars: Дополнительные переменные для промпта (brand, audience, cta и т.д.)

        Returns:
                dict с ключами: success, text, tokens_used, error
        """
        ai_settings = await self.ai_repo.get_or_create(channel_id)

        if not ai_settings.enabled:
            return {
                "success": False,
                "error": "ИИ отключен для этого канала",
                "text": None,
                "tokens_used": 0,
            }

        eff_day, eff_month, req_cap = await self._effective_limits(
            ai_settings, channel_id
        )
        if eff_day is not None and int(ai_settings.tokens_used_day or 0) >= int(
            eff_day
        ):
            return {
                "success": False,
                "error": "Превышен дневной лимит токенов",
                "text": None,
                "tokens_used": 0,
            }
        if eff_month is not None and int(ai_settings.tokens_used_month or 0) >= int(
            eff_month
        ):
            return {
                "success": False,
                "error": "Превышен месячный лимит токенов",
                "text": None,
                "tokens_used": 0,
            }

        system_prompt, user_prompt = await self._build_prompts(
            ai_settings, topic, extra_vars
        )

        if (ai_settings.max_tokens or 0) > int(req_cap):
            ai_settings.max_tokens = int(req_cap)
        chosen_model = self._pick_model(ai_settings, mode="from_scratch")
        base_url, api_key = self._resolve_model_credentials(chosen_model)

        user_id = extra_vars.get("user_id")
        prompt_key = extra_vars.get("prompt_key")
        if user_id and prompt_key:
            messages = [{"role": "system", "content": system_prompt}]
            conv_id = await self.conv_repo.get_or_create(
                user_id=int(user_id), prompt_key=str(prompt_key), channel_id=channel_id
            )
            budget = max(512, int((ai_settings.max_tokens or 2000) * 0.7))
            summary, summary_tokens = await self.conv_repo.get_summary(conv_id)
            if summary:
                messages.append(
                    {"role": "system", "content": f"Сводка контекста:\n{summary}"}
                )
            recent = await self.conv_repo.list_recent_by_tokens(
                conv_id, max_tokens=budget - int(summary_tokens or 0)
            )
            messages.extend(recent)
            messages.append({"role": "user", "content": user_prompt})
            await self.conv_repo.append(
                conv_id, role="user", content=user_prompt, tokens=0
            )
            result = await self._call_openrouter_messages(
                messages,
                model=chosen_model,
                temperature=ai_settings.temperature,
                top_p=ai_settings.top_p,
                max_tokens=ai_settings.max_tokens,
                base_url=base_url,
                api_key=api_key,
            )
            if result.get("success"):
                await self.conv_repo.append(
                    conv_id,
                    role="assistant",
                    content=result.get("text") or "",
                    tokens=int(result.get("completion_tokens", 0) or 0),
                )
        else:
            result = await self._call_openrouter(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=chosen_model,
                temperature=ai_settings.temperature,
                top_p=ai_settings.top_p,
                max_tokens=ai_settings.max_tokens,
                base_url=base_url,
                api_key=api_key,
            )

        if not result["success"]:
            return result

        tokens_used = result.get("tokens_used", 0)
        if tokens_used > 0:
            await self.ai_repo.increment_tokens(channel_id, tokens_used)
            try:
                is_pro = await self._is_pro_channel(channel_id)
                if is_pro:
                    settings_now = await self.ai_repo.get_or_create(channel_id)
                    day_limit, month_limit, _ = await self._effective_limits(
                        settings_now, channel_id
                    )
                    if month_limit:
                        used = int(getattr(settings_now, "tokens_used_month", 0) or 0)
                        pct = (used / int(month_limit)) if month_limit else 0
                        if pct >= 0.8 and pct < 1.0:
                            await self.notifier.notify_ai_tokens_80pct(
                                channel_id, used, int(month_limit), pct
                            )
            except Exception:
                pass

        text = result["text"]
        if ai_settings.moderation_enabled:
            text = await self._moderate_text(text, ai_settings)

        return {
            "success": True,
            "text": text,
            "tokens_used": tokens_used,
            "error": None,
        }

    async def build_prompt(
        self,
        channel_id: int,
        *,
        mode: str,
        topic: str = "",
        original_text: str = "",
        url: str = "",
        instruction: str | None = None,
        extra: dict | None = None,
    ) -> tuple[dict, str, str, str]:
        extra = extra or {}
        ai_settings = await self.ai_repo.get_or_create(channel_id)
        if mode == "from_link":
            inst = instruction or self._default_instruction_by_mode(
                extra.get("link_mode") or "summary"
            )
            system_prompt, user_prompt = await self._build_prompts(
                ai_settings,
                topic="",
                extra_vars={
                    "article_text": extra.get("article_text", ""),
                    "source_url": url,
                    "instruction": inst,
                    "force_custom": bool(extra.get("force_custom", False)),
                },
            )
            chosen_model = self._pick_model(
                ai_settings, mode=extra.get("link_mode") or "summary"
            )
        elif mode == "improve":
            system_prompt, user_prompt = await self._build_prompts(
                ai_settings,
                topic="",
                extra_vars={
                    "original_text": original_text,
                    "instruction": (instruction or "улучши"),
                    "force_custom": bool(extra.get("force_custom", False)),
                },
            )
            chosen_model = self._pick_model(ai_settings, mode="rewrite")
        else:
            system_prompt, user_prompt = await self._build_prompts(
                ai_settings, topic, extra
            )
            chosen_model = self._pick_model(ai_settings, mode="from_scratch")
        return ai_settings.__dict__, chosen_model, system_prompt, user_prompt

    async def generate_with_model(
        self,
        *,
        ai_settings: dict,
        model: str,
        system_prompt: str,
        user_prompt: str,
        user_id: int | None = None,
        prompt_key: str | None = None,
    ) -> dict:
        class _Obj:
            def __init__(self, d: dict):
                self.__dict__.update(d)

        aset = _Obj(ai_settings)
        base_url, api_key = self._resolve_model_credentials(model)
        if user_id and prompt_key:
            messages = [{"role": "system", "content": system_prompt}]
            conv_id = await self.conv_repo.get_or_create(
                user_id=int(user_id),
                prompt_key=str(prompt_key),
                channel_id=int(
                    getattr(aset, "channel_id", 0) or ai_settings.get("channel_id", 0)
                ),
            )
            budget = max(512, int((aset.max_tokens or 2000) * 0.7))
            summary, summary_tokens = await self.conv_repo.get_summary(conv_id)
            if summary:
                messages.append(
                    {"role": "system", "content": f"Сводка контекста:\n{summary}"}
                )
            recent = await self.conv_repo.list_recent_by_tokens(
                conv_id, max_tokens=budget - int(summary_tokens or 0)
            )
            messages.extend(recent)
            messages.append({"role": "user", "content": user_prompt})
            await self.conv_repo.append(
                conv_id, role="user", content=user_prompt, tokens=0
            )
            result = await self._call_openrouter_messages(
                messages,
                model=model,
                temperature=aset.temperature,
                top_p=aset.top_p,
                max_tokens=aset.max_tokens,
                base_url=base_url,
                api_key=api_key,
            )
            if result.get("success"):
                await self.conv_repo.append(
                    conv_id,
                    role="assistant",
                    content=result.get("text") or "",
                    tokens=int(result.get("completion_tokens", 0) or 0),
                )
        else:
            result = await self._call_openrouter(
                system_prompt,
                user_prompt,
                model,
                aset.temperature,
                aset.top_p,
                aset.max_tokens,
                base_url=base_url,
                api_key=api_key,
            )
        if result.get("success"):
            cid = int(
                getattr(aset, "channel_id", 0) or ai_settings.get("channel_id", 0) or 0
            )
            if cid:
                await self.ai_repo.increment_tokens(
                    cid, int(result.get("tokens_used", 0))
                )
        return result

    async def postprocess(self, text: str, *, ai_settings: dict) -> str:
        class _Obj:
            def __init__(self, d: dict):
                self.__dict__.update(d)

        aset = _Obj(ai_settings)
        if getattr(aset, "moderation_enabled", False):
            text = await self._moderate_text(text, aset)
        return text

    async def run_pipeline(
        self,
        *,
        channel_id: int,
        mode: str,
        topic: str = "",
        original_text: str = "",
        url: str = "",
        instruction: str | None = None,
        extra: dict | None = None,
        user_id: int | None = None,
        prompt_key: str | None = None,
    ) -> dict:
        guard_settings = await self.ai_repo.get_or_create(channel_id)
        if not bool(getattr(guard_settings, "enabled", False)):
            return {
                "success": False,
                "text": None,
                "tokens_used": 0,
                "error": "ИИ отключен для этого канала",
            }
        day_limit, month_limit, request_cap = await self._effective_limits(
            guard_settings, channel_id
        )
        if day_limit is not None and int(guard_settings.tokens_used_day or 0) >= int(day_limit):
            return {
                "success": False,
                "text": None,
                "tokens_used": 0,
                "error": "Превышен дневной лимит токенов",
            }
        if month_limit is not None and int(guard_settings.tokens_used_month or 0) >= int(month_limit):
            return {
                "success": False,
                "text": None,
                "tokens_used": 0,
                "error": "Превышен месячный лимит токенов",
            }

        ai_settings, model, system_prompt, user_prompt = await self.build_prompt(
            channel_id,
            mode=mode,
            topic=topic,
            original_text=original_text,
            url=url,
            instruction=instruction,
            extra=extra,
        )
        ai_settings["max_tokens"] = min(
            max(1, int(ai_settings.get("max_tokens") or 2000)),
            max(1, int(request_cap)),
        )
        res = await self.generate_with_model(
            ai_settings=ai_settings,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            user_id=user_id,
            prompt_key=prompt_key,
        )
        if res.get("success"):
            res["text"] = await self.postprocess(
                res.get("text") or "", ai_settings=ai_settings
            )
        return res

    async def _is_pro_channel(self, channel_id: int) -> bool:
        try:
            async with self.session.bind.connect() as _:
                pass
        except Exception:
            pass
        try:
            repo = ChannelsRepo(self.session)
            ch = await repo.get_by_id(channel_id)
            if not ch:
                return False
            owner = await self.session.get(Client, int(getattr(ch, "owner_id", 0)))
            return bool(getattr(owner, "is_premium", False)) if owner else False
        except Exception:
            return False

    async def _build_prompts(
        self, ai_settings, topic: str, extra_vars: dict
    ) -> tuple[str, str]:
        return await self.prompt_builder.build(
            ai_settings, topic=topic, extra_vars=extra_vars
        )

    async def _call_openrouter(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> dict:
        api_key = api_key or settings.openrouter_api_key
        base_url = base_url or settings.openrouter_base_url
        if not api_key:
            return {
                "success": False,
                "error": "OpenRouter API key не настроен",
                "text": None,
                "tokens_used": 0,
            }
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        req_id = _uuid.uuid4().hex
        return await self._call_openrouter_messages(
            messages,
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            base_url=base_url,
            api_key=api_key,
            request_id=req_id,
        )

    async def _moderate_text(self, text: str, ai_settings) -> str:
        if ai_settings.forbidden_words:
            text_lower = text.lower()
            for word in ai_settings.forbidden_words:
                if word.lower() in text_lower:
                    text = text.replace(word, "***")
                    logger.warning(f"⚠️ Модерация: заменено запрещённое слово '{word}'")
        return text

    async def generate_from_link(
        self,
        channel_id: int,
        url: str,
        mode: str = "summary",
        *,
        force_custom: bool = False,
        user_id: int | None = None,
        prompt_key: str | None = None,
    ) -> dict:
        ai_settings = await self.ai_repo.get_or_create(channel_id)
        eff_day, eff_month, req_cap = await self._effective_limits(
            ai_settings, channel_id
        )
        if eff_month is not None and int(ai_settings.tokens_used_month or 0) >= int(
            eff_month
        ):
            return {
                "success": False,
                "text": "",
                "tokens_used": 0,
                "error": "Превышен месячный лимит токенов",
            }
        if (ai_settings.max_tokens or 0) > int(req_cap):
            ai_settings.max_tokens = int(req_cap)
        is_pro = await self._is_pro_channel(channel_id)
        if not is_pro:
            try:
                from datetime import datetime, timezone

                day_key = datetime.now(timezone.utc).strftime("%Y%m%d")
                conv_id = await self.conv_repo.get_or_create(
                    user_id=0,
                    prompt_key=f"summary_day_{day_key}",
                    channel_id=channel_id,
                )
                recent = await self.conv_repo.list_recent_by_tokens(
                    conv_id, max_tokens=10
                )
                count_today = sum(1 for m in recent if m.get("role") == "assistant")
                if count_today >= 3:
                    return {
                        "success": False,
                        "text": "",
                        "tokens_used": 0,
                        "error": "Лимит саммари в Free: 3 в сутки. Доступно в Pro без ограничений.",
                    }
            except Exception:
                pass
        if ai_settings.preset_id:
            await self.presets_repo.get_by_id(ai_settings.preset_id)

        try:
            from app.services.http.fetcher import fetch_html

            req_id = _uuid.uuid4().hex
            html_content = await fetch_html(
                url,
                timeout_seconds=settings.http_fetch_timeout_seconds,
                max_retries=settings.http_fetch_max_retries,
                backoff_initial=settings.http_fetch_backoff_initial,
                backoff_max=settings.http_fetch_backoff_max,
                request_id=req_id,
                user_agent=settings.http_fetch_user_agent,
            )
        except Exception as e:
            logger.error(f"Ошибка загрузки URL {url}: {e}")
            return {
                "success": False,
                "text": "",
                "tokens_used": 0,
                "error": f"Не удалось загрузить страницу: {str(e)}",
            }

        try:
            article_text = extract_article_text(
                html_content, max_len=int(settings.content_extract_max_len)
            )
        except Exception as e:
            logger.error(f"Ошибка парсинга HTML: {e}")
            return {
                "success": False,
                "text": "",
                "tokens_used": 0,
                "error": f"Не удалось извлечь текст: {str(e)}",
            }

        instruction = self._default_instruction_by_mode(mode)
        system_prompt, user_prompt = await self._build_prompts(
            ai_settings,
            topic="",
            extra_vars={
                "article_text": article_text,
                "source_url": url,
                "instruction": instruction,
                "force_custom": force_custom,
            },
        )

        chosen_model = self._pick_model(ai_settings, mode=mode)
        base_url, api_key = self._resolve_model_credentials(chosen_model)

        if user_id and prompt_key:
            messages = [{"role": "system", "content": system_prompt}]
            conv_id = await self.conv_repo.get_or_create(
                user_id=int(user_id), prompt_key=str(prompt_key), channel_id=channel_id
            )
            budget = max(512, int((ai_settings.max_tokens or 2000) * 0.7))
            summary, summary_tokens = await self.conv_repo.get_summary(conv_id)
            if summary:
                messages.append(
                    {"role": "system", "content": f"Сводка контекста:\n{summary}"}
                )
            recent = await self.conv_repo.list_recent_by_tokens(
                conv_id, max_tokens=budget - int(summary_tokens or 0)
            )
            messages.extend(recent)
            messages.append({"role": "user", "content": user_prompt})
            await self.conv_repo.append(
                conv_id, role="user", content=user_prompt, tokens=0
            )
            result = await self._call_openrouter_messages(
                messages,
                model=chosen_model,
                temperature=ai_settings.temperature,
                top_p=ai_settings.top_p,
                max_tokens=ai_settings.max_tokens,
                base_url=base_url,
                api_key=api_key,
            )
            if result.get("success"):
                await self.conv_repo.append(
                    conv_id,
                    role="assistant",
                    content=result.get("text") or "",
                    tokens=int(result.get("completion_tokens", 0) or 0),
                )
        else:
            result = await self._call_openrouter(
                system_prompt,
                user_prompt,
                chosen_model,
                ai_settings.temperature,
                ai_settings.top_p,
                ai_settings.max_tokens,
                base_url=base_url,
                api_key=api_key,
            )

        if result["success"]:
            await self.ai_repo.increment_tokens(
                channel_id, result.get("tokens_used", 0)
            )
            if not is_pro:
                try:
                    from datetime import datetime, timezone

                    day_key = datetime.now(timezone.utc).strftime("%Y%m%d")
                    conv_id = await self.conv_repo.get_or_create(
                        user_id=0,
                        prompt_key=f"summary_day_{day_key}",
                        channel_id=channel_id,
                    )
                    await self.conv_repo.append(
                        conv_id, role="assistant", content="ok", tokens=0
                    )
                except Exception:
                    pass
            if ai_settings.moderation_enabled:
                result["text"] = await self._moderate_text(result["text"], ai_settings)

        return result

    async def improve_text(
        self,
        channel_id: int,
        original_text: str,
        instruction: str = "улучши",
        *,
        force_custom: bool = False,
        user_id: int | None = None,
        prompt_key: str | None = None,
    ) -> dict:
        ai_settings = await self.ai_repo.get_or_create(channel_id)
        eff_day, eff_month, req_cap = await self._effective_limits(
            ai_settings, channel_id
        )
        if eff_day is not None and int(ai_settings.tokens_used_day or 0) >= int(
            eff_day
        ):
            return {
                "success": False,
                "text": "",
                "tokens_used": 0,
                "error": "Превышен дневной лимит токенов",
            }
        if eff_month is not None and int(ai_settings.tokens_used_month or 0) >= int(
            eff_month
        ):
            return {
                "success": False,
                "text": "",
                "tokens_used": 0,
                "error": "Превышен месячный лимит токенов",
            }
        if (ai_settings.max_tokens or 0) > int(req_cap):
            ai_settings.max_tokens = int(req_cap)

        system_prompt, user_prompt = await self._build_prompts(
            ai_settings,
            topic="",
            extra_vars={
                "original_text": original_text,
                "instruction": instruction,
                "force_custom": force_custom,
            },
        )

        chosen_model = self._pick_model(ai_settings, mode="rewrite")
        base_url, api_key = self._resolve_model_credentials(chosen_model)

        messages = [{"role": "system", "content": system_prompt}]
        if user_id and prompt_key:
            conv_id = await self.conv_repo.get_or_create(
                user_id=user_id, prompt_key=prompt_key, channel_id=channel_id
            )
            budget = max(512, int((ai_settings.max_tokens or 2000) * 0.7))
            summary, summary_tokens = await self.conv_repo.get_summary(conv_id)
            if summary:
                messages.append(
                    {"role": "system", "content": f"Сводка контекста:\n{summary}"}
                )
            recent = await self.conv_repo.list_recent_by_tokens(
                conv_id, max_tokens=budget - int(summary_tokens or 0)
            )
            messages.extend(recent)
            messages.append({"role": "user", "content": user_prompt})
            await self.conv_repo.append(
                conv_id, role="user", content=user_prompt, tokens=0
            )
            result = await self._call_openrouter_messages(
                messages,
                model=chosen_model,
                temperature=ai_settings.temperature,
                top_p=ai_settings.top_p,
                max_tokens=ai_settings.max_tokens,
                base_url=base_url,
                api_key=api_key,
            )
            if result.get("success"):
                answer = result.get("text") or ""
                await self.conv_repo.append(
                    conv_id,
                    role="assistant",
                    content=answer,
                    tokens=int(result.get("completion_tokens", 0) or 0),
                )
        else:
            result = await self._call_openrouter(
                system_prompt,
                user_prompt,
                chosen_model,
                ai_settings.temperature,
                ai_settings.top_p,
                ai_settings.max_tokens,
                base_url=base_url,
                api_key=api_key,
            )

        if result["success"]:
            await self.ai_repo.increment_tokens(
                channel_id, result.get("tokens_used", 0)
            )
            if ai_settings.moderation_enabled:
                result["text"] = await self._moderate_text(result["text"], ai_settings)

        return result

    async def _call_openrouter_messages(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        base_url: str | None = None,
        api_key: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        api_key = api_key or settings.openrouter_api_key
        base_url = base_url or settings.openrouter_base_url
        if not api_key:
            return {
                "success": False,
                "error": "OpenRouter API key не настроен",
                "text": None,
                "tokens_used": 0,
            }
        req_id = request_id or _uuid.uuid4().hex
        res = await self.llm.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            base_url=base_url,
            api_key=api_key,
            request_id=req_id,
        )
        return res

    def _pick_model(self, ai_settings, mode: str) -> str:
        try:
            filters = dict(getattr(ai_settings, "filters", {}) or {})
            ai_models = dict(filters.get("ai_models", {}) or {})
            return (
                ai_models.get(mode)
                or ai_models.get(mode.replace("-", "_"))
                or ai_settings.model
            )
        except Exception:
            return ai_settings.model

    def _resolve_model_credentials(
        self, model_code: str
    ) -> tuple[str | None, str | None]:
        raw = (settings.ai_models_json or "").strip()
        if not raw:
            return settings.openrouter_base_url, settings.openrouter_api_key
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                for it in data:
                    if isinstance(it, dict) and str(it.get("code")) == model_code:
                        return (
                            it.get("base_url") or settings.openrouter_base_url,
                            it.get("api_key") or settings.openrouter_api_key,
                        )
            elif isinstance(data, dict):
                return settings.openrouter_base_url, settings.openrouter_api_key
        except Exception:
            pass
        return settings.openrouter_base_url, settings.openrouter_api_key

    async def _effective_limits(
        self, ai_settings, channel_id: int
    ) -> tuple[int | None, int | None, int]:
        is_pro = await self._is_pro_channel(channel_id)
        plan_day = None if is_pro else 5_000
        plan_month = 2_000_000 if is_pro else 50_000
        eff_day = (
            int(ai_settings.tokens_limit_day)
            if getattr(ai_settings, "tokens_limit_day", None)
            else plan_day
        )
        eff_month = (
            int(ai_settings.tokens_limit_month)
            if getattr(ai_settings, "tokens_limit_month", None)
            else plan_month
        )
        req_cap = 4096 if is_pro else 1024
        return eff_day, eff_month, req_cap

    def _default_instruction_by_mode(self, mode: str) -> str:
        mapping = {
            "summary": "Создай краткое саммари (резюме) этой статьи для Telegram-поста.",
            "rewrite": "Перепиши эту статью своими словами, сохраняя ключевые идеи.",
            "paraphrase": "Перефразируй эту статью, сделав её более понятной.",
        }
        return mapping.get(mode, mapping["summary"])
